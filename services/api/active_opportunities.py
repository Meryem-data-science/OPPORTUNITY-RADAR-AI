"""Read-only availability projection beside immutable assessment snapshots.

An assessment remains valid history when its posting closes. Public catalogue
surfaces can hide that posting without rewriting the assessment or its audit.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any


def read_active_opportunity_ids(
    connection: Any, opportunity_ids: Sequence[int]
) -> frozenset[int]:
    """Return only currently visible, active postings from the supplied ids.

    This is a catalogue projection, not a snapshot-integrity check: callers
    validate the complete stored cohort before using this subset.
    """
    wanted = tuple(sorted(set(opportunity_ids)))
    if not wanted:
        return frozenset()
    # Keep each SELECT below SQLite's traditional parameter limit. An outer
    # deferred transaction holds the same authority state across all chunks.
    active: set[int] = set()
    for offset in range(0, len(wanted), 500):
        batch = wanted[offset : offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT id FROM opportunities
            WHERE id IN ({placeholders}) AND status = ? AND is_active = ?
            ORDER BY id""",
            (*batch, "visible", 1),
        ).fetchall()
        active.update(int(row[0]) for row in rows)
    return frozenset(active)


@contextmanager
def read_surface_snapshot(connection: Any) -> Iterator[None]:
    """Hold SQLite reads together without writes or owning an outer transaction.

    SQLite reports `in_transaction` explicitly. Backends without that contract
    keep their existing transaction behaviour; this helper does not infer or
    alter a remote transaction.
    """
    owns_snapshot = getattr(connection, "in_transaction", None) is False
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        yield
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")
