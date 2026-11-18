# Authorization rules

Authentication is a development fixture (static bearer tokens, stored as SHA-256 digests).
Authorization is not: it is enforced on every read and every write, and it is what the Stage 4
denial tests exercise (`tests/integration/test_authorization.py`, 20 cases).

## Three credential scopes

| Scope            | Who                              | Can reach                                           |
| ---------------- | -------------------------------- | --------------------------------------------------- |
| `user`           | participants and organizers      | the participant-facing API, inside **one** organization |
| `judge`          | the judge workers                | `/internal/judge/*` only                            |
| `platform_admin` | bootstrap                        | create organizations and identities                 |

The scopes do not overlap in either direction, and the tests assert both:

* a `judge` credential on `/contests/...` → `403`
* a `user` credential on `/internal/judge/...` → `403`
* an organizer creating an organization → `403`

A `judge` token cannot be minted for a human identity: the database enforces
`kind = 'SERVICE' AND user_id IS NULL` for service tokens.

## Two roles inside an organization

| Capability                                               | Organizer | Participant |
| -------------------------------------------------------- | :-------: | :---------: |
| create and amend contests                                | yes       | no          |
| create problems, versions, test suites                   | yes       | no          |
| list test suites (sizes only; contents are never served) | yes       | no          |
| list participants                                        | yes       | no          |
| read any submission in the organization, with its source | yes       | own only    |
| read judge detail (runtime config, worker, lease epoch)  | yes       | no          |
| rejudge                                                  | yes       | no          |
| rebuild / verify / explain standings                     | yes       | no          |
| read queue depth, arm failpoints                         | yes       | no          |
| join a contest and submit                                | no        | yes         |
| read the leaderboard                                     | yes       | yes         |

An organizer cannot submit. Not a privilege question: a scoreboard that can contain its own
author is a worse scoreboard.

## Tenancy: 404, not 403

Every lookup goes through `api/app/services/access.py`, so a route cannot forget to scope a
read. The rule:

* **outside the caller's organization → `404 not_found`.** Knowing an identifier must not
  confirm that it exists. A `403` would answer the question "does this contest exist?", which
  is exactly the question a cross-tenant caller should not be able to ask.
* **inside the caller's organization, wrong role → `403 forbidden`.** The object is
  legitimately visible to this tenant, so the honest answer is that this caller may not do it.

The same rule covers another participant's work: in the same contest, another participant's
submission answers `404`, not `403`. Private code is not something whose existence is
confirmable. Filters are ignored rather than honoured, so
`GET /contests/{id}/submissions?user_id=<someone-else>` returns the caller's own rows, not an
error and never someone else's rows.

## Hidden data

Hidden test inputs and expected outputs leave the database on exactly one route:
`GET /internal/judge/attempts/{id}/payload`, which requires the `judge` scope.

* Participants get per-test *status and resource usage*, never the input, the expected output
  or the program's own output.
* Sample cases are published; non-sample cases are not, at any point in the contest.
* `GET /problems/{id}/test-suites` is organizer-only, and returns suite versions and case
  **counts**, never case contents.
* Compile output is returned to the author of the submission, because it is their compiler
  talking about their file.
* Structured logs drop a fixed set of field names defensively
  (`authorization`, `token`, `password`, `source_code`, `expected_output`, `input_data`), so an
  added log line cannot leak a credential or a test case by accident.
* Inside the sandbox, only the *inputs* are delivered. Expected outputs never enter the
  container that runs submitted code; comparison happens in the worker.

## Request validation and error shape

Every failure has the same shape and a machine-readable `type`:

```json
{"error": {"type": "...", "code": "...", "message": "...", "details": null, "request_id": "..."}}
```

| `type`           | Status | Meaning                                                 |
| ---------------- | ------ | ------------------------------------------------------- |
| `validation`     | 422    | the request was malformed (shape, type, range)           |
| `business_rule`  | 409    | well formed, but a contest rule rejects it               |
| `authentication` | 401    | no usable credential                                     |
| `authorization`  | 403    | credential understood, action not allowed                |
| `not_found`      | 404    | no such object *for this caller*                         |
| `infrastructure` | 503    | our side failed; retrying is reasonable                  |

Keeping these apart is a deliberate requirement: a client has to be able to tell "you sent
nonsense" from "the contest says no" from "we broke, try again".
