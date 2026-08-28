"""Unit tests for RadarAgent source-run instrumentation."""

import logging
from unittest.mock import Mock

from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.opportunities import PersistenceSummary
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.models.source_run import FAILED, SUCCESS, SourceRunAttempt
from services.collector.qualification.persistence import (
    PersistenceSummary as QualificationPersistenceSummary,
)
from services.collector.sources import SourceConfig


SETTINGS = Settings(ApplicationEnvironment.TEST, DatabaseBackend.SQLITE)


def source(source_id="source_a"):
    return SourceConfig(source_id, "greenhouse", True, "Org", "board", status="active")


def candidate(source_id="source_a"):
    return OpportunityCandidate(
        source_id, "1", "Role", "Org", "Morocco", "description", None,
        f"https://example.com/{source_id}", None, None,
    )


class RecordingRunRecorder:
    """Capture what RadarAgent would persist, without touching a database."""

    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def __call__(self, settings, source_config, attempt, *, status, metrics, error=None):
        self.calls.append(
            {
                "settings": settings, "source": source_config, "attempt": attempt,
                "status": status, "metrics": metrics, "error": error,
            }
        )
        if self._error is not None:
            raise self._error
        return None


def agent(*, sources, factory, persister, recorder, logger=None):
    return RadarAgent(
        source_loader=lambda: sources,
        collector_factory=factory,
        settings_loader=lambda: SETTINGS,
        persister=persister,
        qualification_persister=Mock(return_value=QualificationPersistenceSummary(0, 0, 0, 0)),
        run_recorder=recorder,
        logger=logger,
    )


def test_successful_source_records_started_finished_and_real_counts():
    recorder = RecordingRunRecorder()
    collector = Mock()
    collector.collect.return_value = [candidate(), candidate()]
    del collector.run_metrics

    summary = agent(
        sources=[source()], factory=lambda unused: collector,
        persister=Mock(return_value=PersistenceSummary(2, 0)), recorder=recorder,
    ).run_once()

    assert summary.sources_succeeded == 1
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["status"] == SUCCESS
    assert call["error"] is None
    assert call["settings"] is SETTINGS
    assert call["source"].id == "source_a"
    assert isinstance(call["attempt"], SourceRunAttempt)
    assert call["attempt"].source_id == "source_a"
    assert call["attempt"].started_at
    assert call["metrics"].items_found == 2
    assert call["metrics"].new_items == 2
    assert call["metrics"].pages_checked is None
    assert call["metrics"].relevant_items is None
    assert call["metrics"].http_status is None
    assert call["metrics"].parser_version is None


def test_collection_failure_records_a_failed_run_with_no_invented_counts():
    recorder = RecordingRunRecorder()
    collector = Mock()
    collector.collect.side_effect = RuntimeError("board unreachable")
    del collector.run_metrics

    summary = agent(
        sources=[source()], factory=lambda unused: collector,
        persister=Mock(), recorder=recorder,
    ).run_once()

    assert summary.sources_failed == 1
    call = recorder.calls[0]
    assert call["status"] == FAILED
    assert isinstance(call["error"], RuntimeError)
    assert call["metrics"].items_found is None
    assert call["metrics"].new_items is None


def test_persistence_failure_keeps_the_items_that_were_really_collected():
    recorder = RecordingRunRecorder()
    collector = Mock()
    collector.collect.return_value = [candidate(), candidate(), candidate()]
    del collector.run_metrics

    agent(
        sources=[source()], factory=lambda unused: collector,
        persister=Mock(side_effect=RuntimeError("database unavailable")), recorder=recorder,
    ).run_once()

    call = recorder.calls[0]
    assert call["status"] == FAILED
    assert call["metrics"].items_found == 3
    assert call["metrics"].new_items is None


def test_optional_collector_metrics_are_passed_through_without_overriding_real_counts():
    recorder = RecordingRunRecorder()

    class ReportingCollector:
        def collect(self):
            return [candidate()]

        def run_metrics(self):
            return {
                "pages_checked": 2, "http_status": 200,
                "parser_version": "greenhouse-v2", "items_found": 999,
            }

    agent(
        sources=[source()], factory=lambda unused: ReportingCollector(),
        persister=Mock(return_value=PersistenceSummary(1, 0)), recorder=recorder,
    ).run_once()

    metrics = recorder.calls[0]["metrics"]
    assert (metrics.pages_checked, metrics.http_status) == (2, 200)
    assert metrics.parser_version == "greenhouse-v2"
    assert metrics.items_found == 1
    assert metrics.new_items == 1


def test_a_failing_recorder_never_changes_source_outcomes_or_leaks_a_message(caplog):
    recorder = RecordingRunRecorder(error=RuntimeError("history unavailable token=hidden"))
    logger = logging.getLogger("test.radar.source_runs")

    def factory(config):
        collector = Mock()
        collector.collect.return_value = [candidate(config.id)]
        del collector.run_metrics
        return collector

    with caplog.at_level(logging.INFO, logger=logger.name):
        summary = agent(
            sources=[source("source_a"), source("source_b")], factory=factory,
            persister=Mock(return_value=PersistenceSummary(1, 0)), recorder=recorder,
            logger=logger,
        ).run_once()

    assert (summary.sources_total, summary.sources_succeeded, summary.sources_failed) == (2, 2, 0)
    assert summary.items_created == 2
    assert len(recorder.calls) == 2
    not_recorded = [
        record for record in caplog.records
        if record.event == "radar_source_run_not_recorded"
    ]
    assert [record.source_id for record in not_recorded] == ["source_a", "source_b"]
    assert all(record.error_type == "RuntimeError" for record in not_recorded)
    assert all(record.run_status == SUCCESS for record in not_recorded)
    assert "history unavailable" not in caplog.text
    assert "token=hidden" not in caplog.text


def test_every_source_is_instrumented_even_when_one_of_them_fails():
    recorder = RecordingRunRecorder()

    def factory(config):
        collector = Mock()
        if config.id == "source_b":
            collector.collect.side_effect = RuntimeError("collector down")
        else:
            collector.collect.return_value = [candidate(config.id)]
        del collector.run_metrics
        return collector

    summary = agent(
        sources=[source("source_a"), source("source_b"), source("source_c")],
        factory=factory, persister=Mock(return_value=PersistenceSummary(1, 0)),
        recorder=recorder,
    ).run_once()

    assert (summary.sources_succeeded, summary.sources_failed) == (2, 1)
    assert [(call["source"].id, call["status"]) for call in recorder.calls] == [
        ("source_a", SUCCESS), ("source_b", FAILED), ("source_c", SUCCESS),
    ]
    assert len({call["attempt"] for call in recorder.calls}) == 3


def test_a_collector_factory_failure_is_still_recorded_as_a_failed_run():
    recorder = RecordingRunRecorder()

    def factory(unused):
        raise ValueError("unsupported collector type")

    summary = agent(
        sources=[source()], factory=factory, persister=Mock(), recorder=recorder,
    ).run_once()

    assert summary.sources_failed == 1
    call = recorder.calls[0]
    assert call["status"] == FAILED
    assert isinstance(call["error"], ValueError)
    assert call["metrics"].items_found is None
