"""Structured JSON logging for Opportunity Radar services."""

from datetime import datetime, timezone
import json
import logging
import re
import sys
from typing import TextIO


LOGGER_NAME = "services.collector"
_HANDLER_MARKER = "opportunity_radar_json"
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:token|access_token|refresh_token|password|secret|client_secret|"
    r"authorization|api_key|database_url)(?:$|_)",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(
    r"authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
    re.IGNORECASE,
)
_KEY_VALUE_SECRET = re.compile(
    r"\b(access_token|refresh_token|token|password|secret|client_secret|api_key|"
    r"database_url)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "source_id",
    "event",
    "opportunity_id",
}


def redact_text(value: object) -> str:
    """Redact common credential patterns from human-readable text."""
    text = str(value)
    text = _AUTHORIZATION.sub("Authorization: [REDACTED]", text)
    return _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)


def _sanitize_value(key: str, value: object) -> object:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_value(str(item_key), item)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(key, item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


class JsonFormatter(logging.Formatter):
    """Format standard log records as one sanitized JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "module": record.name,
            "source_id": _sanitize_value("source_id", getattr(record, "source_id", None)),
            "event": _sanitize_value("event", getattr(record, "event", None)),
            "opportunity_id": _sanitize_value(
                "opportunity_id", getattr(record, "opportunity_id", None)
            ),
            "message": redact_text(record.getMessage()),
        }
        context = {
            key: _sanitize_value(key, value)
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS
        }
        if context:
            payload["context"] = context
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(stream: TextIO | None = None) -> logging.Logger:
    """Configure the service logger once and return it."""
    logger = logging.getLogger(LOGGER_NAME)
    target_stream = stream if stream is not None else sys.stdout
    marked_handlers = [
        existing
        for existing in logger.handlers
        if getattr(existing, "name", None) == _HANDLER_MARKER
    ]
    handler = marked_handlers[0] if marked_handlers else None
    for duplicate in marked_handlers[1:]:
        logger.removeHandler(duplicate)
        duplicate.close()

    if handler is not None and getattr(handler.stream, "closed", False):
        # StreamHandler.setStream() flushes the previous stream first. Replace a
        # handler whose captured pytest/stdout stream is already closed instead.
        logger.removeHandler(handler)
        handler.close()
        handler = None
    if handler is None:
        handler = logging.StreamHandler(target_stream)
        handler.name = _HANDLER_MARKER
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    elif handler.stream is not target_stream:
        handler.setStream(target_stream)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def get_logger(module: str, stream: TextIO | None = None) -> logging.Logger:
    """Return a child logger that uses the shared structured handler."""
    configure_logging(stream)
    return logging.getLogger(module)
