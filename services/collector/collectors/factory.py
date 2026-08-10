"""Collector registry keyed by source type."""

from collections.abc import Callable

from services.collector.collectors.base import OpportunityCollector
from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.collectors.linkedin_job_alert import LinkedInJobAlertCollector
from services.collector.sources import SourceConfig


class UnsupportedCollectorTypeError(ValueError):
    """Raised when no collector is registered for a source type."""


CollectorBuilder = Callable[[SourceConfig], OpportunityCollector]
COLLECTOR_REGISTRY: dict[str, CollectorBuilder] = {
    "greenhouse": GreenhouseCollector,
    "gmail_linkedin_alert": LinkedInJobAlertCollector,
}


def collector_for(source: SourceConfig) -> OpportunityCollector:
    """Build the collector registered for ``source.type``."""
    try:
        builder = COLLECTOR_REGISTRY[source.type]
    except KeyError as error:
        raise UnsupportedCollectorTypeError(
            f"unsupported collector type: {source.type}"
        ) from error
    return builder(source)
