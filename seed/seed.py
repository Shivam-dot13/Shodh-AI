"""Deterministic seed bundle.

Run it twice and nothing changes: every row has a uuid5 identifier derived from a stable name,
and every write is a get-or-create. That is what lets the functional suite, the concurrency
suite and the workload all run against the same labelled data version.

Writes evidence/seed-manifest.json, which is the contract the tests, the demo script and the
workload harness read: identifiers, fixture tokens, program roles and the data version.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/srv/api")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))

from app.auth import hash_token  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.domain.enums import CompareMode, Role, ScoringMode, TokenKind  # noqa: E402
from app.models import (  # noqa: E402
    ApiToken,
    Contest,
    ContestParticipant,
    Organization,
    Problem,
    ProblemVersion,
    TestCase,
    TestSuite,
    User,
)

NAMESPACE = uuid.UUID("3f2b8d56-5a4c-4f1e-9b2a-7c6d5e4f3a21")
HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
EVIDENCE = pathlib.Path(os.environ.get("EVIDENCE_DIR", "/srv/evidence"))


def det(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, "/".join(parts))


def get_or_create(session, model, row_id: uuid.UUID, **fields):
    row = session.get(model, row_id)
    if row is None:
        row = model(id=row_id, **fields)
        session.add(row)
        session.flush()
        return row, True
    for key, value in fields.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
    return row, False


def ensure_token(session, *, token: str, label: str, scope: str, kind: TokenKind, user_id=None) -> None:
    row_id = det("token", label)
    get_or_create(
        session,
        ApiToken,
        row_id,
        user_id=user_id,
        kind=kind,
        scope=scope,
        label=label,
        token_sha256=hash_token(token),
    )


def seed_problem(session, contest: Contest, spec: dict, library: dict) -> dict:
    name = spec["library"]
    definition = library[name]
    key = f"{contest.org_id}/{contest.slug}/{name}"
    problem, _ = get_or_create(
        session,
        Problem,
        det("problem", key),
        contest_id=contest.id,
        slug=name,
        title=definition["title"],
        ordinal=spec.get("ordinal", 1),
    )

    def ensure_suite(suite_spec: dict, version: int) -> TestSuite:
        suite, _ = get_or_create(
            session,
            TestSuite,
            det("suite", key, str(version)),
            problem_id=problem.id,
            version=version,
            label=suite_spec.get("label", ""),
        )
        for case in suite_spec["cases"]:
            get_or_create(
                session,
                TestCase,
                det("case", key, str(version), str(case["ordinal"])),
                test_suite_id=suite.id,
                ordinal=case["ordinal"],
                is_sample=case.get("is_sample", False),
                weight=case.get("weight", 1),
                input_data=case["input"],
                expected_output=case["expected_output"],
            )
        return suite

    suite_v1 = ensure_suite(definition["suite"], 1)
    suite_v2 = ensure_suite(spec["extra_suite"], 2) if spec.get("extra_suite") else None

    def ensure_version(version_spec: dict, version: int, suite: TestSuite) -> ProblemVersion:
        row, _ = get_or_create(
            session,
            ProblemVersion,
            det("version", key, str(version)),
            problem_id=problem.id,
            version=version,
            statement=version_spec["statement"],
            time_limit_ms=version_spec["time_limit_ms"],
            memory_limit_mb=version_spec["memory_limit_mb"],
            output_limit_bytes=version_spec["output_limit_bytes"],
            max_points=version_spec["max_points"],
            compare_mode=CompareMode(version_spec["compare_mode"]),
            default_test_suite_id=suite.id,
        )
        return row

    version_v1 = ensure_version(definition, 1, suite_v1)
    problem.current_version_id = version_v1.id

    version_v2 = None
    if spec.get("extra_version"):
        extra = spec["extra_version"]
        target = suite_v2 if extra.get("use_extra_suite") and suite_v2 is not None else suite_v1
        version_v2 = ensure_version(extra, 2, target)
        if extra.get("publish"):
            problem.current_version_id = version_v2.id

    session.flush()
    return {
        "slug": name,
        "problem_id": str(problem.id),
        "title": problem.title,
        "current_version_id": str(problem.current_version_id),
        "versions": {
            "1": {"id": str(version_v1.id), "test_suite_id": str(suite_v1.id), "max_points": version_v1.max_points},
            **(
                {"2": {"id": str(version_v2.id), "test_suite_id": str(version_v2.default_test_suite_id), "max_points": version_v2.max_points}}
                if version_v2 is not None
                else {}
            ),
        },
        "test_suites": {
            "1": {"id": str(suite_v1.id), "cases": len(definition["suite"]["cases"])},
            **(
                {"2": {"id": str(suite_v2.id), "cases": len(spec["extra_suite"]["cases"])}}
                if suite_v2 is not None
                else {}
            ),
        },
    }


def main() -> int:
    bundle = json.loads((DATA / "bundle.json").read_text(encoding="utf-8"))
    programs = json.loads((DATA / "programs" / "manifest.json").read_text(encoding="utf-8"))
    anchor = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    session = SessionLocal()
    manifest: dict = {
        "data_version": bundle["data_version"],
        "programs_version": programs["data_version"],
        "seeded_at": datetime.now(timezone.utc).isoformat(),
        "window_anchor": anchor.isoformat(),
        "execution_mode": os.environ.get("EXEC_MODE", "docker"),
        "commit": os.environ.get("COMMIT_SHA", "unknown"),
        "service_tokens": {},
        "organizations": {},
        "contests": {},
        "programs": programs["programs"],
        "programs_dir": "seed/data/programs",
    }
    try:
        judge_token = os.environ.get("JUDGE_SERVICE_TOKEN", "shodh_judge_dev_token")
        admin_token = os.environ.get("PLATFORM_ADMIN_TOKEN", "shodh_platform_admin_dev_token")
        ensure_token(session, token=judge_token, label="judge-worker", scope="judge", kind=TokenKind.SERVICE)
        ensure_token(
            session, token=admin_token, label="platform-admin", scope="platform_admin", kind=TokenKind.SERVICE
        )
        manifest["service_tokens"] = {"judge": judge_token, "platform_admin": admin_token}

        users_by_email: dict[str, User] = {}
        for org_spec in bundle["organizations"]:
            org, _ = get_or_create(
                session,
                Organization,
                det("org", org_spec["slug"]),
                slug=org_spec["slug"],
                name=org_spec["name"],
            )
            org_entry = {"id": str(org.id), "slug": org.slug, "name": org.name, "users": {}}
            for user_spec in org_spec["users"]:
                user, _ = get_or_create(
                    session,
                    User,
                    det("user", user_spec["email"]),
                    org_id=org.id,
                    email=user_spec["email"],
                    display_name=user_spec["display_name"],
                    role=Role(user_spec["role"]),
                )
                users_by_email[user.email] = user
                ensure_token(
                    session,
                    token=user_spec["token"],
                    label=user_spec["email"],
                    scope="user",
                    kind=TokenKind.USER,
                    user_id=user.id,
                )
                org_entry["users"][user.email] = {
                    "id": str(user.id),
                    "role": user.role.value,
                    "display_name": user.display_name,
                    "token": user_spec["token"],
                }
            manifest["organizations"][org.slug] = org_entry

        for contest_spec in bundle["contests"]:
            org_slug = contest_spec["org"]
            org_id = uuid.UUID(manifest["organizations"][org_slug]["id"])
            contest, _ = get_or_create(
                session,
                Contest,
                det("contest", org_slug, contest_spec["slug"]),
                org_id=org_id,
                slug=contest_spec["slug"],
                title=contest_spec["title"],
                starts_at=anchor + timedelta(hours=contest_spec["open_from_hours"]),
                ends_at=anchor + timedelta(hours=contest_spec["open_until_hours"]),
                scoring_mode=ScoringMode(contest_spec["scoring_mode"]),
                penalty_minutes=contest_spec["penalty_minutes"],
                max_attempts_per_problem=contest_spec.get("max_attempts_per_problem"),
            )
            for email in contest_spec["participants"]:
                user = users_by_email[email]
                get_or_create(
                    session,
                    ContestParticipant,
                    det("participant", str(contest.id), email),
                    contest_id=contest.id,
                    user_id=user.id,
                )
            problems = [
                seed_problem(session, contest, spec, bundle["problem_library"])
                for spec in contest_spec["problems"]
            ]
            manifest["contests"][contest_spec["slug"]] = {
                "id": str(contest.id),
                "org": org_slug,
                "org_id": str(org_id),
                "scoring_mode": contest.scoring_mode.value,
                "penalty_minutes": contest.penalty_minutes,
                "max_attempts_per_problem": contest.max_attempts_per_problem,
                "starts_at": contest.starts_at.isoformat(),
                "ends_at": contest.ends_at.isoformat(),
                "open_now": contest.starts_at <= datetime.now(timezone.utc) < contest.ends_at,
                "participants": contest_spec["participants"],
                "problems": {p["slug"]: p for p in problems},
            }

        session.commit()
    finally:
        session.close()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "seed-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "seeded": True,
                "data_version": manifest["data_version"],
                "organizations": list(manifest["organizations"]),
                "contests": list(manifest["contests"]),
                "manifest": str(EVIDENCE / "seed-manifest.json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
