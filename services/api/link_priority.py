"""Deterministic choice of the best original link for one opportunity.

An opportunity can be observed by several sources, and the human duplicate
review chooses which row stays canonical for deduplication. That choice must
not decide which link is shown to the user: a career page / ATS observation is
a better destination than a job-board observation even when the job-board row
was kept as canonical.

The policy is expressed once here, for the source types actually supported
today, and read from data already persisted in `opportunity_sources` joined to
`sources`. It is read-only and adds no column, table, or migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


#: Source types whose observations point at an official career page / ATS,
#: most preferred first. `greenhouse` is the company's own ATS board.
OFFICIAL_SOURCE_TYPES: tuple[str, ...] = ("greenhouse",)

#: Source types that observe a third-party job board. They are never preferred
#: over an official observation; today only the LinkedIn Gmail alert.
JOB_BOARD_SOURCE_TYPES: tuple[str, ...] = ("gmail_linkedin_alert",)


@dataclass(frozen=True)
class SourceObservation:
    """One persisted `opportunity_sources` row with its source type."""

    observation_id: int
    source_type: str
    application_url: str | None
    source_url: str | None
    canonical_url: str | None


def is_official_source_type(source_type: str | None) -> bool:
    """Return whether this source type represents an official career page."""
    return source_type in OFFICIAL_SOURCE_TYPES


def official_rank(source_type: str | None) -> int | None:
    """Return the official priority of a source type, or None when not official."""
    if source_type is None or source_type not in OFFICIAL_SOURCE_TYPES:
        return None
    return OFFICIAL_SOURCE_TYPES.index(source_type)


def _usable(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def preferred_link(
    application_url: str | None,
    source_url: str | None,
    canonical_url: str | None,
) -> str | None:
    """Return the first usable link, preferring application, then source, then canonical."""
    for value in (application_url, source_url, canonical_url):
        usable = _usable(value)
        if usable is not None:
            return usable
    return None


def _observation_link(observation: SourceObservation) -> str | None:
    return preferred_link(
        observation.application_url, observation.source_url, observation.canonical_url
    )


def select_original_url(
    observations: Iterable[SourceObservation], *, fallback: str
) -> str:
    """Return the official observation link when one exists, else the fallback.

    Tie-break, in order, over the official observations that carry a usable
    link: the most preferred official source type (`OFFICIAL_SOURCE_TYPES`
    order), then the smallest `opportunity_sources.id`, which is the earliest
    recorded observation. Observation ids are stable: a merge only re-points
    `opportunity_id`, so the choice never depends on merge ordering.
    """
    candidates: list[tuple[int, int, str]] = []
    for observation in observations:
        rank = official_rank(observation.source_type)
        link = _observation_link(observation)
        if rank is not None and link is not None:
            candidates.append((rank, observation.observation_id, link))
    if not candidates:
        return fallback
    return min(candidates)[2]
