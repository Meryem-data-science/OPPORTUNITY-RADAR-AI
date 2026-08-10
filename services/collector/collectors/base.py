"""Minimal contract shared by external opportunity collectors."""

from abc import ABC, abstractmethod
from typing import Any, Protocol

from services.collector.models.opportunity import OpportunityCandidate


class OpportunityCollector(Protocol):
    """The minimal capability required by the radar orchestrator."""

    def collect(self) -> list[OpportunityCandidate]: ...


class BaseCollector(ABC):
    """Fetch, validate, and normalize opportunities from one configured source."""

    source_id: str

    @abstractmethod
    def fetch(self) -> Any:
        """Fetch and return a source response payload."""

    @abstractmethod
    def parse(self, payload: Any) -> list[dict[str, Any]]:
        """Validate a payload and return its raw opportunity records."""

    @abstractmethod
    def normalize(self, item: dict[str, Any]) -> OpportunityCandidate:
        """Normalize one raw source record."""

    def collect(self) -> list[OpportunityCandidate]:
        """Run the complete read-only collection flow."""
        return [self.normalize(item) for item in self.parse(self.fetch())]
