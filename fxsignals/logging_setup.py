"""Structured JSON-lines logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization|cookie|session)", re.I)
SECRET_VALUE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie)(\"?\s*[:=]\s*\"?)([^\s\",}]+)")


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return SECRET_VALUE.sub(r"\1\2***", obj)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = redact(extra)
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{datetime.now().strftime('%H:%M:%S')} {record.levelname:<7} {redact(record.getMessage())}"
        return base


def setup_logging(log_dir: str, level: str = "INFO", console: bool = True) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("fxsignals")
    root.setLevel(level.upper())
    root.handlers.clear()
    fh = RotatingFileHandler(Path(log_dir) / "fxsignals.jsonl", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(ConsoleFormatter())
        ch.setLevel(level.upper())
        root.addHandler(ch)
    root.propagate = False
    return root


def log(logger: logging.Logger, level: int, msg: str, **data) -> None:
    logger.log(level, msg, extra={"data": data} if data else None)
