"""Opt-in application of shared migrations to the real Turso database."""

import os

import pytest

from services.collector.config import load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.migrations import apply_migrations
from services.collector.database.schema import check_foundation_schema


LIVE_REQUIREMENTS = (
    os.environ.get("RUN_TURSO_LIVE_MIGRATION_TEST") == "1"
    and os.environ.get("DATABASE_BACKEND") == "turso"
    and bool(os.environ.get("TURSO_DATABASE_URL", "").strip())
    and bool(os.environ.get("TURSO_AUTH_TOKEN", "").strip())
)

pytestmark = pytest.mark.skipif(
    not LIVE_REQUIREMENTS,
    reason="Turso migration test requires explicit opt-in and real credentials",
)


def test_live_turso_migrations_are_idempotent() -> None:
    settings = load_settings()
    connection = connect_configured_database(settings)
    try:
        first_run = apply_migrations(connection)
        assert check_foundation_schema(connection) is True
        assert apply_migrations(connection) == []
        assert first_run in ([], ["0001"])
    finally:
        connection.close()
