"""Opt-in smoke test for the real public Scale AI Greenhouse board."""

import os

import pytest

from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.sources import get_enabled_source


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SOURCE_TEST") != "1",
    reason="set RUN_LIVE_SOURCE_TEST=1 to query the real public source",
)


def test_scale_ai_greenhouse_live() -> None:
    candidates = GreenhouseCollector(
        get_enabled_source("scale_ai_greenhouse")
    ).collect()
    assert candidates, (
        "Scale AI Greenhouse returned zero jobs; verify the board manually"
    )
    example = candidates[0]
    assert example.source_url.startswith(("http://", "https://"))
    assert "localhost" not in example.source_url
    print(f"Live example: {example.canonical_title} — {example.source_url}")
