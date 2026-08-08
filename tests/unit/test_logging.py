"""Tests for structured and sanitized service logging."""

from datetime import datetime, timezone
from io import StringIO
import json

import pytest

from services.collector import main as collector_main
from services.collector.logging_config import configure_logging, get_logger


def _emit(message: str, **extra: object) -> dict[str, object]:
    stream = StringIO()
    logger = get_logger("services.collector.test", stream)
    logger.info(message, extra={"event": "test_event", **extra})
    return json.loads(stream.getvalue())


def test_log_is_valid_json_with_required_fields_and_utc_timestamp() -> None:
    payload = _emit("Test message")

    assert set(payload) >= {
        "timestamp",
        "level",
        "module",
        "source_id",
        "event",
        "opportunity_id",
        "message",
    }
    assert payload["level"] == "INFO"
    assert payload["source_id"] is None
    assert payload["opportunity_id"] is None
    timestamp = datetime.fromisoformat(str(payload["timestamp"]))
    assert timestamp.utcoffset() == timezone.utc.utcoffset(timestamp)


def test_log_includes_optional_context() -> None:
    payload = _emit(
        "Context message",
        source_id="fixture_source",
        opportunity_id="fixture-opportunity",
    )

    assert payload["source_id"] == "fixture_source"
    assert payload["opportunity_id"] == "fixture-opportunity"


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("token=SUPER_SECRET_TEST_VALUE", "SUPER_SECRET_TEST_VALUE"),
        ("password=PASSWORD_TEST_VALUE", "PASSWORD_TEST_VALUE"),
        (
            "Authorization: Bearer TEST_SECRET_VALUE",
            "TEST_SECRET_VALUE",
        ),
    ],
)
def test_message_secrets_are_redacted(message: str, secret: str) -> None:
    output = json.dumps(_emit(message))

    assert secret not in output
    assert "[REDACTED]" in output


def test_sensitive_structured_fields_are_redacted() -> None:
    payload = _emit("Structured context", api_key="API_KEY_TEST_VALUE")

    assert payload["context"] == {"api_key": "[REDACTED]"}
    assert "API_KEY_TEST_VALUE" not in json.dumps(payload)


def test_repeated_configuration_does_not_duplicate_handlers() -> None:
    stream = StringIO()
    configure_logging(stream)
    configure_logging(stream)
    logger = get_logger("services.collector.test", stream)

    logger.info("Once", extra={"event": "single_event"})

    assert len(stream.getvalue().splitlines()) == 1


def test_main_emits_service_initialized_json(capsys) -> None:
    collector_main.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "service_initialized"
    assert payload["level"] == "INFO"
