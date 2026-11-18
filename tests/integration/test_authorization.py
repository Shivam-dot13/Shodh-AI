"""Scenario family: security.

Organization and role boundaries on reads and writes, hidden-data protection, and the
separation between the participant-facing API and the judge plane. Knowing an identifier must
not grant access to anything.
"""
from __future__ import annotations

import uuid

import pytest

from conftest import HIDDEN_INPUT_MARKERS, HIDDEN_OUTPUT_MARKERS, submit, wait_for_terminal

pytestmark = pytest.mark.integration


@pytest.fixture()
def outsider(manifest, clients):
    """An organizer from a different organization: maximum privilege, wrong tenant."""
    token = manifest["organizations"]["borealis-institute"]["users"]["organizer@borealis.test"]["token"]
    return clients(token, "borealis organizer")


class TestCrossOrganization:
    def test_a_known_contest_id_is_invisible_from_another_organization(self, fresh_contest, outsider):
        bundle = fresh_contest()
        for path in (
            f"/contests/{bundle['contest_id']}",
            f"/contests/{bundle['contest_id']}/problems",
            f"/contests/{bundle['contest_id']}/leaderboard",
            f"/contests/{bundle['contest_id']}/participants",
            f"/contests/{bundle['contest_id']}/stats",
            f"/problems/{bundle['problem_id']}",
            f"/problems/{bundle['problem_id']}/test-suites",
        ):
            response = outsider.get(path)
            assert response.status_code == 404, f"{path} leaked {response.status_code}"
            assert response.json()["error"]["type"] == "not_found"

    def test_a_known_submission_id_is_invisible_from_another_organization(
        self, fresh_contest, programs, outsider
    ):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        submission = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")
        ).json()
        assert outsider.get(f"/submissions/{submission['id']}").status_code == 404
        assert outsider.get(f"/submissions/{submission['id']}/attempts").status_code == 404
        rejudge = outsider.post(
            f"/submissions/{submission['id']}/rejudge",
            json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
        )
        assert rejudge.status_code == 404

    def test_an_outsider_cannot_write_into_another_organization(self, fresh_contest, outsider):
        bundle = fresh_contest()
        response = outsider.post(
            f"/contests/{bundle['contest_id']}/problems",
            json={
                "slug": "injected",
                "title": "injected",
                "version": {"test_suite": {"cases": [{"ordinal": 1, "input": "1", "expected_output": "1"}]}},
            },
        )
        assert response.status_code == 404
        assert outsider.patch(f"/contests/{bundle['contest_id']}", json={"title": "hijacked"}).status_code == 404

    def test_an_outsider_cannot_read_another_organization_record(self, manifest, outsider):
        acme_org_id = manifest["organizations"]["acme-university"]["id"]
        assert outsider.get(f"/orgs/{acme_org_id}").status_code == 404
        response = outsider.post(
            f"/orgs/{acme_org_id}/users",
            json={"email": f"x{uuid.uuid4().hex[:6]}@acme.test", "display_name": "x", "role": "ORGANIZER"},
        )
        assert response.status_code == 404


class TestRoleBoundaries:
    def test_a_participant_cannot_perform_organizer_actions(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        submission = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")
        ).json()

        denied = {
            "create a problem": rohan.post(
                f"/contests/{bundle['contest_id']}/problems",
                json={
                    "slug": "sneaky",
                    "title": "sneaky",
                    "version": {"test_suite": {"cases": [{"ordinal": 1, "input": "1", "expected_output": "1"}]}},
                },
            ),
            "publish a problem version": rohan.post(
                f"/problems/{bundle['problem_id']}/versions",
                json={"test_suite": {"cases": [{"ordinal": 1, "input": "1", "expected_output": "1"}]}},
            ),
            "add a test suite": rohan.post(
                f"/problems/{bundle['problem_id']}/test-suites",
                json={"cases": [{"ordinal": 1, "input": "1", "expected_output": "1"}]},
            ),
            "list test suites": rohan.get(f"/problems/{bundle['problem_id']}/test-suites"),
            "list participants": rohan.get(f"/contests/{bundle['contest_id']}/participants"),
            "read judge detail": rohan.get(f"/submissions/{submission['id']}/attempts"),
            "rejudge": rohan.post(
                f"/submissions/{submission['id']}/rejudge",
                json={"problem_version_id": bundle["version_id"], "test_suite_id": bundle["suite_id"]},
            ),
            "amend the contest": rohan.patch(f"/contests/{bundle['contest_id']}", json={"title": "mine now"}),
            "rebuild standings": rohan.post(f"/contests/{bundle['contest_id']}/standings/rebuild"),
            "naive leaderboard": rohan.get(f"/contests/{bundle['contest_id']}/leaderboard?source=naive"),
            "rebuild leaderboard": rohan.get(f"/contests/{bundle['contest_id']}/leaderboard?source=rebuild"),
            "verify standings": rohan.get(f"/contests/{bundle['contest_id']}/standings/verify"),
            "read query plans": rohan.get(f"/contests/{bundle['contest_id']}/standings/explain"),
            "read queue depth": rohan.get("/ops/queue"),
            "arm a failpoint": rohan.post("/dev/failpoints", json={"name": "x", "enabled": True}),
        }
        for what, response in denied.items():
            assert response.status_code == 403, f"{what} returned {response.status_code}"
            assert response.json()["error"]["type"] == "authorization"

    def test_an_organizer_cannot_submit(self, fresh_contest, programs):
        bundle = fresh_contest()
        response = bundle["organizer"].post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={
                "problem_id": bundle["problem_id"],
                "language": "python3",
                "source_code": programs("accepted"),
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 403

    def test_a_participant_cannot_see_another_participant_work(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        meera = bundle["participants"]["meera@acme.test"]
        mine = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")
        ).json()

        # Same organization, same contest, but someone else private code: not even its existence.
        assert meera.get(f"/submissions/{mine['id']}").status_code == 404
        listing = meera.get(f"/contests/{bundle['contest_id']}/submissions").json()
        assert all(row["user_id"] == bundle["user_ids"]["meera@acme.test"] for row in listing["items"])
        # Asking for someone else submissions explicitly is ignored, not honoured.
        filtered = meera.get(
            f"/contests/{bundle['contest_id']}/submissions?user_id={bundle['user_ids']['rohan@acme.test']}"
        ).json()
        assert filtered["items"] == []

    def test_an_organizer_can_see_participant_work_in_its_own_organization(
        self, fresh_contest, programs
    ):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        submission = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")
        ).json()
        wait_for_terminal(rohan, submission["id"])
        detail = bundle["organizer"].get(f"/submissions/{submission['id']}")
        assert detail.status_code == 200
        assert detail.json()["source_code"]
        attempts = bundle["organizer"].get(f"/submissions/{submission['id']}/attempts").json()["items"]
        assert attempts[0]["runtime_config"]["time_limit_ms"] == 2000
        assert attempts[0]["lease_epoch"] >= 1


class TestHiddenData:
    def test_participants_never_receive_hidden_test_content(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        submission = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("wrong_output")
        ).json()
        wait_for_terminal(rohan, submission["id"])

        seen = " ".join(
            [
                rohan.get(f"/problems/{bundle['problem_id']}").text,
                rohan.get(f"/contests/{bundle['contest_id']}/problems").text,
                rohan.get(f"/submissions/{submission['id']}").text,
                rohan.get(f"/contests/{bundle['contest_id']}/leaderboard").text,
            ]
        )
        # ordinal 1 is a sample and may be shown; ordinals 2 and 3 are hidden.
        for marker in HIDDEN_INPUT_MARKERS + HIDDEN_OUTPUT_MARKERS:
            assert marker not in seen, f"hidden test data {marker} reached a participant response"

    def test_samples_are_published_but_hidden_cases_are_not(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        detail = rohan.get(f"/problems/{bundle['problem_id']}").json()
        assert [case["ordinal"] for case in detail["sample_tests"]] == [1]

    def test_the_judge_payload_route_is_not_reachable_with_a_user_credential(
        self, fresh_contest, programs
    ):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        submission = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted")
        ).json()
        attempt_id = submission["latest_attempt"]["id"]
        for client in (rohan, bundle["organizer"]):
            response = client.get(f"/internal/judge/attempts/{attempt_id}/payload")
            assert response.status_code == 403


class TestCredentialPlanes:
    def test_a_judge_credential_cannot_use_participant_or_organizer_routes(
        self, manifest, clients, fresh_contest
    ):
        bundle = fresh_contest()
        judge = clients(manifest["service_tokens"]["judge"], "judge")
        for path in (
            "/contests",
            f"/contests/{bundle['contest_id']}",
            f"/contests/{bundle['contest_id']}/leaderboard",
            f"/problems/{bundle['problem_id']}/test-suites",
        ):
            assert judge.get(path).status_code == 403, path

    def test_a_user_credential_cannot_use_the_judge_plane(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        for client in (rohan, bundle["organizer"]):
            lease = client.post("/internal/judge/leases", json={"worker_id": "fake", "capacity": 1})
            assert lease.status_code == 403
            reap = client.post("/internal/judge/reap")
            assert reap.status_code == 403

    def test_an_organizer_credential_cannot_create_organizations(self, fresh_contest):
        bundle = fresh_contest()
        response = bundle["organizer"].post("/orgs", json={"slug": "nope-nope", "name": "Nope"})
        assert response.status_code == 403

    def test_unauthenticated_and_bogus_credentials_are_rejected(self, clients, fresh_contest):
        bundle = fresh_contest()
        anonymous = clients(None, "anonymous")
        bogus = clients("shodh_not_a_real_token", "bogus")
        assert anonymous.get(f"/contests/{bundle['contest_id']}").status_code == 401
        assert bogus.get(f"/contests/{bundle['contest_id']}").status_code == 401
        # Health and version stay open on purpose.
        assert anonymous.get("/healthz").status_code == 200
