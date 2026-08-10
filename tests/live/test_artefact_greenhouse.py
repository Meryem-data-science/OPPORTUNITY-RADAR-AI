"""Opt-in smoke test for the real public Artefact Greenhouse board."""

import os
from urllib.parse import urlparse

import pytest

from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.sources import get_enabled_source


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SOURCE_TEST") != "1",
    reason="set RUN_LIVE_SOURCE_TEST=1 to query the real public source",
)


def test_artefact_greenhouse_live() -> None:
    candidates = GreenhouseCollector(
        get_enabled_source("artefact_greenhouse")
    ).collect()
    assert candidates, "Artefact Greenhouse returned zero jobs; verify the board manually"

    example = candidates[0]
    parsed_url = urlparse(example.source_url)
    assert example.source_id == "artefact_greenhouse"
    assert example.organization == "Artefact"
    assert example.canonical_title.strip()
    assert parsed_url.scheme in {"http", "https"}
    assert parsed_url.netloc
    assert parsed_url.hostname != "localhost"
    assert example.description is not None
    assert example.description.strip()
    assert example.application_url == example.source_url
    assert example.canonical_url == example.source_url
    print(
        f"Live Artefact example: {example.canonical_title} — "
        f"{example.location} — {example.source_url}"
    )
