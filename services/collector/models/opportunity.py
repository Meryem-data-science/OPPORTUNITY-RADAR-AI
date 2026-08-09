"""Models produced by collectors before any database persistence."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OpportunityCandidate:
    """A normalized, extracted opportunity that has not been stored."""

    source_id: str
    source_external_id: str
    canonical_title: str
    organization: str
    location: str | None
    description: str | None
    published_at: str | None
    source_url: str
    application_url: str | None
    canonical_url: str | None
