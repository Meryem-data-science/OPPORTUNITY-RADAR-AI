"""Opt-in live connectivity test for a real Turso database."""

import os

import pytest

from services.collector.config import load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.health import check_database_health


LIVE_REQUIREMENTS = (
    os.environ.get("RUN_TURSO_LIVE_TEST") == "1"
    and os.environ.get("DATABASE_BACKEND") == "turso"
    and bool(os.environ.get("TURSO_DATABASE_URL", "").strip())
    and bool(os.environ.get("TURSO_AUTH_TOKEN", "").strip())
)

pytestmark = pytest.mark.skipif(
    not LIVE_REQUIREMENTS,
    reason="Turso live test requires explicit opt-in and real credentials",
)


def test_live_turso_select_one() -> None:
    settings = load_settings()
    connection = connect_configured_database(settings)
    try:
        assert check_database_health(connection) is True
    finally:
        connection.close()
