"""Structured JSON logging for the API, worker and scheduler. Secrets never appear: only fields
explicitly passed via `extra` are emitted, and credential-like values are masked."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

_STD = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_SECRET = re.compile(r"(sk-ant-[\w-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+|postgres(?:ql)?://\S+)")


def _mask(v: object) -> object:
    return _SECRET.sub("[REDACTED]", v) if isinstance(v, str) else v


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(), "level": record.levelname,
               "logger": record.name, "msg": _mask(record.getMessage())}
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                out[k] = _mask(v)
        if record.exc_info and record.levelno >= logging.ERROR:
            out["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None   # no traceback text
        return json.dumps(out, default=str)


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
