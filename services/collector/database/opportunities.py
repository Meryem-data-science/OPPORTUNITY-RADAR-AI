"""Transactional persistence for collected opportunity candidates."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from services.collector.config import DatabaseBackend, Settings
from services.collector.database.connection import DatabaseConnection
from services.collector.database.connection import connect_configured_database
from services.collector.models.opportunity import OpportunityCandidate
from services.collector.sources import SourceConfig

VISIBLE_STATUS = "visible"


class OpportunityPersistenceError(RuntimeError):
    """Raised when an opportunity batch cannot be persisted atomically."""


@dataclass(frozen=True)
class PersistenceSummary:
    """Counts of normal create and refresh outcomes for one batch."""

    created: int
    updated: int

    @property
    def total(self) -> int:
        return self.created + self.updated


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _upsert_source(connection: DatabaseConnection, source: SourceConfig) -> None:
    statement, parameters = _source_upsert_statement(source)
    connection.execute(statement, parameters)


def _source_upsert_statement(source: SourceConfig) -> tuple[str, tuple[object, ...]]:
    return (
        """
        INSERT INTO sources (
            id, type, enabled, category, country, frequency_minutes, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type = excluded.type,
            enabled = excluded.enabled,
            category = excluded.category,
            country = excluded.country,
            frequency_minutes = excluded.frequency_minutes,
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            source.id,
            source.type,
            int(source.enabled),
            source.category,
            source.country,
            source.frequency_minutes,
            source.status,
        ),
    )


def _find_opportunity_id(
    connection: DatabaseConnection, source_id: str, source_url: str
) -> int | None:
    row = connection.execute(
        """
        SELECT opportunity_id
        FROM opportunity_sources
        WHERE source_id = ? AND source_url = ?
        LIMIT 1
        """,
        (source_id, source_url),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _insert_candidate(
    connection: DatabaseConnection, candidate: OpportunityCandidate, observed_at: str
) -> int:
    row = connection.execute(
        """
        INSERT INTO opportunities (
            canonical_title, organization, location, description, published_at,
            discovered_at, first_seen_at, last_seen_at, source_url,
            application_url, canonical_url, status, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        RETURNING id
        """,
        (
            candidate.canonical_title,
            candidate.organization,
            candidate.location,
            candidate.description,
            candidate.published_at,
            observed_at,
            observed_at,
            observed_at,
            candidate.source_url,
            candidate.application_url,
            candidate.canonical_url,
            VISIBLE_STATUS,
        ),
    ).fetchone()
    if row is None:
        raise OpportunityPersistenceError("opportunity insert returned no id")
    opportunity_id = int(row[0])
    connection.execute(
        """
        INSERT INTO opportunity_sources (
            opportunity_id, source_id, source_url, application_url,
            canonical_url, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            opportunity_id,
            candidate.source_id,
            candidate.source_url,
            candidate.application_url,
            candidate.canonical_url,
            observed_at,
        ),
    )
    return opportunity_id


def _update_candidate(
    connection: DatabaseConnection,
    opportunity_id: int,
    candidate: OpportunityCandidate,
    observed_at: str,
) -> None:
    primary = connection.execute(
        "SELECT source_url FROM opportunities WHERE id = ?", (opportunity_id,)
    ).fetchone()
    if primary is None:
        raise OpportunityPersistenceError("persisted source references a missing opportunity")
    if candidate.source_url != primary[0]:
        connection.execute(
            """UPDATE opportunities SET last_seen_at = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (observed_at, opportunity_id),
        )
        connection.execute(
            """UPDATE opportunity_sources SET
                   application_url = COALESCE(?, application_url),
                   canonical_url = COALESCE(?, canonical_url)
               WHERE opportunity_id = ? AND source_id = ? AND source_url = ?""",
            (candidate.application_url, candidate.canonical_url, opportunity_id,
             candidate.source_id, candidate.source_url),
        )
        return
    connection.execute(
        """
        UPDATE opportunities SET
            canonical_title = ?,
            organization = ?,
            location = COALESCE(?, location),
            description = COALESCE(?, description),
            published_at = COALESCE(?, published_at),
            application_url = COALESCE(?, application_url),
            canonical_url = COALESCE(?, canonical_url),
            last_seen_at = ?,
            status = ?,
            is_active = 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            candidate.canonical_title,
            candidate.organization,
            candidate.location,
            candidate.description,
            candidate.published_at,
            candidate.application_url,
            candidate.canonical_url,
            observed_at,
            VISIBLE_STATUS,
            opportunity_id,
        ),
    )
    connection.execute(
        """
        UPDATE opportunity_sources SET
            application_url = COALESCE(?, application_url),
            canonical_url = COALESCE(?, canonical_url)
        WHERE opportunity_id = ? AND source_id = ? AND source_url = ?
        """,
        (
            candidate.application_url,
            candidate.canonical_url,
            opportunity_id,
            candidate.source_id,
            candidate.source_url,
        ),
    )


def persist_opportunities(
    connection: DatabaseConnection,
    source: SourceConfig,
    candidates: Iterable[OpportunityCandidate],
    *,
    clock: Callable[[], str] = _utc_now,
) -> PersistenceSummary:
    """Upsert a source and atomically create or refresh all its candidates."""
    batch = list(candidates)
    if any(candidate.source_id != source.id for candidate in batch):
        raise OpportunityPersistenceError("candidate source_id does not match source")
    created = 0
    updated = 0
    connection.execute("BEGIN")
    try:
        _upsert_source(connection, source)
        for candidate in batch:
            observed_at = clock()
            opportunity_id = _find_opportunity_id(
                connection, source.id, candidate.source_url
            )
            if opportunity_id is None:
                _insert_candidate(connection, candidate, observed_at)
                created += 1
            else:
                _update_candidate(connection, opportunity_id, candidate, observed_at)
                updated += 1
        connection.execute("COMMIT")
    except Exception as error:
        try:
            connection.execute("ROLLBACK")
        except Exception as rollback_error:
            raise OpportunityPersistenceError(
                "opportunity batch failed and rollback was unsuccessful"
            ) from rollback_error
        if isinstance(error, OpportunityPersistenceError):
            raise
        raise OpportunityPersistenceError(
            "opportunity batch persistence failed"
        ) from error
    return PersistenceSummary(created=created, updated=updated)


def persist_configured_opportunities(
    settings: Settings,
    source: SourceConfig,
    candidates: Iterable[OpportunityCandidate],
) -> PersistenceSummary:
    """Persist to operational Phase 1 SQLite; reject remote opportunity writes."""
    batch = list(candidates)
    if any(candidate.source_id != source.id for candidate in batch):
        raise OpportunityPersistenceError("candidate source_id does not match source")
    if settings.database_backend is DatabaseBackend.TURSO:
        raise OpportunityPersistenceError(
            "Remote Turso opportunity writes are disabled in Phase 1"
        )
    connection = connect_configured_database(settings)
    try:
        return persist_opportunities(connection, source, batch)
    finally:
        connection.close()
