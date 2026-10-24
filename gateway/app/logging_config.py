"""Process logging. ``LOG_FORMAT=text`` is the default; ``json`` is one object per line."""

from __future__ import annotations

import json
import logging

from .config import normalize_log_format
from .observability import RequestIdFilter, current_request_id


_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
    "request_id",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line. The message is the formatted record, not the raw args."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id() or "-"
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": record.request_id,
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
        return json.dumps(payload, ensure_ascii=False)


def build_formatter(log_format: str) -> logging.Formatter:
    normalized = normalize_log_format(log_format)
    if normalized == "json":
        return JsonFormatter()
    return logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s request_id=%(request_id)s"
    )


def configure_logging(level: str, log_format: str = "text") -> None:
    root = logging.getLogger()
    resolved = getattr(logging, str(level).upper(), None)
    root.setLevel(resolved if isinstance(resolved, int) else logging.INFO)
    formatter = build_formatter(log_format)
    handler = next((item for item in root.handlers if getattr(item, "_qingyu_handler", False)), None)
    if handler is None:
        handler = logging.StreamHandler()
        handler._qingyu_handler = True  # type: ignore[attr-defined]
        handler.addFilter(RequestIdFilter())
        root.addHandler(handler)
    handler.setFormatter(formatter)
