"""Explicit opt-in smoke test against low-volume public ReKrute pages."""

import os

import pytest

from services.collector.collectors.rekrute import ReKruteCollector, ReKruteHttpClient, RobotsDeniedError


@pytest.mark.skipif(os.environ.get("RUN_LIVE_REKRUTE_TEST") != "1", reason="set RUN_LIVE_REKRUTE_TEST=1 for the real public ReKrute smoke test")
def test_live_rekrute_public_produces_an_active_real_candidate() -> None:
    client = ReKruteHttpClient()
    try:
        try:
            result = ReKruteCollector(client, pause_seconds=0.5).collect("data-engineer", limit=2)
        except RobotsDeniedError as error:
            pytest.skip(f"ReKrute robots preflight did not authorize collection: {error}")
    finally:
        client.close()
    assert result.candidates, "no active parseable real offer found for data-engineer"
    candidate = result.candidates[0]
    assert candidate.source_id == "rekrute_public"
    assert candidate.source_external_id.isdigit()
    assert candidate.canonical_title
    assert candidate.organization
    assert candidate.description
    assert candidate.source_url.startswith("https://www.rekrute.com/")
    assert candidate.canonical_url.startswith("https://www.rekrute.com/")
