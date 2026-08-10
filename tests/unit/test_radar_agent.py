import logging
from unittest.mock import Mock

import pytest

from services.collector.agent import RadarAgent
from services.collector.collectors.factory import (
    UnsupportedCollectorTypeError,
    collector_for,
)
from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.collectors.linkedin_job_alert import LinkedInJobAlertCollector
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
        "linkedin_job_alert_email",
    }
    assert all(
        isinstance(collector_for(item), GreenhouseCollector)
        for item in configured_sources if item.type == "greenhouse"
    )
    assert isinstance(collector_for(configured_sources[2]), LinkedInJobAlertCollector)
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


def test_requested_sources_are_filtered_and_cannot_bypass_availability():
    sources = [source("a"), source("b"), source("off", enabled=False)]
    collector = Mock()
    collector.collect.return_value = []
    factory = Mock(return_value=collector)
    agent = RadarAgent(
        source_loader=lambda: sources, collector_factory=factory,
        settings_loader=lambda: SETTINGS, persister=Mock(return_value=PersistenceSummary(0, 0)),
        source_ids=["b"],
    )
    assert agent.run_once().sources_total == 1
    factory.assert_called_once_with(sources[1])
    for selected, message in ((["missing"], "unknown"), (["off"], "disabled or inactive")):
        with pytest.raises(ValueError, match=message):
            RadarAgent(source_loader=lambda: sources, source_ids=selected).run_once()


def test_three_sources_succeed_and_linkedin_failure_is_isolated():
    sources = [source("greenhouse_one"), source("greenhouse_two"), source("linkedin_job_alert_email")]
    persister = Mock(return_value=PersistenceSummary(1, 0))

    def successful_factory(config):
        collector = Mock()
        collector.collect.return_value = [candidate(config.id)]
        return collector

    successful = RadarAgent(
        source_loader=lambda: sources, collector_factory=successful_factory,
        settings_loader=lambda: SETTINGS, persister=persister,
    ).run_once()
    assert (successful.sources_total, successful.sources_succeeded, successful.sources_failed) == (3, 3, 0)
    assert persister.call_count == 3
    assert persister.call_args_list[-1].args[1] is sources[-1]
    persisted_linkedin_candidate = persister.call_args_list[-1].args[2][0]
    assert isinstance(persisted_linkedin_candidate, OpportunityCandidate)
    assert not hasattr(persisted_linkedin_candidate, "body_html")

    def failing_linkedin_factory(config):
        collector = Mock()
        if config.id == "linkedin_job_alert_email":
            collector.collect.side_effect = RuntimeError("Gmail failed")
        else:
            collector.collect.return_value = [candidate(config.id)]
        return collector

    isolated_persister = Mock(return_value=PersistenceSummary(1, 0))
    isolated = RadarAgent(
        source_loader=lambda: sources, collector_factory=failing_linkedin_factory,
        settings_loader=lambda: SETTINGS, persister=isolated_persister,
    ).run_once()
    assert (isolated.sources_total, isolated.sources_succeeded, isolated.sources_failed) == (3, 2, 1)
    assert isolated.source_results[-1].source_id == "linkedin_job_alert_email"
    assert isolated.source_results[-1].error_type == "RuntimeError"
    assert isolated_persister.call_count == 2
