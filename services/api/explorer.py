"""Read-only public surface of the Explorer Data & AI mode.

Phase 11.1C exposes the persisted Data/AI corpus for exploration, independently
of any profile. It is not a Recommendation: nothing here ranks by a score, reads
a Matching, Priority or Portfolio snapshot, or knows who is asking. The only
order is navigation order — `last_seen_at DESC, id DESC` — and every filter is a
comparison against a value an earlier phase already persisted:

    country / city      opportunity_location_resolutions (Phase 7), RESOLVED only
    opportunity_type    opportunity_constraints.opportunity_type (Phase 3.5)
    domain              opportunity_qualifications.fine_primary_category (Phase 8)
    source              opportunity_sources JOIN sources
    freshness           opportunities.last_seen_at against the request clock

An unknown value is never a guessed one. A dimension that is NULL, UNKNOWN or
AMBIGUOUS keeps its posting in the listing while that dimension is not
filtered, and never matches once it is. No classifier, resolver or extractor is
reachable from this module, and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Sequence

from pydantic import BaseModel

from services.api.fine_classification import decode_fine_classification
from services.api.link_priority import (
    SourceObservation,
    preferred_link,
    select_original_url,
)
from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_readonly_database
from services.collector.logging_config import get_logger
from services.collector.qualification.fine_taxonomy import FineCategory
from services.digital_twin.preferences.models import OpportunityType


LOGGER = get_logger("services.collector.api.explorer")
PUBLIC_EXPLORER_ERROR = "Explorer data is temporarily unavailable."

#: The Explorer universe: coarse Phase 8 qualifications that are Data/AI targets.
EXPLORER_QUALIFICATIONS = ("CORE_TARGET", "ADJACENT_TARGET")
DEFAULT_LIMIT = 24
MAX_LIMIT = 50


class ExplorerFreshness(StrEnum):
    """The closed freshness windows, measured on `opportunities.last_seen_at`."""

    LAST_24_HOURS = "24h"
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"


FRESHNESS_WINDOWS: dict[ExplorerFreshness, timedelta] = {
    ExplorerFreshness.LAST_24_HOURS: timedelta(hours=24),
    ExplorerFreshness.LAST_7_DAYS: timedelta(days=7),
    ExplorerFreshness.LAST_30_DAYS: timedelta(days=30),
}


class ExplorerApiReadError(RuntimeError):
    """Raised when Explorer data cannot safely be exposed."""


@dataclass(frozen=True)
class ExplorerQuery:
    """One validated Explorer request. `None` means the filter is not active."""

    country: str | None = None
    city: str | None = None
    opportunity_type: OpportunityType | None = None
    domain: FineCategory | None = None
    source: str | None = None
    freshness: ExplorerFreshness | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0


class ExplorerResolvedLocationResponse(BaseModel):
    country_code: str
    city_key: str | None


class ExplorerSourceResponse(BaseModel):
    source_id: str
    source_type: str


class ExplorerItemResponse(BaseModel):
    opportunity_id: int
    canonical_title: str
    organization: str
    #: The collected location string, exactly as stored. Never parsed here.
    raw_location: str | None
    original_url: str
    last_seen_at: str
    #: Persisted Phase 8 value. `None` is not `OTHER`.
    fine_primary_category: FineCategory | None
    #: Persisted Phase 3.5 value. `None` means not asserted, never a default.
    opportunity_type: OpportunityType | None
    #: Only RESOLVED persisted resolutions; UNKNOWN/AMBIGUOUS are never shown.
    resolved_locations: list[ExplorerResolvedLocationResponse]
    has_unresolved_location: bool
    sources: list[ExplorerSourceResponse]


class ExplorerCityOptionResponse(BaseModel):
    country_code: str
    city_key: str


class ExplorerAvailableFiltersResponse(BaseModel):
    countries: list[str]
    cities: list[ExplorerCityOptionResponse]
    opportunity_types: list[OpportunityType]
    domains: list[FineCategory]
    sources: list[ExplorerSourceResponse]


class ExplorerResponse(BaseModel):
    items: list[ExplorerItemResponse]
    returned: int
    total: int
    limit: int
    offset: int
    available_filters: ExplorerAvailableFiltersResponse


def _utc_now() -> datetime:
    """The request clock; a seam so tests can hold time still."""
    return datetime.now(timezone.utc)


#: The Explorer base population. The two joins are on primary keys
#: (`opportunity_qualifications.opportunity_id`, `opportunity_constraints.
#: opportunity_id`), so they add columns and never multiply a posting; the
#: constraints join is LEFT so a posting without a constraints row stays.
_BASE_TABLES = """
FROM opportunities AS o
JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
LEFT JOIN opportunity_constraints AS k ON k.opportunity_id = o.id
"""
_BASE_WHERE = "WHERE o.status = ? AND o.is_active = ? AND q.qualification IN (?, ?)"
_BASE_PARAMETERS: tuple[Any, ...] = ("visible", 1, *EXPLORER_QUALIFICATIONS)


def _base(extra_joins: str = "") -> str:
    return f"{_BASE_TABLES} {extra_joins} {_BASE_WHERE}"


def _filtered_from(query: ExplorerQuery, now: Callable[[], datetime]) -> tuple[str, list[Any]]:
    """The base population narrowed by the active filters, parameterized.

    Multi-valued relationships are tested with EXISTS, so several matching
    location rows or source observations can never repeat a posting.
    """
    clauses: list[str] = []
    parameters: list[Any] = list(_BASE_PARAMETERS)
    if query.country is not None or query.city is not None:
        # One EXISTS: a combined country+city filter must be satisfied by the
        # same persisted resolution row, never by two unrelated ones.
        location = [
            "r.opportunity_id = o.id",
            "r.resolution_status = ?",
        ]
        parameters.append("RESOLVED")
        if query.country is not None:
            location.append("r.country_code = ?")
            parameters.append(query.country)
        if query.city is not None:
            location.append("r.city_key = ?")
            parameters.append(query.city)
        clauses.append(
            "EXISTS (SELECT 1 FROM opportunity_location_resolutions AS r WHERE "
            + " AND ".join(location)
            + ")"
        )
    if query.opportunity_type is not None:
        clauses.append("k.opportunity_type = ?")
        parameters.append(query.opportunity_type.value)
    if query.domain is not None:
        clauses.append("q.fine_primary_category = ?")
        parameters.append(query.domain.value)
    if query.source is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM opportunity_sources AS os"
            " JOIN sources AS s ON s.id = os.source_id"
            " WHERE os.opportunity_id = o.id AND os.source_id = ?)"
        )
        parameters.append(query.source)
    if query.freshness is not None:
        threshold = now() - FRESHNESS_WINDOWS[query.freshness]
        # julianday() is NULL for an unparsable timestamp, and NULL >= x is not
        # true: such a posting cannot match an active freshness filter.
        clauses.append("julianday(o.last_seen_at) >= julianday(?)")
        parameters.append(threshold.isoformat())
    sql = _base() + "".join(f" AND {clause}" for clause in clauses)
    return sql, parameters


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _locations(
    connection: Any, ids: list[int]
) -> tuple[dict[int, list[ExplorerResolvedLocationResponse]], set[int]]:
    """RESOLVED locations per posting, and the postings with anything unresolved."""
    resolved: dict[int, list[ExplorerResolvedLocationResponse]] = {}
    seen: dict[int, set[tuple[str, str | None]]] = {}
    with_rows: set[int] = set()
    unresolved: set[int] = set()
    rows = connection.execute(
        f"""SELECT opportunity_id, resolution_status, country_code, city_key
        FROM opportunity_location_resolutions
        WHERE opportunity_id IN ({_placeholders(ids)})
        ORDER BY opportunity_id, source_location_id, segment_position, id""",
        tuple(ids),
    ).fetchall()
    for opportunity_id, status, country_code, city_key in rows:
        opportunity_id = int(opportunity_id)
        with_rows.add(opportunity_id)
        if status != "RESOLVED" or country_code is None:
            unresolved.add(opportunity_id)
            continue
        pair = (country_code, city_key)
        if pair in seen.setdefault(opportunity_id, set()):
            continue
        seen[opportunity_id].add(pair)
        resolved.setdefault(opportunity_id, []).append(
            ExplorerResolvedLocationResponse(country_code=country_code, city_key=city_key)
        )
    unresolved.update(set(ids) - with_rows)
    return resolved, unresolved


def _observations(
    connection: Any, ids: list[int]
) -> tuple[dict[int, list[SourceObservation]], dict[int, list[ExplorerSourceResponse]]]:
    """Persisted source observations per posting, and one entry per source_id."""
    observations: dict[int, list[SourceObservation]] = {}
    sources: dict[int, dict[str, str]] = {}
    rows = connection.execute(
        f"""SELECT opportunity_sources.opportunity_id, opportunity_sources.id,
        opportunity_sources.source_id, sources.type,
        opportunity_sources.application_url, opportunity_sources.source_url,
        opportunity_sources.canonical_url
        FROM opportunity_sources JOIN sources
        ON sources.id = opportunity_sources.source_id
        WHERE opportunity_sources.opportunity_id IN ({_placeholders(ids)})
        ORDER BY opportunity_sources.opportunity_id, opportunity_sources.id""",
        tuple(ids),
    ).fetchall()
    for opportunity_id, observation_id, source_id, source_type, application, source_url, canonical in rows:
        opportunity_id = int(opportunity_id)
        observations.setdefault(opportunity_id, []).append(
            SourceObservation(
                observation_id=int(observation_id),
                source_type=source_type,
                application_url=application,
                source_url=source_url,
                canonical_url=canonical,
            )
        )
        sources.setdefault(opportunity_id, {})[source_id] = source_type
    by_source_id = {
        opportunity_id: [
            ExplorerSourceResponse(source_id=source_id, source_type=entries[source_id])
            for source_id in sorted(entries)
        ]
        for opportunity_id, entries in sources.items()
    }
    return observations, by_source_id


def _items(connection: Any, query: ExplorerQuery, now: Callable[[], datetime]) -> tuple[list[ExplorerItemResponse], int]:
    filtered_from, parameters = _filtered_from(query, now)
    total = int(connection.execute(f"SELECT COUNT(*) {filtered_from}", tuple(parameters)).fetchone()[0])
    rows = connection.execute(
        f"""SELECT o.id, o.canonical_title, o.organization, o.location,
        o.last_seen_at, o.source_url, o.application_url, o.canonical_url,
        q.qualification, q.fine_primary_category, q.fine_secondary_categories_json,
        q.fine_category_evidence_json, q.fine_reasons_json, q.fine_classifier_version,
        k.opportunity_type
        {filtered_from}
        ORDER BY o.last_seen_at DESC, o.id DESC
        LIMIT ? OFFSET ?""",
        (*parameters, query.limit, query.offset),
    ).fetchall()
    ids = [int(row[0]) for row in rows]
    if not ids:
        return [], total
    resolved, unresolved = _locations(connection, ids)
    observations, sources = _observations(connection, ids)
    items = []
    for row in rows:
        opportunity_id = int(row[0])
        fallback = preferred_link(row[6], row[5], row[7]) or row[5]
        # The existing Phase 8 decoder refuses an incoherent persisted row; the
        # category is read, never computed.
        fine = decode_fine_classification(row[8], row[9], row[10], row[11], row[12], row[13])
        items.append(
            ExplorerItemResponse(
                opportunity_id=opportunity_id,
                canonical_title=row[1],
                organization=row[2],
                raw_location=row[3],
                original_url=select_original_url(
                    observations.get(opportunity_id, ()), fallback=fallback
                ),
                last_seen_at=row[4],
                fine_primary_category=fine.primary_category,
                opportunity_type=row[14],
                resolved_locations=resolved.get(opportunity_id, []),
                has_unresolved_location=opportunity_id in unresolved,
                sources=sources.get(opportunity_id, []),
            )
        )
    return items, total


_LOCATION_JOIN = "JOIN opportunity_location_resolutions AS r ON r.opportunity_id = o.id"
_SOURCE_JOIN = (
    "JOIN opportunity_sources AS os ON os.opportunity_id = o.id "
    "JOIN sources AS s ON s.id = os.source_id"
)


def _available_filters(connection: Any) -> ExplorerAvailableFiltersResponse:
    """Every persisted, known option of the whole Explorer base population.

    Deliberately blind to the active filters and to pagination: it describes the
    universe, not the current page. NULL and unresolved values are not options.
    """

    def distinct(select: str, joins: str, condition: str, order: str, *extra: Any) -> list[Any]:
        return connection.execute(
            f"SELECT DISTINCT {select} {_base(joins)} {condition} ORDER BY {order}",
            (*_BASE_PARAMETERS, *extra),
        ).fetchall()

    countries = distinct(
        "r.country_code", _LOCATION_JOIN,
        "AND r.resolution_status = ? AND r.country_code IS NOT NULL",
        "r.country_code", "RESOLVED",
    )
    cities = distinct(
        "r.country_code, r.city_key", _LOCATION_JOIN,
        "AND r.resolution_status = ? AND r.country_code IS NOT NULL AND r.city_key IS NOT NULL",
        "r.country_code, r.city_key", "RESOLVED",
    )
    types = distinct("k.opportunity_type", "", "AND k.opportunity_type IS NOT NULL", "k.opportunity_type")
    domains = distinct(
        "q.fine_primary_category", "", "AND q.fine_primary_category IS NOT NULL", "q.fine_primary_category"
    )
    sources = distinct("os.source_id, s.type", _SOURCE_JOIN, "", "os.source_id, s.type")
    return ExplorerAvailableFiltersResponse(
        countries=[row[0] for row in countries],
        cities=[ExplorerCityOptionResponse(country_code=row[0], city_key=row[1]) for row in cities],
        opportunity_types=[row[0] for row in types],
        domains=[row[0] for row in domains],
        sources=[ExplorerSourceResponse(source_id=row[0], source_type=row[1]) for row in sources],
    )


def _read_one_snapshot(
    connection: Any, query: ExplorerQuery, now: Callable[[], datetime]
) -> ExplorerResponse:
    """Read the page, the total and the options as one picture, then release it.

    A plain deferred `BEGIN` takes no write lock, and `ROLLBACK` is the one
    ending that cannot write. A transaction the connection already holds is
    borrowed and left alone.
    """
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        items, total = _items(connection, query, now)
        return ExplorerResponse(
            items=items,
            returned=len(items),
            total=total,
            limit=query.limit,
            offset=query.offset,
            available_filters=_available_filters(connection),
        )
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.execute("ROLLBACK")


def read_explorer_surface(query: ExplorerQuery) -> ExplorerResponse:
    """Read the persisted Explorer listing for one validated query, read-only."""
    connection = None
    try:
        if not 1 <= query.limit <= MAX_LIMIT or query.offset < 0:
            raise ExplorerApiReadError(PUBLIC_EXPLORER_ERROR)
        settings = load_settings()
        # `mode=ro` opens an existing file only: a GET never creates a database
        # or its directory, even before `query_only` is set.
        if (
            settings.database_backend is not DatabaseBackend.SQLITE
            or settings.sqlite_database_path is None
        ):
            raise ExplorerApiReadError(PUBLIC_EXPLORER_ERROR)
        connection = connect_readonly_database(settings.sqlite_database_path)
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise ExplorerApiReadError(PUBLIC_EXPLORER_ERROR)
        response = _read_one_snapshot(connection, query, _utc_now)
        LOGGER.info(
            "Explorer API request succeeded.",
            extra={
                "event": "explorer_api_request_succeeded",
                "items_returned": response.returned,
                "total": response.total,
            },
        )
        return response
    except ExplorerApiReadError:
        LOGGER.error(
            "Explorer API request refused.",
            extra={"event": "explorer_api_request_refused"},
        )
        raise
    except Exception as error:
        LOGGER.error(
            "Explorer API request failed.",
            extra={
                "event": "explorer_api_request_failed",
                "error_type": type(error).__name__,
            },
        )
        raise ExplorerApiReadError(PUBLIC_EXPLORER_ERROR) from None
    finally:
        if connection is not None:
            connection.close()
