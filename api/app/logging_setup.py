"""Structured JSON logging with request / submission / attempt correlation ids.

Every log line carries request_id so one operation can be followed across the API and the
worker; the worker re-uses the request_id that created the submission.
"""
from __future__ import annotations

import contextvars
import logging
import sys
from typing import Any

from pythonjsonlogger import jsonlogger

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_ctx", default={})

REDACT_KEYS = {"authorization", "token", "password", "source_code", "expected_output", "input_data"}


def bind(**kwargs: Any) -> None:
    cur = dict(_ctx.get())
    cur.update({k: v for k, v in kwargs.items() if v is not None})
    _ctx.set(cur)


def clear() -> None:
    _ctx.set({})


def context() -> dict[str, Any]:
    return dict(_ctx.get())


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _ctx.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class SafeJsonFormatter(jsonlogger.JsonFormatter):
    """Drops credential-ish and hidden-test-ish fields defensively."""

    def add_fields(self, log_record, record, message_dict):  # type: ignore[no-untyped-def]
        super().add_fields(log_record, record, message_dict)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        for key in list(log_record):
            if key.lower() in REDACT_KEYS:
                log_record[key] = "[redacted]"


def setup_logging(level: str = "INFO", service: str = "api") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SafeJsonFormatter("%(asctime)s %(level)s %(name)s %(message)s", rename_fields={"asctime": "ts"})
    )
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(noisy)
        lg.handlers = [handler]
        lg.propagate = False
    logging.getLogger(__name__).info("logging configured", extra={"service": service})


def current_request_id() -> str | None:
    value = _ctx.get().get("request_id")
    return str(value) if value else None
