"""Tests for explicit persistence CLI safeguards."""

from io import StringIO
import json
import logging

from services.collector.cli import persist_source
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.opportunities import PersistenceSummary
from services.collector import logging_config


class Logger:
    def info(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


def disable_logging(monkeypatch) -> None:
    monkeypatch.setattr(persist_source, "get_logger", lambda name: Logger())


def test_apply_is_required_before_collection(monkeypatch) -> None:
    disable_logging(monkeypatch)
    collected = False

    def unexpected_source(source_id: str):
        nonlocal collected
        collected = True

    monkeypatch.setattr(persist_source, "get_enabled_source", unexpected_source)
    assert persist_source.main(["--source", "test", "--limit", "1"]) == 1
    assert collected is False


def test_non_positive_limit_is_rejected() -> None:
    try:
        persist_source.parse_args(["--source", "test", "--limit", "0", "--apply"])
    except SystemExit as error:
        assert error.code != 0
    else:
        raise AssertionError("non-positive limit was accepted")


def test_turso_write_is_always_refused_before_persistence(monkeypatch) -> None:
    disable_logging(monkeypatch)
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.TURSO,
        turso_database_url="libsql://fixture.invalid",
        turso_auth_token="TEST_ONLY_SECRET",
    )
    connection_attempted = False

    class Collector:
        def __init__(self, source) -> None:
            pass

        def collect(self) -> list[object]:
            return []

    def unexpected_persistence(*args, **kwargs):
        nonlocal connection_attempted
        connection_attempted = True

    monkeypatch.setattr(
        persist_source, "get_enabled_source", lambda source_id: object()
    )
    monkeypatch.setattr(persist_source, "GreenhouseCollector", Collector)
    monkeypatch.setattr(persist_source, "load_settings", lambda: settings)
    monkeypatch.setattr(
        persist_source, "persist_configured_opportunities", unexpected_persistence
    )

    assert persist_source.main(["--source", "test", "--limit", "1", "--apply"]) == 1
    assert connection_attempted is False


def test_success_uses_real_structured_logger_without_reserved_fields(
    monkeypatch, tmp_path
) -> None:
    stream = StringIO()
    service_logger = logging.getLogger(logging_config.LOGGER_NAME)
    service_logger.handlers.clear()
    source = type("Source", (), {"id": "test_greenhouse"})()
    candidate = type(
        "Candidate",
        (),
        {"description": "TEST_ONLY_SECRET full description"},
    )()
    settings = Settings(
        environment=ApplicationEnvironment.TEST,
        database_backend=DatabaseBackend.SQLITE,
        sqlite_database_path=tmp_path / "test.db",
    )

    class Collector:
        def __init__(self, configured_source) -> None:
            assert configured_source is source

        def collect(self):
            return [candidate]

    monkeypatch.setattr(
        persist_source,
        "get_logger",
        lambda name: logging_config.get_logger(name, stream=stream),
    )
    monkeypatch.setattr(persist_source, "get_enabled_source", lambda source_id: source)
    monkeypatch.setattr(persist_source, "GreenhouseCollector", Collector)
    monkeypatch.setattr(persist_source, "load_settings", lambda: settings)
    monkeypatch.setattr(
        persist_source,
        "persist_configured_opportunities",
        lambda *args: PersistenceSummary(created=1, updated=0),
    )

    try:
        assert (
            persist_source.main(
                ["--source", "test_greenhouse", "--limit", "1", "--apply"]
            )
            == 0
        )
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        service_logger.handlers.clear()

    succeeded = next(
        record
        for record in records
        if record["event"] == "source_persistence_succeeded"
    )
    assert succeeded["context"]["items_created"] == 1
    assert succeeded["context"]["items_updated"] == 0
    assert "created" not in succeeded["context"]
    assert "updated" not in succeeded["context"]
    rendered = stream.getvalue()
    assert "full description" not in rendered
    assert "TEST_ONLY_SECRET" not in rendered
