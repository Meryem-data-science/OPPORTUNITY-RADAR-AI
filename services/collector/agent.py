"""Deterministic orchestration of one complete opportunity radar run."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging

from services.collector.collectors.base import BaseCollector
from services.collector.collectors.factory import collector_for
from services.collector.config import Settings, load_settings
from services.collector.database.opportunities import (
    PersistenceSummary,
    persist_configured_opportunities,
)
from services.collector.logging_config import get_logger
from services.collector.models.opportunity import OpportunityCandidate
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
class RadarRunSummary:
    sources_total: int
    sources_succeeded: int
    sources_failed: int
    items_collected: int
    items_created: int
    items_updated: int
    source_results: tuple[SourceRunSummary, ...]


SourceLoader = Callable[[], list[SourceConfig]]
CollectorFactory = Callable[[SourceConfig], BaseCollector]
Persister = Callable[
    [Settings, SourceConfig, Iterable[OpportunityCandidate]], PersistenceSummary
]


class RadarAgent:
    """Run every enabled, active source once without retries."""

    def __init__(
        self,
        *,
        source_loader: SourceLoader = load_source_registry,
        collector_factory: CollectorFactory = collector_for,
        settings_loader: Callable[[], Settings] = load_settings,
        persister: Persister = persist_configured_opportunities,
        logger: logging.Logger | None = None,
    ) -> None:
        self._source_loader = source_loader
        self._collector_factory = collector_factory
        self._settings_loader = settings_loader
        self._persister = persister
        self._logger = logger or get_logger(__name__)

    def run_once(self) -> RadarRunSummary:
        """Collect and persist each eligible source exactly once."""
        sources = [
            source
            for source in self._source_loader()
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
            collected = 0
            try:
                candidates = self._collector_factory(source).collect()
                collected = len(candidates)
                persisted = self._persister(settings, source, candidates)
            except Exception as error:
                error_type = type(error).__name__
                results.append(
                    SourceRunSummary(source.id, collected, 0, 0, False, error_type)
                )
                self._logger.error(
                    "Radar source failed.",
                    extra={
                        "event": "radar_source_failed",
                        "source_id": source.id,
                        "items_collected": collected,
                        "error_type": error_type,
                    },
                )
                continue
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

        succeeded = sum(result.success for result in results)
        summary = RadarRunSummary(
            sources_total=len(sources),
            sources_succeeded=succeeded,
            sources_failed=len(sources) - succeeded,
            items_collected=sum(result.collected for result in results),
            items_created=sum(result.created for result in results),
            items_updated=sum(result.updated for result in results),
            source_results=tuple(results),
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
            },
        )
        return summary
