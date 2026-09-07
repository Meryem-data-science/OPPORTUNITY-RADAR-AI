"""Transactional SQLite persistence for versioned opportunity qualification."""

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from typing import Callable

from services.collector.config import DatabaseBackend, Settings
from services.collector.database.connection import connect_configured_database

from .classifier import CLASSIFIER_VERSION, Classification, classify_opportunity
from .fine_classifier import (
    FINE_CLASSIFIER_VERSION, FineClassification, FineEvidence, classify_fine_categories,
)


class QualificationPersistenceError(RuntimeError):
    """Raised when qualification persistence cannot safely proceed."""


@dataclass(frozen=True)
class PersistenceSummary:
    created: int
    updated: int
    unchanged: int
    total: int


def persist_configured_qualifications(settings: Settings) -> PersistenceSummary:
    """Reconcile qualifications in the configured operational SQLite database."""
    if settings.database_backend is DatabaseBackend.TURSO:
        raise QualificationPersistenceError(
            "Remote Turso qualification writes are disabled in this phase"
        )
    connection = connect_configured_database(settings)
    try:
        return persist_qualifications(connection)
    finally:
        connection.close()


_INPUT_FIELDS = (
    "canonical_title", "description", "source_url", "application_url", "canonical_url",
)

#: The columns migration 0025 adds. Coarse and fine results are one derived
#: record, so persistence requires all of them before it writes anything.
_FINE_COLUMNS = (
    "fine_primary_category", "fine_secondary_categories_json", "fine_category_evidence_json",
    "fine_reasons_json", "fine_classifier_version",
)


def input_fingerprint(
    canonical_title: str | None,
    description: str | None,
    source_url: str | None,
    application_url: str | None,
    canonical_url: str | None,
) -> str:
    """Hash canonical UTF-8 JSON containing every persisted classifier input."""
    values = (canonical_title, description, source_url, application_url, canonical_url)
    payload = dict(zip(_INPUT_FIELDS, values, strict=True))
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _json(values: tuple[object, ...]) -> str:
    return json.dumps([str(value) for value in values], ensure_ascii=False, separators=(",", ":"))


def _evidence_json(evidence: tuple[FineEvidence, ...]) -> str:
    """Serialize fine evidence as ordered objects of enum values, never dataclass reprs."""
    return json.dumps(
        [
            {
                "category": str(item.category), "field": str(item.field),
                "kind": str(item.kind), "signal": item.signal,
            }
            for item in evidence
        ],
        ensure_ascii=False, separators=(",", ":"),
    )


def _persisted_values(result: Classification) -> tuple[str, ...]:
    return (
        str(result.qualification), str(result.primary_domain), str(result.opportunity_type),
        str(result.employment_type), str(result.listing_quality), _json(result.matched_domains),
        _json(result.matched_title_signals), _json(result.matched_description_signals),
        _json(result.matched_exclusion_signals), _json(result.reasons),
    )


def _persisted_fine_values(result: FineClassification) -> tuple[str | None, str, str, str]:
    """A NULL primary category with written JSON means "classified, no category"."""
    return (
        None if result.primary_category is None else str(result.primary_category),
        _json(result.secondary_categories), _evidence_json(result.evidence), _json(result.reasons),
    )


def _require_schema(connection: sqlite3.Connection) -> None:
    """Refuse to start unless every column this contract writes already exists.

    Migration application stays an explicit separate action, so the only thing
    persistence does about a missing migration is name it. Without this check a
    database migrated to 0004 but not 0025 would fail mid-transaction with a raw
    ``no such column`` error, which is a stack trace rather than an instruction.
    """
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_qualifications'"
    ).fetchone() is None:
        raise QualificationPersistenceError(
            "migration 0004 is required; explicitly apply migrations before persistence"
        )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(opportunity_qualifications)")
    }
    missing = tuple(column for column in _FINE_COLUMNS if column not in columns)
    if missing:
        raise QualificationPersistenceError(
            "migration 0025 is required; opportunity_qualifications is missing "
            f"{', '.join(missing)}; explicitly apply migrations before persistence"
        )


def persist_qualifications(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    classifier_version: str = CLASSIFIER_VERSION,
    classifier: Callable[..., Classification] = classify_opportunity,
    fine_classifier_version: str = FINE_CLASSIFIER_VERSION,
    fine_classifier: Callable[..., FineClassification] = classify_fine_categories,
) -> PersistenceSummary:
    """Classify eligible rows and atomically insert/update their current result.

    The coarse and fine classifiers are injected and versioned independently:
    they are two rule systems over the same inputs, and coupling their versions
    would make a fine recalibration claim the coarse rules had changed.
    """
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    if not classifier_version:
        raise ValueError("classifier_version must not be empty")
    if not fine_classifier_version:
        raise ValueError("fine_classifier_version must not be empty")
    _require_schema(connection)

    sql = """
        SELECT id, canonical_title, description, source_url, application_url, canonical_url
        FROM opportunities
        WHERE is_active = 1 AND status != 'merged_duplicate'
        ORDER BY id
    """
    parameters: tuple[int, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        parameters = (limit,)
    rows = connection.execute(sql, parameters).fetchall()
    created = updated = unchanged = 0
    timestamp = datetime.now(UTC).isoformat(timespec="microseconds")

    connection.execute("BEGIN")
    try:
        for row in rows:
            opportunity_id, title, description, source_url, application_url, canonical_url = row
            fingerprint = input_fingerprint(title, description, source_url, application_url, canonical_url)
            existing = connection.execute(
                """SELECT input_fingerprint, classifier_version, fine_classifier_version
                   FROM opportunity_qualifications WHERE opportunity_id = ?""",
                (opportunity_id,),
            ).fetchone()
            # A row is current only when both rule systems that wrote it are the
            # ones running now. A NULL fine version is a row migration 0025
            # reached and fine classification never did, so it is reconciled.
            if existing == (fingerprint, classifier_version, fine_classifier_version):
                unchanged += 1
                continue
            result = classifier(
                title, description, source_url=source_url, application_url=application_url,
                canonical_url=canonical_url,
            )
            fine_result = fine_classifier(title, description, qualification=result.qualification)
            values = _persisted_values(result) + _persisted_fine_values(fine_result)
            if existing is None:
                connection.execute(
                    """INSERT INTO opportunity_qualifications (
                        opportunity_id, qualification, primary_domain, opportunity_type,
                        employment_type, listing_quality, matched_domains_json,
                        matched_title_signals_json, matched_description_signals_json,
                        matched_exclusion_signals_json, reasons_json, fine_primary_category,
                        fine_secondary_categories_json, fine_category_evidence_json,
                        fine_reasons_json, classifier_version, fine_classifier_version,
                        input_fingerprint, classified_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (opportunity_id, *values, classifier_version, fine_classifier_version,
                     fingerprint, timestamp, timestamp, timestamp),
                )
                created += 1
            else:
                connection.execute(
                    """UPDATE opportunity_qualifications SET
                        qualification = ?, primary_domain = ?, opportunity_type = ?,
                        employment_type = ?, listing_quality = ?, matched_domains_json = ?,
                        matched_title_signals_json = ?, matched_description_signals_json = ?,
                        matched_exclusion_signals_json = ?, reasons_json = ?,
                        fine_primary_category = ?, fine_secondary_categories_json = ?,
                        fine_category_evidence_json = ?, fine_reasons_json = ?,
                        classifier_version = ?, fine_classifier_version = ?,
                        input_fingerprint = ?, classified_at = ?, updated_at = ?
                    WHERE opportunity_id = ?""",
                    (*values, classifier_version, fine_classifier_version, fingerprint,
                     timestamp, timestamp, opportunity_id),
                )
                updated += 1
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return PersistenceSummary(created, updated, unchanged, len(rows))
