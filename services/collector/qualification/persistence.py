"""Transactional SQLite persistence for versioned opportunity qualification."""

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import sqlite3
from typing import Callable

from .classifier import CLASSIFIER_VERSION, Classification, classify_opportunity


class QualificationPersistenceError(RuntimeError):
    """Raised when qualification persistence cannot safely proceed."""


@dataclass(frozen=True)
class PersistenceSummary:
    created: int
    updated: int
    unchanged: int
    total: int


_INPUT_FIELDS = (
    "canonical_title", "description", "source_url", "application_url", "canonical_url",
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


def _persisted_values(result: Classification) -> tuple[str, ...]:
    return (
        str(result.qualification), str(result.primary_domain), str(result.opportunity_type),
        str(result.employment_type), str(result.listing_quality), _json(result.matched_domains),
        _json(result.matched_title_signals), _json(result.matched_description_signals),
        _json(result.matched_exclusion_signals), _json(result.reasons),
    )


def persist_qualifications(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
    classifier_version: str = CLASSIFIER_VERSION,
    classifier: Callable[..., Classification] = classify_opportunity,
) -> PersistenceSummary:
    """Classify eligible rows and atomically insert/update their current result."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be positive")
    if not classifier_version:
        raise ValueError("classifier_version must not be empty")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunity_qualifications'"
    ).fetchone() is None:
        raise QualificationPersistenceError(
            "migration 0004 is required; explicitly apply migrations before persistence"
        )

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
                "SELECT input_fingerprint, classifier_version FROM opportunity_qualifications WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
            if existing == (fingerprint, classifier_version):
                unchanged += 1
                continue
            result = classifier(
                title, description, source_url=source_url, application_url=application_url,
                canonical_url=canonical_url,
            )
            values = _persisted_values(result)
            if existing is None:
                connection.execute(
                    """INSERT INTO opportunity_qualifications (
                        opportunity_id, qualification, primary_domain, opportunity_type,
                        employment_type, listing_quality, matched_domains_json,
                        matched_title_signals_json, matched_description_signals_json,
                        matched_exclusion_signals_json, reasons_json, classifier_version,
                        input_fingerprint, classified_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (opportunity_id, *values, classifier_version, fingerprint, timestamp, timestamp, timestamp),
                )
                created += 1
            else:
                connection.execute(
                    """UPDATE opportunity_qualifications SET
                        qualification = ?, primary_domain = ?, opportunity_type = ?,
                        employment_type = ?, listing_quality = ?, matched_domains_json = ?,
                        matched_title_signals_json = ?, matched_description_signals_json = ?,
                        matched_exclusion_signals_json = ?, reasons_json = ?, classifier_version = ?,
                        input_fingerprint = ?, classified_at = ?, updated_at = ?
                    WHERE opportunity_id = ?""",
                    (*values, classifier_version, fingerprint, timestamp, timestamp, opportunity_id),
                )
                updated += 1
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return PersistenceSummary(created, updated, unchanged, len(rows))
