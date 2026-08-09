"""Collector registry keyed by source type."""

from collections.abc import Callable

from services.collector.collectors.base import BaseCollector
from services.collector.collectors.greenhouse import GreenhouseCollector
from services.collector.sources import SourceConfig


class UnsupportedCollectorTypeError(ValueError):
    """Raised when no collector is registered for a source type."""


CollectorBuilder = Callable[[SourceConfig], BaseCollector]
COLLECTOR_REGISTRY: dict[str, CollectorBuilder] = {
    "greenhouse": GreenhouseCollector,
}


def collector_for(source: SourceConfig) -> BaseCollector:
    """Build the collector registered for ``source.type``."""
    try:
        builder = COLLECTOR_REGISTRY[source.type]
    except KeyError as error:
        raise UnsupportedCollectorTypeError(
            f"unsupported collector type: {source.type}"
        ) from error
    return builder(source)
