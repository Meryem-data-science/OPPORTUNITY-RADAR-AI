"""Unit tests for RadarAgent source-run instrumentation."""

from itertools import count
import logging
from unittest.mock import Mock

from services.collector.agent import RadarAgent
from services.collector.config import ApplicationEnvironment, DatabaseBackend, Settings
from services.collector.database.opportunities import PersistenceSummary
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.models.source_run import (
    FAILED,
    SUCCESS,
    SourceRunAttempt,
)
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


class RecordingRunLifecycle:
    """Record the run lifecycle RadarAgent drives, with no database involved."""

    def __init__(self, *, start_error=None, finalize_error=None):
        self.started = []
        self.finalized = []
        self._start_error = start_error
        self._finalize_error = finalize_error
        self._ids = count(1)

    def start(self, settings, source_config):
        if self._start_error is not None:
            raise self._start_error
        attempt = SourceRunAttempt(
            next(self._ids), source_config.id, "2026-01-01T00:00:00.000000+00:00"
        )
        self.started.append({"settings": settings, "source": source_config, "attempt": attempt})
        return attempt

    def finalize(self, settings, attempt, *, status, metrics, error=None):
        self.finalized.append(
            {"settings": settings, "attempt": attempt, "status": status,
             "metrics": metrics, "error": error}
        )
        if self._finalize_error is not None:
            raise self._finalize_error
        return None


def agent(*, sources, factory, persister, lifecycle, logger=None):
    return RadarAgent(
        source_loader=lambda: sources,
        collector_factory=factory,
        settings_loader=lambda: SETTINGS,
        persister=persister,
        qualification_persister=Mock(return_value=QualificationPersistenceSummary(0, 0, 0, 0)),
        run_starter=lifecycle.start,
        run_finalizer=lifecycle.finalize,
        logger=logger,
    )


def collector_returning(*candidates):
    collector = Mock()
    collector.collect.return_value = list(candidates)
    del collector.run_metrics
    return collector


def test_a_run_is_started_before_the_collector_is_ever_built():
    lifecycle = RecordingRunLifecycle()
    events = []

    def factory(config):
        events.append("build")
        collector = Mock()
        collector.collect.side_effect = lambda: events.append("collect") or [candidate()]
        del collector.run_metrics
        return collector

    def start(settings, config):
        events.append("start_run")
        return lifecycle.start(settings, config)

    def finalize(settings, attempt, *, status, metrics, error=None):
        events.append("finalize_run")
        return lifecycle.finalize(settings, attempt, status=status, metrics=metrics, error=error)

    RadarAgent(
        source_loader=lambda: [source()], collector_factory=factory,
        settings_loader=lambda: SETTINGS,
        persister=Mock(side_effect=lambda *unused: events.append("persist") or PersistenceSummary(1, 0)),
        qualification_persister=Mock(return_value=QualificationPersistenceSummary(0, 0, 0, 0)),
        run_starter=start, run_finalizer=finalize,
    ).run_once()

    assert events == ["start_run", "build", "collect", "persist", "finalize_run"]


def test_successful_source_finalizes_its_own_attempt_with_real_counts():
    lifecycle = RecordingRunLifecycle()

    summary = agent(
        sources=[source()], factory=lambda unused: collector_returning(candidate(), candidate()),
        persister=Mock(return_value=PersistenceSummary(2, 0)), lifecycle=lifecycle,
    ).run_once()

    assert summary.sources_succeeded == 1
    assert len(lifecycle.started) == 1
    assert len(lifecycle.finalized) == 1
    call = lifecycle.finalized[0]
    assert call["attempt"] is lifecycle.started[0]["attempt"]
    assert call["status"] == SUCCESS
    assert call["error"] is None
    assert call["settings"] is SETTINGS
    assert call["metrics"].items_found == 2
    assert call["metrics"].new_items == 2
    assert call["metrics"].pages_checked is None
    assert call["metrics"].relevant_items is None
    assert call["metrics"].http_status is None
    assert call["metrics"].parser_version is None


def test_collection_failure_finalizes_the_same_attempt_with_no_invented_counts():
    lifecycle = RecordingRunLifecycle()
    collector = Mock()
    collector.collect.side_effect = RuntimeError("board unreachable")
    del collector.run_metrics

    summary = agent(
        sources=[source()], factory=lambda unused: collector,
        persister=Mock(), lifecycle=lifecycle,
    ).run_once()

    assert summary.sources_failed == 1
    call = lifecycle.finalized[0]
    assert call["attempt"] is lifecycle.started[0]["attempt"]
    assert call["status"] == FAILED
    assert isinstance(call["error"], RuntimeError)
    assert call["metrics"].items_found is None
    assert call["metrics"].new_items is None


def test_persistence_failure_keeps_the_items_that_were_really_collected():
    lifecycle = RecordingRunLifecycle()

    agent(
        sources=[source()],
        factory=lambda unused: collector_returning(candidate(), candidate(), candidate()),
        persister=Mock(side_effect=RuntimeError("database unavailable")), lifecycle=lifecycle,
    ).run_once()

    call = lifecycle.finalized[0]
    assert call["status"] == FAILED
    assert call["metrics"].items_found == 3
    assert call["metrics"].new_items is None


def test_optional_collector_metrics_are_passed_through_without_overriding_real_counts():
    lifecycle = RecordingRunLifecycle()

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
        persister=Mock(return_value=PersistenceSummary(1, 0)), lifecycle=lifecycle,
    ).run_once()

    metrics = lifecycle.finalized[0]["metrics"]
    assert (metrics.pages_checked, metrics.http_status) == (2, 200)
    assert metrics.parser_version == "greenhouse-v2"
    assert metrics.items_found == 1
    assert metrics.new_items == 1


def test_an_out_of_range_reported_http_status_is_dropped_without_failing_the_run():
    lifecycle = RecordingRunLifecycle()

    class NonsenseCollector:
        def collect(self):
            return [candidate()]

        def run_metrics(self):
            return {"http_status": 999}

    summary = agent(
        sources=[source()], factory=lambda unused: NonsenseCollector(),
        persister=Mock(return_value=PersistenceSummary(1, 0)), lifecycle=lifecycle,
    ).run_once()

    assert summary.sources_succeeded == 1
    metrics = lifecycle.finalized[0]["metrics"]
    assert metrics.http_status is None
    assert metrics.items_found == 1
    assert metrics.new_items == 1


def test_a_source_whose_run_cannot_be_started_is_not_collected_blind(caplog):
    lifecycle = RecordingRunLifecycle(start_error=RuntimeError("history unavailable token=hidden"))
    factory = Mock()
    persister = Mock()
    logger = logging.getLogger("test.radar.run_not_started")

    with caplog.at_level(logging.INFO, logger=logger.name):
        summary = agent(
            sources=[source()], factory=factory, persister=persister,
            lifecycle=lifecycle, logger=logger,
        ).run_once()

    factory.assert_not_called()
    persister.assert_not_called()
    assert lifecycle.finalized == []
    assert (summary.sources_total, summary.sources_succeeded, summary.sources_failed) == (1, 0, 1)
    assert summary.source_results[0].error_type == "RuntimeError"
    not_started = next(
        record for record in caplog.records if record.event == "radar_source_run_not_started"
    )
    assert not_started.source_id == "source_a"
    assert not_started.error_type == "RuntimeError"
    assert "history unavailable" not in caplog.text
    assert "token=hidden" not in caplog.text


def test_one_source_that_cannot_start_does_not_stop_the_others():
    lifecycle = RecordingRunLifecycle()
    sources = [source("source_a"), source("source_b"), source("source_c")]

    def start(settings, config):
        if config.id == "source_b":
            raise RuntimeError("history unavailable")
        return lifecycle.start(settings, config)

    summary = RadarAgent(
        source_loader=lambda: sources,
        collector_factory=lambda config: collector_returning(candidate(config.id)),
        settings_loader=lambda: SETTINGS,
        persister=Mock(return_value=PersistenceSummary(1, 0)),
        qualification_persister=Mock(return_value=QualificationPersistenceSummary(0, 0, 0, 0)),
        run_starter=start, run_finalizer=lifecycle.finalize,
    ).run_once()

    assert (summary.sources_succeeded, summary.sources_failed) == (2, 1)
    assert [call["source"].id for call in lifecycle.started] == ["source_a", "source_c"]
    assert [call["status"] for call in lifecycle.finalized] == [SUCCESS, SUCCESS]


def test_a_failing_finalizer_never_changes_source_outcomes_or_leaks_a_message(caplog):
    lifecycle = RecordingRunLifecycle(
        finalize_error=RuntimeError("history unavailable token=hidden")
    )
    logger = logging.getLogger("test.radar.source_runs")

    with caplog.at_level(logging.INFO, logger=logger.name):
        summary = agent(
            sources=[source("source_a"), source("source_b")],
            factory=lambda config: collector_returning(candidate(config.id)),
            persister=Mock(return_value=PersistenceSummary(1, 0)),
            lifecycle=lifecycle, logger=logger,
        ).run_once()

    assert (summary.sources_total, summary.sources_succeeded, summary.sources_failed) == (2, 2, 0)
    assert summary.items_created == 2
    assert len(lifecycle.finalized) == 2
    not_finalized = [
        record for record in caplog.records
        if record.event == "radar_source_run_not_finalized"
    ]
    assert [record.source_id for record in not_finalized] == ["source_a", "source_b"]
    assert all(record.error_type == "RuntimeError" for record in not_finalized)
    assert all(record.run_status == SUCCESS for record in not_finalized)
    assert "history unavailable" not in caplog.text
    assert "token=hidden" not in caplog.text


def test_every_source_is_instrumented_even_when_one_of_them_fails():
    lifecycle = RecordingRunLifecycle()

    def factory(config):
        if config.id == "source_b":
            collector = Mock()
            collector.collect.side_effect = RuntimeError("collector down")
            del collector.run_metrics
            return collector
        return collector_returning(candidate(config.id))

    summary = agent(
        sources=[source("source_a"), source("source_b"), source("source_c")],
        factory=factory, persister=Mock(return_value=PersistenceSummary(1, 0)),
        lifecycle=lifecycle,
    ).run_once()

    assert (summary.sources_succeeded, summary.sources_failed) == (2, 1)
    assert [call["attempt"].source_id for call in lifecycle.started] == [
        "source_a", "source_b", "source_c",
    ]
    assert [(call["attempt"].source_id, call["status"]) for call in lifecycle.finalized] == [
        ("source_a", SUCCESS), ("source_b", FAILED), ("source_c", SUCCESS),
    ]
    assert len({call["attempt"].id for call in lifecycle.finalized}) == 3


def test_a_collector_factory_failure_still_finalizes_the_started_run():
    lifecycle = RecordingRunLifecycle()

    def factory(unused):
        raise ValueError("unsupported collector type")

    summary = agent(
        sources=[source()], factory=factory, persister=Mock(), lifecycle=lifecycle,
    ).run_once()

    assert summary.sources_failed == 1
    assert len(lifecycle.started) == 1
    call = lifecycle.finalized[0]
    assert call["attempt"] is lifecycle.started[0]["attempt"]
    assert call["status"] == FAILED
    assert isinstance(call["error"], ValueError)
    assert call["metrics"].items_found is None
