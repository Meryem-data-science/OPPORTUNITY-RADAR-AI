"""Opt-in persistence of one real Greenhouse opportunity to real Turso."""

import os

import pytest

from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.config import load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.database.opportunities import persist_opportunities
from services.collector.sources import get_enabled_source

LIVE_REQUIREMENTS = (
    os.environ.get("RUN_TURSO_LIVE_PERSIST_TEST") == "1"
    and os.environ.get("DATABASE_BACKEND") == "turso"
    and bool(os.environ.get("TURSO_DATABASE_URL", "").strip())
    and bool(os.environ.get("TURSO_AUTH_TOKEN", "").strip())
    and os.environ.get("RUN_TURSO_LIVE_PERSIST") == "1"
)

pytestmark = pytest.mark.skipif(
    not LIVE_REQUIREMENTS,
    reason="Turso opportunity persistence requires all explicit opt-ins and credentials",
)


def test_real_greenhouse_candidate_is_idempotently_persisted_to_turso() -> None:
    source = get_enabled_source("scale_ai_greenhouse")
    candidate = GreenhouseCollector(source).collect()[0]
    assert candidate.description is not None
    connection = connect_configured_database(load_settings())
    try:
        persist_opportunities(connection, source, [candidate])
        stored = connection.execute(
            """
            SELECT o.canonical_title, os.source_url, o.description,
                   length(o.description)
            FROM opportunity_sources os
            JOIN opportunities o ON o.id = os.opportunity_id
            WHERE os.source_id = ? AND os.source_url = ?
            """,
            (source.id, candidate.source_url),
        ).fetchall()
        persist_opportunities(connection, source, [candidate])
        occurrence_count = connection.execute(
            "SELECT COUNT(*) FROM opportunity_sources WHERE source_id = ? AND source_url = ?",
            (source.id, candidate.source_url),
        ).fetchone()[0]
    finally:
        connection.close()

    assert stored == [
        (
            candidate.canonical_title,
            candidate.source_url,
            candidate.description,
            len(candidate.description),
        )
    ]
    assert occurrence_count == 1
