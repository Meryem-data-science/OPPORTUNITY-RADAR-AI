"""Opt-in real Gmail -> LinkedIn parser -> RadarAgent -> isolated SQLite test."""

import os

import pytest

from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.sources import load_source_registry


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LINKEDIN_PIPELINE_TEST") != "1",
    reason="requires explicit real Gmail LinkedIn pipeline opt-in",
)


def test_real_linkedin_alert_pipeline_is_idempotent_in_isolated_sqlite(tmp_path):
    database = tmp_path / "live-linkedin-radar.db"
    connection = connect_database(database)
    try:
        apply_migrations(connection)
    finally:
        connection.close()

    settings = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE, database)
    agent = RadarAgent(
        source_loader=load_source_registry,
        settings_loader=lambda: settings,
        source_ids=["linkedin_job_alert_email"],
    )
    first = agent.run_once()
    assert first.sources_total == first.sources_succeeded == 1
    assert first.items_created > 0

    connection = connect_database(database)
    try:
        first_rows = connection.execute(
            "SELECT source_url, application_url, canonical_url FROM opportunities"
        ).fetchall()
    finally:
        connection.close()
    assert len(first_rows) == first.items_created
    for urls in first_rows:
        assert urls[0] == urls[1] == urls[2]
        assert urls[0].startswith("https://www.linkedin.com/jobs/view/")
        assert "?" not in urls[0]

    second = agent.run_once()
    assert second.items_created == 0
    assert second.items_updated > 0
    connection = connect_database(database)
    try:
        stable_count = connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    finally:
        connection.close()
    assert stable_count == len(first_rows)
