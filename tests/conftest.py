"""Shared fixtures, plus the run report that labels every result.

Nothing here hardcodes a verdict or a leaderboard. Expected outcomes come from the seed data
and from what the rules say, and every assertion checks the stored records as well as the HTTP
response.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import create_engine, text

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://shodh:shodh_dev_password@postgres:5432/shodh")
EVIDENCE_DIR = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))
MANIFEST_PATH = EVIDENCE_DIR / "seed-manifest.json"
PROGRAMS_DIR = pathlib.Path(os.environ.get("PROGRAMS_DIR", "/srv/seed/data/programs"))
# evaluate.sh sets this to false for the phase where it stops the judge workers so the tests
# can be the only judge on the queue.
WORKERS_RUNNING = os.environ.get("WORKERS_RUNNING", "true").lower() != "false"

VERDICT_TIMEOUT = float(os.environ.get("VERDICT_TIMEOUT", "120"))


# --------------------------------------------------------------------------- run report


@dataclass
class RunReport:
    labels: dict = field(default_factory=dict)
    results: list = field(default_factory=list)


_report = RunReport()


def pytest_configure(config):  # noqa: D401
    config.addinivalue_line("markers", "unit: pure rule tests, no database and no execution")
    _report.labels = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit": os.environ.get("COMMIT_SHA", "unknown"),
        "configured_execution_mode": os.environ.get("EXEC_MODE", "unknown"),
        "api_base_url": API_BASE_URL,
        "workers_running": WORKERS_RUNNING,
        "markers_selected": config.getoption("-m") or "all",
    }
    try:
        with httpx.Client(base_url=API_BASE_URL, timeout=5.0) as client:
            _report.labels["api_version"] = client.get("/version").json()
    except Exception as exc:  # pragma: no cover
        _report.labels["api_version_error"] = str(exc)
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        _report.labels["data_version"] = manifest.get("data_version")
        _report.labels["programs_version"] = manifest.get("programs_version")
        _report.labels["seeded_at"] = manifest.get("seeded_at")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        _report.results.append(
            {
                "test": report.nodeid,
                "outcome": report.outcome,
                "duration_s": round(report.duration, 3),
                "markers": sorted(m.name for m in item.iter_markers()),
                "execution_mode": getattr(item, "_shodh_exec_mode", None),
            }
        )


def pytest_terminal_summary(terminalreporter):
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    label = os.environ.get("RUN_LABEL", "tests")
    counts: dict[str, int] = {}
    for row in _report.results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    payload = {
        "label": label,
        "labels": _report.labels,
        "totals": counts,
        "results": _report.results,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = EVIDENCE_DIR / f"test-report-{label}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    terminalreporter.write_line(f"[evidence] wrote {path}")


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def manifest() -> dict:
    assert MANIFEST_PATH.exists(), f"seed manifest missing at {MANIFEST_PATH}; run the seed first"
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(engine):
    with engine.connect() as connection:
        yield connection


@pytest.fixture(scope="session")
def api_version() -> dict:
    with httpx.Client(base_url=API_BASE_URL, timeout=10.0) as client:
        return client.get("/version").json()


@pytest.fixture(scope="session")
def execution_mode(api_version) -> str:
    """The mode the running system is actually judging in. Recorded in every result."""
    return api_version.get("execution_mode", "unknown")


@pytest.fixture(autouse=True)
def _label_execution_mode(request):
    try:
        mode = request.getfixturevalue("execution_mode")
    except Exception:
        mode = None
    request.node._shodh_exec_mode = mode
    yield


class Client:
    """Thin HTTP helper that keeps one bearer token and never hides a status code."""

    def __init__(self, token: str | None = None, label: str = ""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(base_url=API_BASE_URL, headers=headers, timeout=30.0)
        self.label = label

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self._client.request(method, path, **kwargs)

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._client.get(path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self._client.post(path, **kwargs)

    def patch(self, path: str, **kwargs) -> httpx.Response:
        return self._client.patch(path, **kwargs)


@pytest.fixture()
def clients(manifest):
    opened: list[Client] = []

    def make(token: str | None = None, label: str = "") -> Client:
        client = Client(token, label)
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.close()


@pytest.fixture()
def tokens(manifest) -> dict:
    out = {"judge": manifest["service_tokens"]["judge"], "platform_admin": manifest["service_tokens"]["platform_admin"]}
    for org in manifest["organizations"].values():
        for email, user in org["users"].items():
            out[email] = user["token"]
    return out


@pytest.fixture()
def programs():
    """Look up a seeded program: programs(role, language="python3", problem="sum-two").

    The seed bundle labels each program with the problem it solves, its language and its role
    (accepted, wrong_output, timeout, ...). Tests ask by role; what the program is *expected* to
    produce is asserted by the test, never carried in the bundle.
    """
    manifest = json.loads((PROGRAMS_DIR / "manifest.json").read_text(encoding="utf-8"))
    index = {
        (entry["problem"], entry["role"], entry["language"]): (PROGRAMS_DIR / entry["file"]).read_text(
            encoding="utf-8"
        )
        for entry in manifest["programs"]
    }

    def get(role: str, language: str = "python3", problem: str = "sum-two") -> str:
        try:
            return index[(problem, role, language)]
        except KeyError:
            raise KeyError(
                f"no seeded program for problem={problem} role={role} language={language}; "
                f"available: {sorted(index)}"
            ) from None

    get.index = index  # type: ignore[attr-defined]
    return get


# --------------------------------------------------------------------------- helpers


def key() -> str:
    return f"it-{uuid.uuid4().hex}"


def submit(
    client: Client,
    contest_id: str,
    problem_id: str,
    source: str,
    *,
    language: str = "python3",
    idempotency_key: str | None = None,
    expect: tuple[int, ...] = (201, 200),
) -> httpx.Response:
    response = client.post(
        f"/contests/{contest_id}/submissions",
        json={"problem_id": problem_id, "language": language, "source_code": source},
        headers={"Idempotency-Key": idempotency_key or key()},
    )
    if expect:
        assert response.status_code in expect, response.text
    return response


def wait_for_terminal(client: Client, submission_id: str, timeout: float = VERDICT_TIMEOUT) -> dict:
    """Poll until the submission has a verdict or the judge gave up."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/submissions/{submission_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in ("JUDGED", "JUDGE_FAILED"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"submission {submission_id} never reached a terminal state: {last}")


def attempt_rows(db, submission_id: str) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT attempt_no, state, verdict, score, max_score, lease_epoch, exec_mode,
                   worker_id, failure_reason, problem_version_id, test_suite_id
            FROM judging_attempts WHERE submission_id = :s ORDER BY attempt_no
            """
        ),
        {"s": submission_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def job_row(db, attempt_id: str) -> dict | None:
    row = db.execute(
        text(
            "SELECT state, delivery_count, max_deliveries, available_at, lease_expires_at, leased_by,"
            " last_error FROM judge_jobs WHERE attempt_id = :a"
        ),
        {"a": attempt_id},
    ).mappings().first()
    return dict(row) if row else None


def cell_row(db, contest_id: str, user_id: str, problem_id: str) -> dict | None:
    row = db.execute(
        text(
            """
            SELECT points, solved, penalty_minutes, rejected_before_solve, authoritative_attempt_id
            FROM problem_scores
            WHERE contest_id = :c AND user_id = :u AND problem_id = :p
            """
        ),
        {"c": contest_id, "u": user_id, "p": problem_id},
    ).mappings().first()
    return dict(row) if row else None


# Hidden cases use distinctive digit strings so a leak test can look for them literally.
HIDDEN_INPUT_MARKERS = ("717171", "123456")
HIDDEN_OUTPUT_MARKERS = ("999999", "777777")
SIMPLE_SUITE = {
    "label": "created by the test suite",
    "cases": [
        {"ordinal": 1, "input": "2 3\n", "expected_output": "5\n", "is_sample": True, "weight": 1},
        {"ordinal": 2, "input": "717171 282828\n", "expected_output": "999999\n", "is_sample": False, "weight": 1},
        {"ordinal": 3, "input": "123456 654321\n", "expected_output": "777777\n", "is_sample": False, "weight": 1},
    ],
}


@pytest.fixture()
def fresh_contest(manifest, clients, tokens):
    """A contest, problem and test suite created through the public API, not the seed.

    Used so each test owns its own standings, and so the system is shown accepting contest and
    problem data it has never seen through the documented contracts.
    """
    created: list[dict] = []

    def make(
        *,
        org: str = "acme-university",
        organizer_email: str = "organizer@acme.test",
        participants: tuple[str, ...] = ("rohan@acme.test", "meera@acme.test"),
        penalty_minutes: int = 20,
        max_attempts_per_problem: int | None = None,
        starts_in_minutes: int = -5,
        ends_in_minutes: int = 600,
        problem: dict | None = None,
    ) -> dict:
        import datetime as _dt

        organizer = Client(tokens[organizer_email], organizer_email)
        now = _dt.datetime.now(_dt.timezone.utc)
        slug = f"t-{uuid.uuid4().hex[:10]}"
        response = organizer.post(
            "/contests",
            json={
                "slug": slug,
                "title": f"Test contest {slug}",
                "starts_at": (now + _dt.timedelta(minutes=starts_in_minutes)).isoformat(),
                "ends_at": (now + _dt.timedelta(minutes=ends_in_minutes)).isoformat(),
                "penalty_minutes": penalty_minutes,
                "max_attempts_per_problem": max_attempts_per_problem,
            },
        )
        assert response.status_code == 201, response.text
        contest = response.json()

        spec = problem or {
            "slug": "sum-two",
            "title": "Sum of Two Integers",
            "ordinal": 1,
            "version": {
                "statement": "Print a + b.",
                "time_limit_ms": 2000,
                "memory_limit_mb": 128,
                "output_limit_bytes": 8192,
                "max_points": 100,
                "compare_mode": "TRIM_TRAILING_WS",
                "test_suite": SIMPLE_SUITE,
            },
        }
        response = organizer.post(f"/contests/{contest['id']}/problems", json=spec)
        assert response.status_code == 201, response.text
        problem_row = response.json()

        joined = {}
        for email in participants:
            participant = Client(tokens[email], email)
            join = participant.post(f"/contests/{contest['id']}/participants/me")
            assert join.status_code in (200, 201), join.text
            joined[email] = participant

        bundle = {
            "contest": contest,
            "contest_id": contest["id"],
            "organizer": organizer,
            "problem": problem_row,
            "problem_id": problem_row["id"],
            "version_id": problem_row["current_version_id"],
            "suite_id": problem_row["current_version"]["default_test_suite_id"],
            "participants": joined,
            "user_ids": {
                email: manifest["organizations"][org]["users"][email]["id"] for email in participants
            },
        }
        created.append(bundle)
        return bundle

    yield make
    for bundle in created:
        bundle["organizer"].close()
        for participant in bundle["participants"].values():
            participant.close()


def close_contest(bundle: dict) -> None:
    """Shift the contest window into the past, the way a contest ends in practice.

    Participants have to join and submit while it is open, so a test that needs a closed
    contest opens one, does its setup, then closes it through the organizer API.
    """
    import datetime as _dt

    starts = _dt.datetime.fromisoformat(bundle["contest"]["starts_at"])
    response = bundle["organizer"].patch(
        f"/contests/{bundle['contest_id']}",
        json={"ends_at": (starts + _dt.timedelta(minutes=1)).isoformat()},
    )
    assert response.status_code == 200, response.text


def require_quiet_queue():
    if WORKERS_RUNNING:
        pytest.skip(
            "needs exclusive control of the judge queue; run with the workers stopped "
            "(scripts/evaluate.sh does this in its concurrency phase)"
        )
