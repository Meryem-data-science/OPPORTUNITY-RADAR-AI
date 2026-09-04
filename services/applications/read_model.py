"""The projection a candidature is read through.

A tracked application is only half a thing on its own: the row says SUBMITTED
and holds a follow-up date, and the user wants to see the job title, the
company, and above all the link back to the real offer. So the read model
joins the two, and it joins them in one place so that no page has to rebuild
the join and no page can get the link wrong.

The link is not copied into `applications`. The opportunity remains the single
authority for where an offer lives, and the outward URL is chosen by
:mod:`services.api.link_priority` — the same deterministic policy the
opportunity, Portfolio and notification surfaces already use — so the button
on `/applications` can never point somewhere the home page would not.

Everything here reads. The connection it is handed may be, and on the API
surface is, opened read-only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)
from services.applications import repository
from services.applications.models import (
    ApplicationError,
    ApplicationEventRecord,
    ApplicationRecord,
)


class ApplicationReadError(ApplicationError):
    """Raised when a candidature cannot be described safely."""


@dataclass(frozen=True)
class ApplicationOpportunityView:
    """The facts about the real offer a candidature is for."""

    id: int
    canonical_title: str
    organization: str
    location: str | None
    original_url: str


@dataclass(frozen=True)
class ApplicationView:
    """One candidature and the opportunity it tracks."""

    application: ApplicationRecord
    opportunity: ApplicationOpportunityView


@dataclass(frozen=True)
class ApplicationDetailView:
    """One candidature, its opportunity, and everything that happened to it."""

    application: ApplicationRecord
    opportunity: ApplicationOpportunityView
    events: tuple[ApplicationEventRecord, ...]


def _observations(
    connection: sqlite3.Connection, opportunity_ids: Sequence[int]
) -> dict[int, list[SourceObservation]]:
    placeholders = ", ".join("?" for _ in opportunity_ids)
    rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id, opportunity_sources.id,
        sources.type, opportunity_sources.application_url,
        opportunity_sources.source_url, opportunity_sources.canonical_url
        FROM opportunity_sources JOIN sources ON sources.id = opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id, opportunity_sources.id""",
        tuple(opportunity_ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in rows:
        observations.setdefault(int(row[0]), []).append(SourceObservation(*row[1:]))
    return observations


def read_opportunity_views(
    connection: sqlite3.Connection, opportunity_ids: Sequence[int]
) -> dict[int, ApplicationOpportunityView]:
    """Describe every named opportunity, or refuse the whole batch.

    A candidature whose opportunity has gone missing is not something to
    render with a blank title: the schema forbids it, so finding it here means
    the database disagrees with itself and the read fails rather than papering
    over it.
    """
    wanted = sorted(set(opportunity_ids))
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    rows = connection.execute(
        f"""SELECT id, canonical_title, organization, location, source_url,
        application_url, canonical_url FROM opportunities
        WHERE id IN ({placeholders}) ORDER BY id ASC""",
        tuple(wanted),
    ).fetchall()
    if {int(row[0]) for row in rows} != set(wanted):
        raise ApplicationReadError(
            "a tracked opportunity is missing from the opportunity authority"
        )
    observations = _observations(connection, wanted)
    views: dict[int, ApplicationOpportunityView] = {}
    for row in rows:
        opportunity_id = int(row[0])
        title, organization = row[1], row[2]
        if not isinstance(title, str) or not isinstance(organization, str):
            raise ApplicationReadError("invalid opportunity metadata")
        fallback = preferred_link(row[5], row[4], row[6]) or row[4]
        if not isinstance(fallback, str) or not fallback.strip():
            raise ApplicationReadError("tracked opportunity has no usable link")
        views[opportunity_id] = ApplicationOpportunityView(
            id=opportunity_id,
            canonical_title=title,
            organization=organization,
            location=None if row[3] is None else str(row[3]),
            original_url=select_original_url(
                observations.get(opportunity_id, ()), fallback=fallback
            ),
        )
    return views


def read_applications(
    connection: sqlite3.Connection, *, profile_id: int
) -> list[ApplicationView]:
    """Every candidature of this profile, most recently moved first."""
    records = repository.list_applications(connection, profile_id=profile_id)
    views = read_opportunity_views(
        connection, [record.opportunity_id for record in records]
    )
    return [
        ApplicationView(
            application=record, opportunity=views[record.opportunity_id]
        )
        for record in records
    ]


def read_application_detail(
    connection: sqlite3.Connection, *, profile_id: int, application_id: int
) -> ApplicationDetailView | None:
    """One candidature with its whole timeline, or None if this profile has none."""
    record = repository.read_application(
        connection, profile_id=profile_id, application_id=application_id
    )
    if record is None:
        return None
    views = read_opportunity_views(connection, [record.opportunity_id])
    history = repository.read_events(connection, [record.id])
    return ApplicationDetailView(
        application=record,
        opportunity=views[record.opportunity_id],
        events=tuple(history.get(record.id, ())),
    )
