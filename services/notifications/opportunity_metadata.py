"""Read-only opportunity facts for notification payloads.

Every value here already has an authority elsewhere in the product, and this
module only reads it: the title and organization come from ``opportunities``,
and the outward link is chosen by :mod:`services.api.link_priority` — the same
deterministic policy the opportunity and Portfolio surfaces already use, so a
notification can never point somewhere the Portfolio page would not.
"""

from __future__ import annotations

import sqlite3
from typing import Sequence

from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)

from .policy import NOTIFICATION_TARGET_PATH, OpportunityNotificationMetadata


class NotificationMetadataError(RuntimeError):
    """Raised when an opportunity cannot be described safely."""


def _observations(
    connection: sqlite3.Connection, opportunity_ids: Sequence[int]
) -> dict[int, list[SourceObservation]]:
    placeholders = ", ".join("?" for _ in opportunity_ids)
    rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id,opportunity_sources.id,sources.type,
        opportunity_sources.application_url,opportunity_sources.source_url,
        opportunity_sources.canonical_url FROM opportunity_sources JOIN sources
        ON sources.id=opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({placeholders})
        ORDER BY opportunity_sources.opportunity_id,opportunity_sources.id""",
        tuple(opportunity_ids),
    ).fetchall()
    observations: dict[int, list[SourceObservation]] = {}
    for row in rows:
        observations.setdefault(int(row[0]), []).append(SourceObservation(*row[1:]))
    return observations


def read_opportunity_metadata(
    connection: sqlite3.Connection, opportunity_ids: Sequence[int]
) -> dict[int, OpportunityNotificationMetadata]:
    """Describe every requested opportunity, or refuse the whole batch."""
    wanted = sorted(set(opportunity_ids))
    if not wanted:
        return {}
    rows = connection.execute(
        f"""SELECT id,canonical_title,organization,source_url,application_url,canonical_url
        FROM opportunities WHERE id IN ({", ".join("?" for _ in wanted)})
        ORDER BY id ASC""",
        tuple(wanted),
    ).fetchall()
    if {int(row[0]) for row in rows} != set(wanted):
        raise NotificationMetadataError(
            "notified opportunities are missing from the opportunity authority"
        )
    observations = _observations(connection, wanted)
    metadata: dict[int, OpportunityNotificationMetadata] = {}
    for row in rows:
        opportunity_id = int(row[0])
        title, organization = row[1], row[2]
        if not isinstance(title, str) or not isinstance(organization, str):
            raise NotificationMetadataError("invalid opportunity metadata")
        fallback = preferred_link(row[4], row[3], row[5]) or row[3]
        if not isinstance(fallback, str) or not fallback.strip():
            raise NotificationMetadataError("opportunity has no usable link")
        metadata[opportunity_id] = OpportunityNotificationMetadata(
            opportunity_id,
            title,
            organization,
            select_original_url(
                observations.get(opportunity_id, ()), fallback=fallback
            ),
            NOTIFICATION_TARGET_PATH,
        )
    return metadata
