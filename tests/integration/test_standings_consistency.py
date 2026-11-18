"""Scenario family: consistency.

Rebuilding the standings from authoritative records must produce exactly what the serving path
produces, for both scoring modes, after a mixture of verdicts, rejudges and judge failures.
"""
from __future__ import annotations

import pytest

from conftest import submit, wait_for_terminal

pytestmark = pytest.mark.integration


def comparable(board: dict) -> list[tuple]:
    return [
        (row["rank"], row["user_id"], row["solved"], row["points"], row["penalty_minutes"])
        for row in board["items"]
    ]


def mixed_contest(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    meera = bundle["participants"]["meera@acme.test"]
    pending = []
    for client, role in (
        (rohan, "wrong_output"),
        (rohan, "accepted"),
        (rohan, "runtime_failure"),
        (meera, "partial_output"),
        (meera, "wrong_output"),
        (meera, "compile_error"),
    ):
        pending.append((client, submit(client, bundle["contest_id"], bundle["problem_id"], programs(role)).json()["id"]))
    for client, submission_id in pending:
        wait_for_terminal(client, submission_id)
    return bundle


def test_all_three_paths_agree(fresh_contest, programs):
    bundle = mixed_contest(fresh_contest, programs)
    organizer = bundle["organizer"]

    served = organizer.get(f"/contests/{bundle['contest_id']}/leaderboard?source=standings").json()
    naive = organizer.get(f"/contests/{bundle['contest_id']}/leaderboard?source=naive").json()
    rebuilt = organizer.get(f"/contests/{bundle['contest_id']}/leaderboard?source=rebuild").json()

    assert comparable(served) == comparable(naive), "the maintained cells disagree with a fresh derivation"
    assert comparable(served) == comparable(rebuilt), "the maintained cells disagree with a full rebuild"
    assert [page["source"] for page in (served, naive, rebuilt)] == ["standings", "naive", "rebuild"]

    report = organizer.get(f"/contests/{bundle['contest_id']}/standings/verify").json()
    assert report["serving_matches_rebuild"] is True
    assert report["serving_matches_naive"] is True


def test_rebuilding_a_healthy_contest_changes_nothing(fresh_contest, programs):
    bundle = mixed_contest(fresh_contest, programs)
    response = bundle["organizer"].post(f"/contests/{bundle['contest_id']}/standings/rebuild")
    assert response.status_code == 200
    body = response.json()
    assert body["cells_written"] >= 1
    assert body["changed"] is False, "a rebuild must be a no-op when the serving path is correct"


def test_paths_still_agree_after_a_rejudge(fresh_contest, programs):
    bundle = mixed_contest(fresh_contest, programs)
    organizer = bundle["organizer"]
    listing = organizer.get(f"/contests/{bundle['contest_id']}/submissions?limit=50").json()
    target = next(row for row in listing["items"] if row["status"] == "JUDGED")
    organizer.post(
        f"/submissions/{target['id']}/rejudge",
        json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
    )
    wait_for_terminal(organizer, target["id"])

    report = organizer.get(f"/contests/{bundle['contest_id']}/standings/verify").json()
    assert report["serving_matches_rebuild"] is True
    assert report["serving_matches_naive"] is True


def test_a_judge_failure_does_not_move_the_standings(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    good = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(rohan, good["id"])
    before = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()

    broken = submit(
        rohan, bundle["contest_id"], bundle["problem_id"], programs("runner_failure_controllable")
    ).json()
    failed = wait_for_terminal(rohan, broken["id"])
    assert failed["status"] == "JUDGE_FAILED"
    assert failed["latest_attempt"]["state"] == "INFRA_FAILED"
    assert failed["latest_attempt"]["verdict"] is None, "an infrastructure failure is not a verdict"
    assert failed["authoritative_attempt"] is None

    after = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/leaderboard").json()
    assert comparable(before) == comparable(after), "a runner failure must not change a score"
    report = bundle["organizer"].get(f"/contests/{bundle['contest_id']}/standings/verify").json()
    assert report["serving_matches_rebuild"] is True


def test_leaderboard_pages_are_consistent_with_the_whole_list(fresh_contest, programs):
    bundle = mixed_contest(fresh_contest, programs)
    organizer = bundle["organizer"]
    whole = organizer.get(f"/contests/{bundle['contest_id']}/leaderboard?limit=50").json()
    paged: list = []
    for offset in range(0, whole["page"]["total"], 1):
        page = organizer.get(f"/contests/{bundle['contest_id']}/leaderboard?limit=1&offset={offset}").json()
        paged.extend(page["items"])
    assert comparable({"items": paged}) == comparable(whole)


def test_query_plans_are_available_for_both_paths(fresh_contest, programs):
    bundle = mixed_contest(fresh_contest, programs)
    for source in ("standings", "naive"):
        response = bundle["organizer"].get(
            f"/contests/{bundle['contest_id']}/standings/explain?source={source}"
        )
        assert response.status_code == 200
        plan = response.json()["plan"]
        assert any("cost=" in line for line in plan), plan[:3]
        assert any("Execution Time" in line for line in plan), plan[-3:]


def test_an_etag_identifies_the_page_not_just_the_contest(fresh_contest, programs):
    """Paging with If-None-Match must never be answered with a different page."""
    bundle = mixed_contest(fresh_contest, programs)
    organizer = bundle["organizer"]
    first = organizer.get(f"/contests/{bundle['contest_id']}/leaderboard?limit=1&offset=0")
    assert first.status_code == 200
    tag = first.headers["ETag"]

    same = organizer.get(
        f"/contests/{bundle['contest_id']}/leaderboard?limit=1&offset=0",
        headers={"If-None-Match": tag},
    )
    assert same.status_code == 304, "an unchanged page must still be cacheable"

    other_page = organizer.get(
        f"/contests/{bundle['contest_id']}/leaderboard?limit=1&offset=1",
        headers={"If-None-Match": tag},
    )
    assert other_page.status_code == 200, "a different page must not be answered with 304"
    assert other_page.json()["items"] != first.json()["items"]

    other_size = organizer.get(
        f"/contests/{bundle['contest_id']}/leaderboard?limit=2&offset=0",
        headers={"If-None-Match": tag},
    )
    assert other_size.status_code == 200
    assert len(other_size.json()["items"]) == 2


def test_a_new_verdict_invalidates_a_cached_page(fresh_contest, programs):
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    held = rohan.get(f"/contests/{bundle['contest_id']}/leaderboard")
    tag = held.headers["ETag"]
    assert (
        rohan.get(f"/contests/{bundle['contest_id']}/leaderboard", headers={"If-None-Match": tag}).status_code
        == 304
    )

    submission = submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")).json()
    wait_for_terminal(rohan, submission["id"])
    refreshed = rohan.get(
        f"/contests/{bundle['contest_id']}/leaderboard", headers={"If-None-Match": tag}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["items"][0]["points"] == 100


def test_the_expensive_derivation_paths_are_organizer_only(fresh_contest, programs):
    """A participant must not be able to ask for the path that re-derives the whole contest."""
    bundle = fresh_contest()
    rohan = bundle["participants"]["rohan@acme.test"]
    for source in ("naive", "rebuild"):
        response = rohan.get(f"/contests/{bundle['contest_id']}/leaderboard?source={source}")
        assert response.status_code == 403, source
        assert response.json()["error"]["type"] == "authorization"
    assert rohan.get(f"/contests/{bundle['contest_id']}/leaderboard?source=standings").status_code == 200
    assert bundle["organizer"].get(
        f"/contests/{bundle['contest_id']}/leaderboard?source=naive"
    ).status_code == 200
