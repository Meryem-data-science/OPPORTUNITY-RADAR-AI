"""Runtime configuration loaded explicitly from environment variables."""

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path
from typing import TypeVar


DEFAULT_SQLITE_DATABASE_PATH = Path("data/opportunity-radar.db")
EnumType = TypeVar("EnumType", bound=Enum)


class ConfigurationError(ValueError):
    """Raised when runtime configuration is missing or invalid."""


class ApplicationEnvironment(str, Enum):
    """Supported application runtime environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class DatabaseBackend(str, Enum):
    """Supported database backend selections."""

    SQLITE = "sqlite"
    TURSO = "turso"


@dataclass(frozen=True)
class Settings:
    """Validated and immutable runtime settings."""

    environment: ApplicationEnvironment
    database_backend: DatabaseBackend
    sqlite_database_path: Path | None = None
    turso_database_url: str | None = None
    turso_auth_token: str | None = field(default=None, repr=False)


def _read_enum(name: str, default: str, enum_type: type[EnumType]) -> EnumType:
    value = os.environ.get(name, default).strip()
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(member.value for member in enum_type)
        raise ConfigurationError(
            f"{name} must be one of: {allowed}; received {value!r}"
        ) from error


def _optional_environment_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_settings() -> Settings:
    """Read and validate the current process environment without side effects."""
    environment = _read_enum(
        "OPPORTUNITY_RADAR_ENV",
        ApplicationEnvironment.DEVELOPMENT.value,
        ApplicationEnvironment,
    )
    database_backend = _read_enum(
        "DATABASE_BACKEND",
        DatabaseBackend.SQLITE.value,
        DatabaseBackend,
    )

    if database_backend is DatabaseBackend.SQLITE:
        configured_path = _optional_environment_value("SQLITE_DATABASE_PATH")
        if configured_path is None:
            if environment is not ApplicationEnvironment.DEVELOPMENT:
                raise ConfigurationError(
                    "SQLITE_DATABASE_PATH is required for SQLite outside development"
                )
            sqlite_database_path = DEFAULT_SQLITE_DATABASE_PATH
        else:
            sqlite_database_path = Path(configured_path)
        return Settings(
            environment=environment,
            database_backend=database_backend,
            sqlite_database_path=sqlite_database_path,
        )

    turso_database_url = _optional_environment_value("TURSO_DATABASE_URL")
    turso_auth_token = _optional_environment_value("TURSO_AUTH_TOKEN")
    if turso_database_url is None:
        raise ConfigurationError(
            "TURSO_DATABASE_URL is required when DATABASE_BACKEND=turso"
        )
    if turso_auth_token is None:
        raise ConfigurationError(
            "TURSO_AUTH_TOKEN is required when DATABASE_BACKEND=turso"
        )
    return Settings(
        environment=environment,
        database_backend=database_backend,
        turso_database_url=turso_database_url,
        turso_auth_token=turso_auth_token,
    )
