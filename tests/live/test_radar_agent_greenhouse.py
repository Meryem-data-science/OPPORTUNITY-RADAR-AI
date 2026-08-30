"""Opt-in end-to-end live Greenhouse radar validation."""

import os

import pytest

from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_RADAR_AGENT_TEST") != "1",
    reason="set RUN_LIVE_RADAR_AGENT_TEST=1 to run the live radar test",
)


def test_live_greenhouse_agent_is_idempotent(tmp_path):
    path = tmp_path / "live-radar.db"
    connection = connect_database(path)
    try:
        assert apply_migrations(connection) == [
            "0001",
            "0002",
            "0003",
            "0004",
            "0005",
            "0006",
            "0007",
            "0008",
            "0009",
            "0010",
            "0011",
        ]
    finally:
        connection.close()
    settings = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, path)
    agent = RadarAgent(settings_loader=lambda: settings)
    first = agent.run_once()
    assert first.sources_total >= 1
    assert first.sources_failed == 0
    assert first.items_collected > 0
    assert first.items_created > 0
    connection = connect_database(path)
    try:
        before = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        urls = [row[0] for row in connection.execute(
            "SELECT source_url FROM opportunity_sources"
        ).fetchall()]
    finally:
        connection.close()
    assert before > 0
    assert all(url.startswith("https://job-boards.greenhouse.io/") for url in urls)
    second = agent.run_once()
    connection = connect_database(path)
    try:
        after = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        connection.close()
    assert second.sources_failed == 0
    assert second.items_created == 0
    assert second.items_updated == second.items_collected
    assert after == before
