"""HTTP client for the judge plane.

The worker holds a service credential with the `judge` scope and talks to the same API as
everyone else, over a separate set of routes. It has no database credential at all, which is
what makes the judge boundary a deployment fact rather than a convention.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class StaleLease(RuntimeError):
    """The API rejected this delivery: another one has superseded it."""


class JudgeApiUnavailable(RuntimeError):
    pass


class JudgeClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 20.0, worker_id: str = "worker"):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "User-Agent": f"shodh-judge/{worker_id}"},
            timeout=timeout,
        )
        self.worker_id = worker_id

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict, *, attempts: int = 3, request_id: str | None = None) -> dict:
        headers = {"X-Request-Id": request_id} if request_id else None
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(path, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last = exc
                time.sleep(min(2.0, 0.2 * attempt))
                continue
            if response.status_code == 409:
                raise StaleLease(response.text[:400])
            if response.status_code >= 500:
                last = JudgeApiUnavailable(f"{response.status_code}: {response.text[:200]}")
                time.sleep(min(2.0, 0.2 * attempt))
                continue
            if response.status_code >= 400:
                # A 4xx other than 409 means this worker sent something wrong; do not retry.
                raise JudgeApiUnavailable(f"{response.status_code}: {response.text[:400]}")
            return response.json() if response.content else {}
        raise JudgeApiUnavailable(str(last))

    def lease(self, capacity: int) -> list[dict]:
        body = self._post("/internal/judge/leases", {"worker_id": self.worker_id, "capacity": capacity})
        return body.get("leases", [])

    def heartbeat(self, attempt_id: str, lease_epoch: int) -> None:
        self._post(f"/internal/judge/attempts/{attempt_id}/heartbeat", {"lease_epoch": lease_epoch}, attempts=1)

    def report_result(self, attempt_id: str, payload: dict, *, request_id: str | None = None) -> dict:
        return self._post(
            f"/internal/judge/attempts/{attempt_id}/result", payload, request_id=request_id
        )

    def report_infra_failure(
        self, attempt_id: str, lease_epoch: int, reason: str, *, retryable: bool = True,
        request_id: str | None = None,
    ) -> dict:
        return self._post(
            f"/internal/judge/attempts/{attempt_id}/infra-failure",
            {
                "lease_epoch": lease_epoch,
                "worker_id": self.worker_id,
                "reason": reason[:2000],
                "retryable": retryable,
            },
            request_id=request_id,
        )

    def wait_until_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self._client.get("/readyz")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
        raise JudgeApiUnavailable("API did not become ready")
