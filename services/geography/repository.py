"""Transactional persistence for the Phase 7A.1 geographic projection.

Plain SQLite, no ORM, no second connection factory and no second migration
runner, in the style
`services/collector/extractors/opportunity_constraints/repository.py` already
set. The idempotence key is the same pair `0004`, `0012` and this slice all use
— `source_fingerprint` plus `resolver_version` — so an operator who understands
one projection understands this one.

Three rules are enforced here rather than trusted to callers:

* **`opportunity_constraint_locations` is never written.** There is no
  `INSERT`, `UPDATE` or `DELETE` against it anywhere below, and a test reads
  this file to keep it so. The raw strings are the evidence this projection is
  read from; a derivation that edits its own source cannot be audited against
  it. `opportunities` and `profile_mobility` are untouched for the same reason;
* **no verdict is stored.** Nothing here writes MATCH, OUT_OF_TARGET, an
  eligibility, a score or a profile id, and `0024` has no column that could
  hold one;
* **one source row is replaced whole or not at all.** Writing a reading means
  deleting that source row's previous segments and inserting the new ones
  inside one transaction. A half-written reading — two segments of a new
  string beside a third from the old one — would be a projection that explains
  itself wrongly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Callable

from services.geography.models import (
    LocationResolution,
    LocationSource,
    ResolutionStatus,
    ResolvedSegment,
)

__all__ = [
    "GeographicRepositoryError",
    "delete_location_resolutions",
    "read_location_resolutions",
    "read_opportunity_resolutions",
    "store_location_resolutions",
    "stored_signature",
]


class GeographicRepositoryError(RuntimeError):
    """Raised when the geographic projection cannot be read or written safely."""


_COLUMNS = (
    "opportunity_id", "source_location_id", "segment_position", "raw_segment",
    "country_code", "city_key", "resolution_status", "resolution_rule_id",
    "resolver_version", "source_fingerprint", "resolved_at",
)


def _row(values: Sequence) -> LocationResolution:
    return LocationResolution(
        opportunity_id=int(values[0]),
        source_location_id=int(values[1]),
        segment_position=int(values[2]),
        raw_segment=str(values[3]),
        status=ResolutionStatus(str(values[6])),
        rule_id=str(values[7]),
        country_code=None if values[4] is None else str(values[4]),
        city_key=None if values[5] is None else str(values[5]),
        resolver_version=str(values[8]),
        source_fingerprint=str(values[9]),
    )


def stored_signature(
    connection: sqlite3.Connection, source_location_id: int
) -> tuple[str, str] | None:
    """The `(fingerprint, version)` already stored for one source row, or None.

    This is the whole idempotence check. Every segment of one source row is
    written by one call under one pair, so a row holding two different pairs is
    a projection that was interrupted in a way the transaction should have
    prevented; it is reported as *no* signature, which recomputes it, rather
    than as one of the two.
    """
    rows = connection.execute(
        "SELECT DISTINCT source_fingerprint, resolver_version "
        "FROM opportunity_location_resolutions WHERE source_location_id = ?",
        (source_location_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return str(rows[0][0]), str(rows[0][1])


def delete_location_resolutions(
    connection: sqlite3.Connection, source_location_id: int
) -> None:
    """Remove one source row's whole reading. Writes nothing to its source."""
    connection.execute(
        "DELETE FROM opportunity_location_resolutions WHERE source_location_id = ?",
        (source_location_id,),
    )


def store_location_resolutions(
    connection: sqlite3.Connection,
    source: LocationSource,
    segments: Sequence[ResolvedSegment],
    *,
    resolver_version: str,
    source_fingerprint: str,
    resolved_at: str | None = None,
    after_delete: Callable[[], None] | None = None,
) -> None:
    """Replace one source row's reading, whole, in one transaction.

    The caller supplies segments the pure resolver already produced, so nothing
    is resolved here and nothing is decided here. A failure at any point — a
    `CHECK` the reading violates, a foreign key, an interruption — rolls back
    everything, including the delete, so the previous reading survives intact
    rather than being replaced by a partial one.

    `after_delete` is a test seam invoked once the old rows are gone and before
    the new ones are written; production callers leave it unset.
    """
    if not isinstance(source, LocationSource):
        raise GeographicRepositoryError("source must be a LocationSource")
    if not segments:
        raise GeographicRepositoryError(
            "a source location row always reads as at least one segment"
        )
    timestamp = resolved_at or datetime.now(UTC).isoformat(timespec="microseconds")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute(
            "SELECT 1 FROM opportunity_constraint_locations WHERE id = ? "
            "AND opportunity_id = ?",
            (source.source_location_id, source.opportunity_id),
        ).fetchone() is None:
            raise GeographicRepositoryError(
                f"source location {source.source_location_id} does not belong to "
                f"opportunity {source.opportunity_id}"
            )
        delete_location_resolutions(connection, source.source_location_id)
        if after_delete is not None:
            after_delete()
        for position, segment in enumerate(segments):
            connection.execute(
                f"INSERT INTO opportunity_location_resolutions "
                f"({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                (
                    source.opportunity_id,
                    source.source_location_id,
                    position,
                    segment.raw_segment,
                    segment.country_code,
                    segment.city_key,
                    segment.status.value,
                    segment.rule_id,
                    resolver_version,
                    source_fingerprint,
                    timestamp,
                ),
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def read_location_resolutions(
    connection: sqlite3.Connection, source_location_id: int
) -> tuple[LocationResolution, ...]:
    """One source row's stored reading, in the order the string named it."""
    return tuple(
        _row(values)
        for values in connection.execute(
            f"SELECT {', '.join(_COLUMNS[:-1])} FROM opportunity_location_resolutions "
            "WHERE source_location_id = ? ORDER BY segment_position",
            (source_location_id,),
        ).fetchall()
    )


def read_opportunity_resolutions(
    connection: sqlite3.Connection, opportunity_id: int
) -> tuple[LocationResolution, ...]:
    """Every stored reading of one posting, source row order then segment order.

    This is what the evaluator judges. Reading is not resolving: it returns what
    was stored, and storing is the only thing that changes it.
    """
    return tuple(
        _row(values)
        for values in connection.execute(
            f"SELECT {', '.join(_COLUMNS[:-1])} FROM opportunity_location_resolutions "
            "WHERE opportunity_id = ? ORDER BY source_location_id, segment_position",
            (opportunity_id,),
        ).fetchall()
    )
