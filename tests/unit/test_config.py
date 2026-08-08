"""Tests for explicit runtime configuration loading."""

from pathlib import Path
import logging

import pytest

from services.collector import main as collector_main
from services.collector.config import (
    ApplicationEnvironment,
    ConfigurationError,
    DatabaseBackend,
    load_settings,
)


CONFIGURATION_VARIABLES = (
    "OPPORTUNITY_RADAR_ENV",
    "DATABASE_BACKEND",
    "SQLITE_DATABASE_PATH",
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_configuration_environment(monkeypatch) -> None:
    for name in CONFIGURATION_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_default_settings_use_development_sqlite_without_creating_database(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings()

    assert settings.environment is ApplicationEnvironment.DEVELOPMENT
    assert settings.database_backend is DatabaseBackend.SQLITE
    assert settings.sqlite_database_path == Path("data/opportunity-radar.db")
    assert not settings.sqlite_database_path.exists()


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_supported_environments(monkeypatch, tmp_path, environment: str) -> None:
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", environment)
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(tmp_path / "runtime.db"))

    assert load_settings().environment.value == environment


def test_invalid_environment_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "invalid")

    with pytest.raises(ConfigurationError, match="OPPORTUNITY_RADAR_ENV"):
        load_settings()


def test_sqlite_backend_accepts_explicit_test_path(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "runtime.db"
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(database_path))

    settings = load_settings()

    assert settings.database_backend is DatabaseBackend.SQLITE
    assert settings.sqlite_database_path == database_path
    assert not database_path.exists()


def test_turso_backend_accepts_complete_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://fixture.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST_ONLY_SECRET_TOKEN")

    settings = load_settings()

    assert settings.database_backend is DatabaseBackend.TURSO
    assert settings.turso_database_url == "libsql://fixture.invalid"
    assert settings.turso_auth_token == "TEST_ONLY_SECRET_TOKEN"


def test_invalid_database_backend_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "random")

    with pytest.raises(ConfigurationError, match="DATABASE_BACKEND"):
        load_settings()


def test_turso_requires_database_url_without_exposing_token(monkeypatch) -> None:
    token = "TEST_ONLY_TOKEN"
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", token)

    with pytest.raises(ConfigurationError) as error:
        load_settings()

    assert "TURSO_DATABASE_URL" in str(error.value)
    assert token not in str(error.value)


def test_turso_rejects_blank_database_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "   ")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST_ONLY_TOKEN")

    with pytest.raises(ConfigurationError, match="TURSO_DATABASE_URL"):
        load_settings()


def test_turso_requires_auth_token(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://fixture.invalid")

    with pytest.raises(ConfigurationError, match="TURSO_AUTH_TOKEN"):
        load_settings()


def test_turso_token_is_absent_from_settings_repr(monkeypatch) -> None:
    token = "TEST_ONLY_SECRET_TOKEN"
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://fixture.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", token)

    settings = load_settings()

    assert token not in repr(settings)


def test_load_settings_reads_current_environment_each_time(monkeypatch, tmp_path) -> None:
    settings_a = load_settings()
    test_database = tmp_path / "runtime.db"
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(test_database))

    settings_b = load_settings()

    assert settings_a.environment is ApplicationEnvironment.DEVELOPMENT
    assert settings_b.environment is ApplicationEnvironment.TEST
    assert settings_b.sqlite_database_path == test_database


def test_main_loads_turso_settings_without_exposing_token(monkeypatch, capsys) -> None:
    token = "TEST_ONLY_SECRET_TOKEN"
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://fixture.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", token)

    collector_main.main()

    output = capsys.readouterr().out
    assert '"event": "service_initialized"' in output
    assert token not in output

    service_logger = logging.getLogger("services.collector")
    for handler in service_logger.handlers[:]:
        service_logger.removeHandler(handler)
        handler.close()
