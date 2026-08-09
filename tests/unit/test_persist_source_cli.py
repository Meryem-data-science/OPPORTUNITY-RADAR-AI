"""Tests for explicit persistence CLI safeguards."""

from services.collector.cli import persist_source
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings


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


def test_turso_guard_refuses_connection(monkeypatch) -> None:
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

    def unexpected_connection(settings: Settings):
        nonlocal connection_attempted
        connection_attempted = True

    monkeypatch.delenv("RUN_TURSO_LIVE_PERSIST", raising=False)
    monkeypatch.setattr(
        persist_source, "get_enabled_source", lambda source_id: object()
    )
    monkeypatch.setattr(persist_source, "GreenhouseCollector", Collector)
    monkeypatch.setattr(persist_source, "load_settings", lambda: settings)
    monkeypatch.setattr(
        persist_source, "connect_configured_database", unexpected_connection
    )

    assert persist_source.main(["--source", "test", "--limit", "1", "--apply"]) == 1
    assert connection_attempted is False
