"""Versioned, read-only cohort selection for production matching."""

from __future__ import annotations

import sqlite3


MATCHING_SELECTION_VERSION = "matching-selection-v1"


def select_matching_opportunity_ids(
    connection: sqlite3.Connection,
) -> tuple[int, ...]:
    """Return the deterministic v1 Data/AI matching cohort."""
    rows = connection.execute(
        """SELECT o.id
             FROM opportunities AS o
             JOIN opportunity_qualifications AS q
               ON q.opportunity_id = o.id
            WHERE o.is_active = 1
              AND o.status != 'merged_duplicate'
              AND q.qualification IN ('CORE_TARGET', 'ADJACENT_TARGET')
            ORDER BY o.id"""
    ).fetchall()
    return tuple(int(row[0]) for row in rows)
