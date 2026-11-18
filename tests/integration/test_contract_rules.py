"""Request validation, contest rules and list contracts.

The three failure families have to stay apart: a malformed request is 422, a well formed
request the rules reject is 409, and an object outside the caller tenancy is 404.
"""
from __future__ import annotations

import uuid

import pytest

from conftest import close_contest, submit, wait_for_terminal

pytestmark = pytest.mark.integration


class TestFailureFamilies:
    def test_malformed_request_is_a_validation_error(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        response = rohan.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={"problem_id": "not-a-uuid", "language": "python3", "source_code": "x"},
            headers={"Idempotency-Key": "k1"},
        )
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["type"] == "validation" and body["code"] == "invalid_request"
        assert body["details"], "validation errors must say where the request was wrong"

    def test_unsupported_language_is_a_validation_error(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        response = rohan.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={"problem_id": bundle["problem_id"], "language": "brainfuck", "source_code": "x"},
            headers={"Idempotency-Key": "k2"},
        )
        assert response.status_code == 422

    def test_missing_idempotency_key_is_a_validation_error(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        response = rohan.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={
                "problem_id": bundle["problem_id"],
                "language": "python3",
                "source_code": programs("accepted"),
            },
        )
        assert response.status_code == 422

    def test_header_and_body_key_must_agree(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        response = rohan.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={
                "problem_id": bundle["problem_id"],
                "language": "python3",
                "source_code": programs("accepted"),
                "idempotency_key": "body-key",
            },
            headers={"Idempotency-Key": "header-key"},
        )
        assert response.status_code == 422

    def test_not_joined_is_a_business_rule_error(self, fresh_contest, programs, clients, tokens):
        bundle = fresh_contest(participants=("rohan@acme.test",))
        did_not_join = clients(tokens["meera@acme.test"], "meera")
        response = submit(
            did_not_join,
            bundle["contest_id"],
            bundle["problem_id"],
            programs("accepted"),
            expect=(),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_a_participant"

    def test_submitting_to_a_closed_contest_is_rejected(self, fresh_contest, programs):
        bundle = fresh_contest(starts_in_minutes=-120)
        rohan = bundle["participants"]["rohan@acme.test"]
        close_contest(bundle)
        response = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"), expect=()
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "contest_closed"

    def test_submitting_before_the_contest_opens_is_rejected(self, fresh_contest, programs):
        bundle = fresh_contest(starts_in_minutes=60, ends_in_minutes=120, participants=())
        rohan = bundle["organizer"]  # organizer cannot submit at all
        response = rohan.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={
                "problem_id": bundle["problem_id"],
                "language": "python3",
                "source_code": programs("accepted"),
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 403  # wrong role, checked before contest rules

    def test_attempt_limit_is_a_business_rule_error(self, fresh_contest, programs):
        bundle = fresh_contest(max_attempts_per_problem=2)
        rohan = bundle["participants"]["rohan@acme.test"]
        for _ in range(2):
            submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"))
        response = submit(
            rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"), expect=()
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "attempt_limit_reached"
        assert error["details"]["limit"] == 2

    def test_unknown_ids_are_not_found(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        assert rohan.get(f"/contests/{uuid.uuid4()}").status_code == 404
        assert rohan.get(f"/submissions/{uuid.uuid4()}").status_code == 404
        response = rohan.post(
            f"/contests/{bundle['contest_id']}/submissions",
            json={"problem_id": str(uuid.uuid4()), "language": "python3", "source_code": "print(1)"},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 404

    def test_every_error_carries_a_request_id_when_one_is_supplied(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        response = rohan.get(f"/contests/{uuid.uuid4()}", headers={"X-Request-Id": "trace-me-please"})
        assert response.status_code == 404
        assert response.json()["error"]["request_id"] == "trace-me-please"
        assert response.headers["X-Request-Id"] == "trace-me-please"


class TestListContracts:
    def test_listing_is_paginated_and_stable(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        for n in range(5):
            submit(
                rohan,
                bundle["contest_id"],
                bundle["problem_id"],
                programs("accepted") + f"\n# {n}\n",
            )
        first = rohan.get(f"/contests/{bundle['contest_id']}/submissions?limit=2&offset=0").json()
        second = rohan.get(f"/contests/{bundle['contest_id']}/submissions?limit=2&offset=2").json()
        third = rohan.get(f"/contests/{bundle['contest_id']}/submissions?limit=2&offset=4").json()

        assert first["page"] == {"limit": 2, "offset": 0, "total": 5, "returned": 2}
        pages = [row["id"] for row in first["items"] + second["items"] + third["items"]]
        assert len(set(pages)) == 5, "pages must not overlap or skip"

        # Re-reading a page gives the same rows: the ordering is a total order.
        again = rohan.get(f"/contests/{bundle['contest_id']}/submissions?limit=2&offset=2").json()
        assert [row["id"] for row in again["items"]] == [row["id"] for row in second["items"]]

    def test_pagination_bounds_are_validated(self, fresh_contest):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        assert rohan.get(f"/contests/{bundle['contest_id']}/submissions?limit=0").status_code == 422
        assert rohan.get(f"/contests/{bundle['contest_id']}/submissions?limit=9999").status_code == 422
        assert rohan.get(f"/contests/{bundle['contest_id']}/submissions?offset=-1").status_code == 422

    def test_submission_filters_work_for_an_organizer(self, fresh_contest, programs):
        bundle = fresh_contest()
        rohan = bundle["participants"]["rohan@acme.test"]
        meera = bundle["participants"]["meera@acme.test"]
        submit(rohan, bundle["contest_id"], bundle["problem_id"], programs("accepted"))
        submit(meera, bundle["contest_id"], bundle["problem_id"], programs("accepted"))
        rohan_id = bundle["user_ids"]["rohan@acme.test"]
        filtered = bundle["organizer"].get(
            f"/contests/{bundle['contest_id']}/submissions?user_id={rohan_id}"
        ).json()
        assert filtered["page"]["total"] == 1
        assert filtered["items"][0]["user_id"] == rohan_id


class TestUnseenData:
    def test_a_brand_new_organization_can_run_a_contest_end_to_end(self, manifest, clients, programs):
        """Nothing in the system is specific to the seeded fixtures.

        A platform-admin credential creates an organization, an organizer and a participant
        that the code has never seen, and the whole flow works through the public contracts.
        """
        admin = clients(manifest["service_tokens"]["platform_admin"], "platform-admin")
        slug = f"unseen-{uuid.uuid4().hex[:8]}"
        org = admin.post("/orgs", json={"slug": slug, "name": "Unseen Institute"})
        assert org.status_code == 201, org.text
        org_id = org.json()["id"]

        organizer = admin.post(
            f"/orgs/{org_id}/users",
            json={"email": f"org-{slug}@unseen.test", "display_name": "New Organizer", "role": "ORGANIZER"},
        ).json()
        participant = admin.post(
            f"/orgs/{org_id}/users",
            json={"email": f"p-{slug}@unseen.test", "display_name": "New Participant", "role": "PARTICIPANT"},
        ).json()
        assert organizer["token"] and participant["token"]

        import datetime as dt

        organizer_client = clients(organizer["token"], "new organizer")
        participant_client = clients(participant["token"], "new participant")
        now = dt.datetime.now(dt.timezone.utc)
        contest = organizer_client.post(
            "/contests",
            json={
                "slug": "first-contest",
                "title": "First contest",
                "starts_at": (now - dt.timedelta(minutes=1)).isoformat(),
                "ends_at": (now + dt.timedelta(hours=2)).isoformat(),
            },
        )
        assert contest.status_code == 201, contest.text
        contest_id = contest.json()["id"]
        problem = organizer_client.post(
            f"/contests/{contest_id}/problems",
            json={
                "slug": "echo-sum",
                "title": "Echo the sum",
                "version": {
                    "statement": "Print a + b.",
                    "max_points": 50,
                    "test_suite": {
                        "label": "unseen",
                        "cases": [
                            {"ordinal": 1, "input": "7 8\n", "expected_output": "15\n", "is_sample": True},
                            {"ordinal": 2, "input": "1 1\n", "expected_output": "2\n"},
                        ],
                    },
                },
            },
        )
        assert problem.status_code == 201, problem.text
        assert participant_client.post(f"/contests/{contest_id}/participants/me").status_code == 201

        submission = submit(
            participant_client,
            contest_id,
            problem.json()["id"],
            programs("accepted"),
        ).json()
        judged = wait_for_terminal(participant_client, submission["id"])
        assert judged["authoritative_attempt"]["verdict"] == "ACCEPTED"
        assert judged["authoritative_attempt"]["score"] == 50

        board = organizer_client.get(f"/contests/{contest_id}/leaderboard").json()
        assert board["items"][0]["points"] == 50


class TestIdentityRoutes:
    def test_me_describes_each_credential_kind(self, manifest, clients, tokens):
        participant = clients(tokens["rohan@acme.test"], "rohan").get("/me").json()
        assert participant["scope"] == "user"
        assert participant["user"]["role"] == "PARTICIPANT"
        assert participant["user"]["email"] == "rohan@acme.test"

        judge = clients(manifest["service_tokens"]["judge"], "judge").get("/me").json()
        assert judge["scope"] == "judge" and judge["user"] is None

        admin = clients(manifest["service_tokens"]["platform_admin"], "admin").get("/me").json()
        assert admin["scope"] == "platform_admin" and admin["user"] is None

    def test_the_dev_token_fixture_mints_a_usable_token(self, clients, tokens, manifest):
        anonymous = clients(None, "anonymous")
        minted = anonymous.post("/auth/dev/token", json={"email": "meera@acme.test"})
        assert minted.status_code == 200, minted.text
        body = minted.json()
        assert body["user"]["email"] == "meera@acme.test"

        issued = clients(body["token"], "minted")
        assert issued.get("/me").json()["user"]["id"] == body["user"]["id"]
        assert issued.get("/contests").status_code == 200

        unknown = anonymous.post("/auth/dev/token", json={"email": "nobody@nowhere.test"})
        assert unknown.status_code == 404

    def test_an_organization_is_readable_only_by_its_own_members(self, manifest, clients, tokens):
        acme_id = manifest["organizations"]["acme-university"]["id"]
        borealis_id = manifest["organizations"]["borealis-institute"]["id"]
        rohan = clients(tokens["rohan@acme.test"], "rohan")
        assert rohan.get(f"/orgs/{acme_id}").json()["slug"] == "acme-university"
        assert rohan.get(f"/orgs/{borealis_id}").status_code == 404

    def test_an_organizer_can_create_an_identity_in_its_own_organization(self, manifest, clients, tokens):
        acme_id = manifest["organizations"]["acme-university"]["id"]
        organizer = clients(tokens["organizer@acme.test"], "organizer")
        created = organizer.post(
            f"/orgs/{acme_id}/users",
            json={
                "email": f"newcomer-{uuid.uuid4().hex[:8]}@acme.test",
                "display_name": "Newcomer",
                "role": "PARTICIPANT",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["org_id"] == acme_id
        assert clients(created.json()["token"], "newcomer").get("/me").json()["scope"] == "user"

        duplicate = organizer.post(
            f"/orgs/{acme_id}/users",
            json={"email": "rohan@acme.test", "display_name": "Impostor", "role": "PARTICIPANT"},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "duplicate_email"


def test_an_error_body_carries_the_generated_request_id(fresh_contest, clients, tokens):
    """A client that sends no X-Request-Id must still be able to correlate its own failure."""
    bundle = fresh_contest()
    rohan = clients(tokens["rohan@acme.test"], "rohan")
    response = rohan.get(f"/contests/{uuid.uuid4()}")
    assert response.status_code == 404
    generated = response.headers["X-Request-Id"]
    assert generated, "the response must always carry a request id"
    assert response.json()["error"]["request_id"] == generated, (
        "the error body and the response header must name the same request"
    )
