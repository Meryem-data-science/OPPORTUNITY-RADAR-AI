"""Deterministic orchestration of one complete opportunity radar run."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging
from typing import Protocol

from services.collector.collectors.base import OpportunityCollector
from services.collector.collectors.factory import collector_for
from services.collector.config import Settings, load_settings
from services.collector.database.opportunities import (
    PersistenceSummary as OpportunityPersistenceSummary,
    persist_configured_opportunities,
)
from services.collector.database.source_runs import (
    record_configured_source_run,
    start_source_run,
)
from services.collector.logging_config import get_logger
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.models.source_run import (
    FAILED as SOURCE_RUN_FAILED,
    NO_METRICS,
    SUCCESS as SOURCE_RUN_SUCCESS,
    SourceRunAttempt,
    SourceRunMetrics,
    metrics_reported_by,
)
from services.collector.qualification.persistence import (
    PersistenceSummary as QualificationPersistenceSummary,
    persist_configured_qualifications,
)
from services.collector.sources import SourceConfig, load_source_registry


@dataclass(frozen=True)
class SourceRunSummary:
    source_id: str
    collected: int
    created: int
    updated: int
    success: bool
    error_type: str | None = None


@dataclass(frozen=True)
class QualificationRunSummary:
    success: bool
    created: int
    updated: int
    unchanged: int
    total: int
    error_type: str | None = None


@dataclass(frozen=True)
class RadarRunSummary:
    sources_total: int
    sources_succeeded: int
    sources_failed: int
    items_collected: int
    items_created: int
    items_updated: int
    source_results: tuple[SourceRunSummary, ...]
    qualification: QualificationRunSummary

    @property
    def success(self) -> bool:
        return self.sources_failed == 0 and self.qualification.success


SourceLoader = Callable[[], list[SourceConfig]]
CollectorFactory = Callable[[SourceConfig], OpportunityCollector]
Persister = Callable[
    [Settings, SourceConfig, Iterable[OpportunityCandidate]], OpportunityPersistenceSummary
]
QualificationPersister = Callable[[Settings], QualificationPersistenceSummary]
RunStarter = Callable[[str], SourceRunAttempt]


class RunRecorder(Protocol):
    """Persist the terminal state of one attempted source run."""

    def __call__(
        self,
        settings: Settings,
        source: SourceConfig,
        attempt: SourceRunAttempt,
        *,
        status: str,
        metrics: SourceRunMetrics,
        error: Exception | None = None,
    ) -> object: ...


class RadarAgent:
    """Run every enabled, active source once without retries."""

    def __init__(
        self,
        *,
        source_loader: SourceLoader = load_source_registry,
        collector_factory: CollectorFactory = collector_for,
        settings_loader: Callable[[], Settings] = load_settings,
        persister: Persister = persist_configured_opportunities,
        qualification_persister: QualificationPersister = persist_configured_qualifications,
        run_starter: RunStarter = start_source_run,
        run_recorder: RunRecorder = record_configured_source_run,
        logger: logging.Logger | None = None,
        source_ids: Iterable[str] | None = None,
    ) -> None:
        self._source_loader = source_loader
        self._collector_factory = collector_factory
        self._settings_loader = settings_loader
        self._persister = persister
        self._qualification_persister = qualification_persister
        self._run_starter = run_starter
        self._run_recorder = run_recorder
        self._logger = logger or get_logger(__name__)
        self._source_ids = tuple(source_ids) if source_ids is not None else None

    @staticmethod
    def _source_run_metrics(
        collector: object | None,
        *,
        items_found: int | None = None,
        new_items: int | None = None,
    ) -> SourceRunMetrics:
        """Combine what this run actually observed with any collector metrics.

        Counts the agent measured itself always win; everything a collector does
        not report stays unknown rather than becoming an invented zero.
        """
        reported = NO_METRICS if collector is None else metrics_reported_by(collector)
        return reported.merge(items_found=items_found, new_items=new_items)

    def _record_source_run(
        self,
        settings: Settings,
        source: SourceConfig,
        attempt: SourceRunAttempt,
        *,
        status: str,
        metrics: SourceRunMetrics,
        error: Exception | None = None,
    ) -> None:
        """Persist one attempt's history without letting it change that attempt.

        Run history observes collection; it never governs it. A recording failure
        is reported and dropped so an already-committed source stays successful
        and the remaining sources still run.
        """
        try:
            self._run_recorder(
                settings, source, attempt, status=status, metrics=metrics, error=error
            )
        except Exception as recording_error:
            self._logger.error(
                "Radar source run was not recorded.",
                extra={
                    "event": "radar_source_run_not_recorded",
                    "source_id": source.id,
                    "run_status": status,
                    "error_type": type(recording_error).__name__,
                },
            )

    def run_once(self) -> RadarRunSummary:
        """Collect and persist each eligible source exactly once."""
        configured = self._source_loader()
        if self._source_ids is not None:
            requested = set(self._source_ids)
            known = {source.id for source in configured}
            unknown = requested - known
            if unknown:
                raise ValueError(f"unknown source: {sorted(unknown)[0]}")
            unavailable = [
                source.id for source in configured
                if source.id in requested and (not source.enabled or source.status != "active")
            ]
            if unavailable:
                raise ValueError(f"source is disabled or inactive: {unavailable[0]}")
            configured = [source for source in configured if source.id in requested]
        sources = [
            source
            for source in configured
            if source.enabled and source.status == "active"
        ]
        settings = self._settings_loader()
        self._logger.info(
            "Radar run started.",
            extra={"event": "radar_run_started", "sources_total": len(sources)},
        )
        results: list[SourceRunSummary] = []
        for source in sources:
            self._logger.info(
                "Radar source started.",
                extra={"event": "radar_source_started", "source_id": source.id},
            )
            attempt = self._run_starter(source.id)
            collector: object | None = None
            collected: int | None = None
            try:
                collector = self._collector_factory(source)
                candidates = collector.collect()
                collected = len(candidates)
                persisted = self._persister(settings, source, candidates)
            except Exception as error:
                error_type = type(error).__name__
                self._record_source_run(
                    settings,
                    source,
                    attempt,
                    status=SOURCE_RUN_FAILED,
                    metrics=self._source_run_metrics(collector, items_found=collected),
                    error=error,
                )
                items_collected = 0 if collected is None else collected
                results.append(
                    SourceRunSummary(source.id, items_collected, 0, 0, False, error_type)
                )
                self._logger.error(
                    "Radar source failed.",
                    extra={
                        "event": "radar_source_failed",
                        "source_id": source.id,
                        "items_collected": items_collected,
                        "error_type": error_type,
                    },
                )
                continue
            self._record_source_run(
                settings,
                source,
                attempt,
                status=SOURCE_RUN_SUCCESS,
                metrics=self._source_run_metrics(
                    collector, items_found=collected, new_items=persisted.created
                ),
            )
            results.append(
                SourceRunSummary(
                    source.id,
                    collected,
                    persisted.created,
                    persisted.updated,
                    True,
                )
            )
            self._logger.info(
                "Radar source succeeded.",
                extra={
                    "event": "radar_source_succeeded",
                    "source_id": source.id,
                    "items_collected": collected,
                    "items_created": persisted.created,
                    "items_updated": persisted.updated,
                },
            )

        self._logger.info(
            "Radar qualification started.",
            extra={"event": "radar_qualification_started"},
        )
        try:
            persisted_qualifications = self._qualification_persister(settings)
        except Exception as error:
            error_type = type(error).__name__
            qualification = QualificationRunSummary(
                False, 0, 0, 0, 0, error_type
            )
            self._logger.error(
                "Radar qualification failed.",
                extra={
                    "event": "radar_qualification_failed",
                    "error_type": error_type,
                },
            )
        else:
            qualification = QualificationRunSummary(
                True,
                persisted_qualifications.created,
                persisted_qualifications.updated,
                persisted_qualifications.unchanged,
                persisted_qualifications.total,
            )
            self._logger.info(
                "Radar qualification succeeded.",
                extra={
                    "event": "radar_qualification_succeeded",
                    "qualifications_created": qualification.created,
                    "qualifications_updated": qualification.updated,
                    "qualifications_unchanged": qualification.unchanged,
                    "qualifications_total": qualification.total,
                },
            )

        succeeded = sum(result.success for result in results)
        summary = RadarRunSummary(
            sources_total=len(sources),
            sources_succeeded=succeeded,
            sources_failed=len(sources) - succeeded,
            items_collected=sum(result.collected for result in results),
            items_created=sum(result.created for result in results),
            items_updated=sum(result.updated for result in results),
            source_results=tuple(results),
            qualification=qualification,
        )
        self._logger.info(
            "Radar run completed.",
            extra={
                "event": "radar_run_completed",
                "sources_total": summary.sources_total,
                "sources_succeeded": summary.sources_succeeded,
                "sources_failed": summary.sources_failed,
                "items_collected": summary.items_collected,
                "items_created": summary.items_created,
                "items_updated": summary.items_updated,
                "qualification_success": summary.qualification.success,
                "qualification_error_type": summary.qualification.error_type,
            },
        )
        return summary
