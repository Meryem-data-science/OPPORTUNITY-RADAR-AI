import logging
from unittest.mock import Mock

import pytest

from services.collector.agent import RadarAgent
from services.collector.collectors.factory import (
    UnsupportedCollectorTypeError,
    collector_for,
)
from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.opportunities import PersistenceSummary
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig, load_source_registry


def source(source_id="source_a", *, enabled=True, status="active", source_type="greenhouse"):
    return SourceConfig(source_id, source_type, enabled, "Org", "board", status=status)


def candidate(source_id="source_a", description="safe description"):
    return OpportunityCandidate(
        source_id, "1", "Role", "Org", "Morocco", description, None,
        f"https://example.com/{source_id}", f"https://example.com/{source_id}",
        f"https://example.com/{source_id}",
    )


SETTINGS = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE)


def test_factory_builds_greenhouse_for_every_configured_source_and_rejects_unsupported_type():
    configured_sources = load_source_registry()
    assert {item.id for item in configured_sources} == {
        "scale_ai_greenhouse",
        "artefact_greenhouse",
    }
    assert all(
        isinstance(collector_for(item), GreenhouseCollector)
        for item in configured_sources
    )
    with pytest.raises(UnsupportedCollectorTypeError, match="unsupported collector type"):
        collector_for(source(source_type="lever"))


def test_run_once_loads_filters_and_passes_candidates_to_persistence():
    loader = Mock(return_value=[source(), source("disabled", enabled=False), source("paused", status="paused")])
    collector = Mock()
    candidates = [candidate()]
    collector.collect.return_value = candidates
    factory = Mock(return_value=collector)
    persister = Mock(return_value=PersistenceSummary(1, 0))

    summary = RadarAgent(
        source_loader=loader, collector_factory=factory,
        settings_loader=lambda: SETTINGS, persister=persister,
    ).run_once()

    loader.assert_called_once_with()
    factory.assert_called_once()
    collector.collect.assert_called_once_with()
    persister.assert_called_once_with(SETTINGS, loader.return_value[0], candidates)
    assert (summary.sources_total, summary.sources_succeeded) == (1, 1)
    assert (summary.items_collected, summary.items_created, summary.items_updated) == (1, 1, 0)


def test_multiple_sources_aggregate_and_failure_does_not_retry_or_stop(caplog):
    sources = [source("source_a"), source("source_b")]
    first = Mock()
    first.collect.side_effect = RuntimeError(
        "secret description Authorization=Bearer-private TURSO_AUTH_TOKEN=hidden"
    )
    second_candidates = [candidate("source_b"), candidate("source_b")]
    second = Mock()
    second.collect.return_value = second_candidates
    factory = Mock(side_effect=[first, second])
    persister = Mock(return_value=PersistenceSummary(1, 1))
    logger = logging.getLogger("test.radar.agent")

    with caplog.at_level(logging.INFO, logger="test.radar.agent"):
        summary = RadarAgent(
            source_loader=lambda: sources, collector_factory=factory,
            settings_loader=lambda: SETTINGS, persister=persister,
            logger=logger,
        ).run_once()

    first.collect.assert_called_once_with()
    second.collect.assert_called_once_with()
    persister.assert_called_once_with(SETTINGS, sources[1], second_candidates)
    assert (summary.sources_total, summary.sources_succeeded, summary.sources_failed) == (2, 1, 1)
    assert (summary.items_collected, summary.items_created, summary.items_updated) == (2, 1, 1)
    assert summary.source_results[0].error_type == "RuntimeError"
    assert "secret description" not in caplog.text
    assert "Bearer-private" not in caplog.text
    failed = next(record for record in caplog.records if record.event == "radar_source_failed")
    assert failed.source_id == "source_a"
    assert failed.error_type == "RuntimeError"
    completed = next(record for record in caplog.records if record.event == "radar_run_completed")
    assert completed.items_created == 1
    assert completed.items_updated == 1


def test_persistence_failure_is_isolated_without_retry():
    sources = [source("source_a"), source("source_b")]
    collectors = []
    def factory(config):
        collector = Mock()
        collector.collect.return_value = [candidate(config.id)]
        collectors.append(collector)
        return collector
    persister = Mock(side_effect=[RuntimeError("db unavailable"), PersistenceSummary(0, 1)])
    summary = RadarAgent(
        source_loader=lambda: sources, collector_factory=factory,
        settings_loader=lambda: SETTINGS, persister=persister,
    ).run_once()
    assert persister.call_count == 2
    assert all(item.collect.call_count == 1 for item in collectors)
    assert summary.sources_failed == 1
    assert summary.items_collected == 2
