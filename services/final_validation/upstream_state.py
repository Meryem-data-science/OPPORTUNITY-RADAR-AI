"""Phase 11.2B: read-only evidence about the persisted upstream chain.

Phase 11.2A proves the chain from the current Matching run downwards. This
module proves the part above it, and stops at the Matching boundary:

    Digital Twin -> Profile Targeting -> Collection / Source State
    -> Persisted Dedup State -> Persisted Phase 8 Classification
    -> current Matching run's upstream identities

It answers one question: *is the upstream state already stored for this profile
coherent, traceable and real, according to the owners that stored it?* It never
asks whether that state is fresh against the sources, whether a classification
is right, or whether a posting should be in Matching.

Every domain statement is read from an official owner:

    Digital Twin    facts / provenance / structured / skills / preferences repositories
    Targeting       resolve_profile_target, resolve_profile_type_target (pure
                    derivations over the persisted profile projections; no
                    target is persisted anywhere, and no per-posting verdict is
                    computed here)
    Collection      load_source_registry, read_source_health
    Dedup           list_decisions, list_merges, MERGED_DUPLICATE_STATUS
    Phase 8         decode_fine_classification, the taxonomy vocabularies,
                    input_fingerprint and the classifier version constants
    Matching        read_current_matching
    Scope           in_scope_opportunity_ids (the shared "active, not a merged
                    duplicate" filter)

Direct read-only SQL is used only where no owner read model exists, and only for
structure: row coverage, link presence, orphan counts, and the raw persisted
Phase 8 columns that the owner decoder and vocabularies then judge. None of it
restates a business decision.

Nothing here classifies, resolves per posting, deduplicates, collects, matches,
recommends, synchronizes, migrates, repairs or writes. The file is opened through
the project's `mode=ro` helper, `query_only` is confirmed, every read happens in
one `BEGIN` snapshot released with `ROLLBACK`, and the file and its possible
side files are fingerprinted before and after (the Phase 11.2A pattern, reused).

Each check keeps independent dimensions and never collapses them:

    integrity         PASS | FAIL
    demonstrability   DEMONSTRATED | NOT_DEMONSTRATED | NOT_ASSESSED
    operational_state an owner state (READY, STALE, NEVER_RUN, UNKNOWN, ...) or None
    severity          BLOCKER | WARNING | INFORMATIONAL, presentation only

The only aggregate is `integrity_result`: PASS when every check's integrity
passed. There is no score. The evidence carries no name, email, phone, URL,
CV text, CV hash, free-text note, source error message or absolute path.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from services.collector.database.connection import connect_readonly_database
from services.collector.database.source_health import read_source_health
from services.collector.deduplication.decisions import DecisionError, list_decisions
from services.collector.deduplication.merges import APPLIED, MERGED_DUPLICATE_STATUS, list_merges
from services.collector.matching.read_model import MatchingReadError, read_current_matching
from services.collector.qualification.classifier import CLASSIFIER_VERSION
from services.collector.qualification.fine_classifier import FINE_CLASSIFIER_VERSION
from services.collector.qualification.fine_read_model import (
    FineClassificationDecodeError,
    decode_fine_classification,
)
from services.collector.qualification.persistence import input_fingerprint
from services.collector.qualification.taxonomy import (
    Domain,
    EmploymentType,
    ListingQuality,
    OpportunityType as QualificationOpportunityType,
    Qualification,
)
from services.collector.sources import (
    DEFAULT_SOURCE_REGISTRY,
    SourceConfigurationError,
    load_source_registry,
)
from services.digital_twin.facts.models import FactSourceType, FactStatus
from services.digital_twin.facts.repository import (
    ProfileFactError,
    list_profile_fact_provenance,
    list_profile_facts,
)
from services.digital_twin.preferences.repository import (
    ProfilePreferenceError,
    get_profile_availability,
    get_profile_career_objectives,
    get_profile_mobility,
    get_profile_preferences,
    summarize_profile_preferences,
)
from services.digital_twin.skills.repository import ProfileSkillError, list_profile_skills
from services.digital_twin.structured_profile.models import StructuredProfileError
from services.digital_twin.structured_profile.repository import (
    list_profile_certifications,
    list_profile_educations,
    list_profile_experiences,
    list_profile_languages,
    list_profile_projects,
)
from services.final_validation.operational_state import (
    FAIL,
    PASS,
    OperationalStateError,
    database_unchanged,
    fingerprint_database,
    parse_profile_id,
    require_profile_id,
    serialize_evidence,
)
from services.geography.profile_target import resolve_profile_target
from services.geography.service import in_scope_opportunity_ids
from services.targeting.opportunity_type.profile_target import resolve_profile_type_target


SCHEMA_VERSION = "phase11.2b-upstream-chain-v1"

DEMONSTRATED = "DEMONSTRATED"
NOT_DEMONSTRATED = "NOT_DEMONSTRATED"
NOT_ASSESSED = "NOT_ASSESSED"

READY = "READY"
STALE = "STALE"
NEVER_RUN = "NEVER_RUN"
UNKNOWN = "UNKNOWN"

BLOCKER = "BLOCKER"
WARNING = "WARNING"
INFORMATIONAL = "INFORMATIONAL"

CHECK_ORDER = (
    "SQLITE_QUERY_ONLY",
    "DIGITAL_TWIN_FACT_PROVENANCE",
    "DIGITAL_TWIN_PROJECTIONS",
    "PROFILE_TARGETING",
    "COLLECTION_SOURCES",
    "COLLECTION_OPPORTUNITY_TRACEABILITY",
    "DEDUP_PERSISTED_STATE",
    "PHASE8_QUALIFICATION_COVERAGE",
    "PHASE8_PERSISTED_DECODING",
    "PHASE8_INPUT_CURRENCY",
    "MATCHING_UPSTREAM_BOUNDARY",
    "DATABASE_UNCHANGED",
)

#: Questions this validator deliberately does not answer, each because the only
#: way to answer it would be to recompute, contact a network or invent a rule.
NOT_ASSESSED_DIMENSIONS = (
    ("COLLECTION_FRESHNESS", "NO_OWNER_FRESHNESS_THRESHOLD"),
    ("SOURCE_NETWORK_LIVENESS", "NETWORK_ACCESS_OUT_OF_SCOPE"),
    ("DEDUP_CANDIDATE_DETECTION", "WOULD_RECOMPUTE_SIMILARITY"),
    ("PHASE8_CLASSIFICATION_CORRECTNESS", "WOULD_RECLASSIFY"),
    ("PROFILE_TARGET_VERDICTS_PER_OPPORTUNITY", "WOULD_COMPUTE_TARGETING_VERDICTS"),
    ("MATCHING_COHORT_MEMBERSHIP_FRESHNESS", "WOULD_RECOMPUTE_MATCHING_SELECTION"),
    ("MATCHING_ASSESSMENT_RECOMPUTATION", "WOULD_RECOMPUTE_MATCHING"),
)

#: How many offending opportunity ids a check quotes. The count is always given.
IDS_REPORTED = 20

_FACT_ERRORS = (ProfileFactError, ValueError)
_PROJECTION_ERRORS = (
    ProfilePreferenceError, ProfileSkillError, StructuredProfileError, ValueError, TypeError, KeyError,
)
_TARGETING_ERRORS = (ProfilePreferenceError, ValueError, TypeError, KeyError)
_COARSE_VOCABULARIES = (
    ("qualification", Qualification),
    ("primary_domain", Domain),
    ("opportunity_type", QualificationOpportunityType),
    ("employment_type", EmploymentType),
    ("listing_quality", ListingQuality),
)
_COARSE_JSON_COLUMNS = (
    "matched_domains_json",
    "matched_title_signals_json",
    "matched_description_signals_json",
    "matched_exclusion_signals_json",
    "reasons_json",
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _counts(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _ids(values: Iterable[int]) -> list[int]:
    return sorted(values)[:IDS_REPORTED]


def _value(item: Any) -> Any:
    return getattr(item, "value", item)


def _unavailable(error: BaseException) -> dict[str, Any]:
    """An owner refusal, reduced to its type name: never its message."""
    return {"available": False, "error_code": type(error).__name__}


def _parse_timestamp(raw: object) -> datetime | None:
    """A persisted timestamp as an aware-or-naive datetime, or None if unreadable."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Digital Twin
# --------------------------------------------------------------------------


def digital_twin_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    """Fact lifecycle and provenance coherence, as counts. Never a value."""
    user = connection.execute(
        "SELECT 1 FROM profiles AS p JOIN users AS u ON u.id = p.user_id WHERE p.id = ?", (profile_id,)
    ).fetchone()
    try:
        facts = list_profile_facts(connection, profile_id)
        provenance = {fact.id: list_profile_fact_provenance(connection, profile_id, fact.id) for fact in facts}
    except _FACT_ERRORS as error:
        return {**_unavailable(error), "owner_user_present": user is not None}

    fact_ids = {fact.id for fact in facts}
    without_provenance = [fact for fact in facts if not provenance[fact.id]]
    corrected_dangling = [
        fact for fact in facts
        if fact.status is FactStatus.CORRECTED and fact.replaced_by_fact_id not in fact_ids
    ]
    rows = [row for rows in provenance.values() for row in rows]
    cv_rows = [row for row in rows if row.source_type is FactSourceType.CV]
    # The digest is used for a distinct count and never leaves this function.
    distinct_cv_fingerprints = {row.cv_sha256 for row in cv_rows if row.cv_sha256 is not None}
    accepted_without = sum(1 for fact in without_provenance if fact.status is FactStatus.ACCEPTED)
    return {
        "available": True,
        "error_code": None,
        "owner_user_present": user is not None,
        "fact_count": len(facts),
        "fact_status_counts": _counts(fact.status.value for fact in facts),
        "fact_type_counts": _counts(fact.fact_type for fact in facts),
        "provenance_count": len(rows),
        "provenance_source_type_counts": _counts(row.source_type.value for row in rows),
        "facts_without_provenance_by_status": _counts(fact.status.value for fact in without_provenance),
        "accepted_facts_without_provenance": accepted_without,
        "accepted_fact_provenance_consistent": accepted_without == 0,
        "corrected_facts_without_resolvable_replacement": len(corrected_dangling),
        "distinct_cv_fingerprint_count": len(distinct_cv_fingerprints),
        "cv_provenance_without_fingerprint_count": sum(1 for row in cv_rows if row.cv_sha256 is None),
    }


def projection_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    """The persisted Digital Twin projections and whether their facts still justify them.

    The owners' synchronizations project only ACCEPTED facts, and the four
    explicit-input singletons only facts carrying USER_INPUT provenance. A
    stored row whose fact no longer qualifies is a projection that has not been
    synchronized since: reported, never repaired.
    """
    try:
        facts = {fact.id: fact for fact in list_profile_facts(connection, profile_id)}
        user_input_fact_ids = {
            fact_id for fact_id in facts
            if any(row.source_type is FactSourceType.USER_INPUT
                   for row in list_profile_fact_provenance(connection, profile_id, fact_id))
        }
        structured = {
            "experiences": list_profile_experiences(connection, profile_id),
            "projects": list_profile_projects(connection, profile_id),
            "educations": list_profile_educations(connection, profile_id),
            "certifications": list_profile_certifications(connection, profile_id),
            "languages": list_profile_languages(connection, profile_id),
        }
        skills = list_profile_skills(connection, profile_id)
        singletons = {
            "availability": get_profile_availability(connection, profile_id),
            "mobility": get_profile_mobility(connection, profile_id),
            "preferences": get_profile_preferences(connection, profile_id),
            "career_objectives": get_profile_career_objectives(connection, profile_id),
        }
        summary = summarize_profile_preferences(connection, profile_id).as_dict()
    except _PROJECTION_ERRORS + (ProfileFactError,) as error:
        return _unavailable(error)

    def accepted(fact_id: int) -> bool:
        fact = facts.get(fact_id)
        return fact is not None and fact.status is FactStatus.ACCEPTED

    structured_part = {
        name: {"row_count": len(rows), "rows_not_backed_by_accepted_fact": sum(1 for row in rows if not accepted(row.fact_id))}
        for name, rows in structured.items()
    }
    evidence_rows = [item for skill in skills for item in skill.evidence]
    singleton_part = {
        name: {
            "stated": row is not None,
            "backed_by_accepted_user_input_fact": None if row is None else (
                accepted(row.fact_id) and row.fact_id in user_input_fact_ids
            ),
        }
        for name, row in singletons.items()
    }
    unbacked = (
        sum(part["rows_not_backed_by_accepted_fact"] for part in structured_part.values())
        + sum(1 for item in evidence_rows if not accepted(item.fact_id))
        + sum(1 for part in singleton_part.values() if part["backed_by_accepted_user_input_fact"] is False)
    )
    return {
        "available": True,
        "error_code": None,
        "structured": structured_part,
        "skills": {
            "skill_count": len(skills),
            "evidence_count": len(evidence_rows),
            "evidence_not_backed_by_accepted_fact": sum(1 for item in evidence_rows if not accepted(item.fact_id)),
        },
        "explicit_input": singleton_part,
        # Flags and counts from the owner's own privacy-safe summary. A preferred
        # domain is counted, never named.
        "explicit_input_summary": summary,
        "unbacked_projection_row_count": unbacked,
    }


# --------------------------------------------------------------------------
# Profile targeting
# --------------------------------------------------------------------------


def targeting_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    """What the targeting owners derive from the persisted profile projections.

    No target is persisted: both owners derive one on read, purely, from
    `profile_mobility` and `profile_preferences`. That is what is quoted here.
    The owners' audits — which compute a verdict per posting — are not called.
    """
    try:
        geography = resolve_profile_target(connection, profile_id)
        types = resolve_profile_type_target(connection, profile_id)
        mobility = get_profile_mobility(connection, profile_id)
    except _TARGETING_ERRORS as error:
        return _unavailable(error)
    return {
        "available": True,
        "error_code": None,
        "derivation": "DERIVED_ON_READ_FROM_PERSISTED_PROFILE_PROJECTIONS",
        "persisted_target_exists": False,
        "geography": {
            "known": geography.country_code is not None,
            "country_code": geography.country_code,
            "rule_id": geography.rule_id,
            "mobility_stated": mobility is not None,
            "mobility_scope": None if mobility is None else _value(mobility.value.scope),
            "mobility_location_count": 0 if mobility is None else len(mobility.value.locations),
        },
        "opportunity_type": {
            "known": bool(types.opportunity_types),
            "opportunity_types": [_value(item) for item in types.opportunity_types],
            "rule_id": types.rule_id,
        },
    }


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def collection_evidence(connection: sqlite3.Connection, registry_path: Path) -> dict[str, Any]:
    """Configured and persisted sources, and their persisted run history.

    A source's operational state is the owner's own: the status of its latest
    persisted run, or NEVER_RUN when there is none. No freshness is derived.
    Error messages are never read into the evidence.
    """
    try:
        configured = load_source_registry(registry_path)
    except SourceConfigurationError as error:
        return _unavailable(error)
    configured_by_id = {source.id: source for source in configured}
    persisted = {str(row[0]) for row in connection.execute("SELECT id FROM sources").fetchall()}
    run_rows = connection.execute("SELECT source_id, status, started_at, finished_at FROM source_runs").fetchall()
    health = read_source_health(connection, configured_sources=configured)

    runs_by_source: dict[str, list[tuple]] = {}
    for row in run_rows:
        runs_by_source.setdefault(str(row[0]), []).append(row)

    sources = []
    for item in health:
        config = configured_by_id.get(item.source_id)
        runs = runs_by_source.get(item.source_id, [])
        sources.append({
            "source_id": item.source_id,
            "configured": config is not None,
            "persisted": item.source_id in persisted,
            "enabled": item.enabled,
            "type": None if config is None else config.type,
            "country": None if config is None else config.country,
            "run_count": len(runs),
            "run_status_counts": _counts(run[1] for run in runs),
            "latest_run_status": item.status,
            "latest_run_started_at": item.last_run_at,
            "latest_items_found": item.items_found,
            "anomaly_code": item.anomaly_code,
            "operational_state": NEVER_RUN if not item.has_run else item.status,
        })

    parsed: list[tuple[datetime, str]] = []
    unparseable = 0
    for row in run_rows:
        for raw in (row[2], row[3]):
            if raw is None:
                continue
            moment = _parse_timestamp(raw)
            if moment is None or moment.tzinfo is None:
                unparseable += 1
            else:
                parsed.append((moment, str(raw)))
    newest = max(parsed)[1] if parsed else None
    return {
        "available": True,
        "error_code": None,
        "configured_source_count": len(configured),
        "persisted_source_count": len(persisted),
        "run_count": len(run_rows),
        "run_status_counts": _counts(row[1] for row in run_rows),
        "sources": sources,
        "configured_never_run_source_ids": sorted(
            entry["source_id"] for entry in sources if entry["configured"] and entry["operational_state"] == NEVER_RUN
        ),
        "persisted_not_configured_source_ids": sorted(persisted - set(configured_by_id)),
        "runs_without_persisted_source_count": sum(1 for row in run_rows if str(row[0]) not in persisted),
        "newest_run_timestamp_raw": newest,
        "unparseable_or_naive_run_timestamp_count": unparseable,
        "freshness_claim": NOT_ASSESSED,
    }


def traceability_evidence(connection: sqlite3.Connection, scope: tuple[int, ...]) -> dict[str, Any]:
    """Does every in-scope opportunity trace to a persisted source link?"""
    persisted = {str(row[0]) for row in connection.execute("SELECT id FROM sources").fetchall()}
    existing = {int(row[0]) for row in connection.execute("SELECT id FROM opportunities").fetchall()}
    links = connection.execute("SELECT opportunity_id, source_id FROM opportunity_sources").fetchall()
    scope_set = set(scope)
    linked = {int(row[0]) for row in links}
    untraced = scope_set - linked
    return {
        "in_scope_opportunity_count": len(scope),
        "source_link_count": len(links),
        "in_scope_links_by_source": _counts(str(row[1]) for row in links if int(row[0]) in scope_set),
        "in_scope_without_source_link_count": len(untraced),
        "in_scope_without_source_link_ids": _ids(untraced),
        "links_to_unpersisted_source_count": sum(1 for row in links if str(row[1]) not in persisted),
        "links_to_missing_opportunity_count": sum(1 for row in links if int(row[0]) not in existing),
    }


# --------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------


def dedup_evidence(connection: sqlite3.Connection, scope: tuple[int, ...]) -> dict[str, Any]:
    """The persisted dedup registry, read as stored. No similarity is computed."""
    try:
        decisions = list_decisions(connection)
    except (DecisionError, ValueError, TypeError) as error:
        return _unavailable(error)
    merges = list_merges(connection)
    moves = connection.execute("SELECT COUNT(*) FROM deduplication_merge_source_moves").fetchone()[0]
    opportunities = {
        int(row[0]): (row[1], row[2])
        for row in connection.execute("SELECT id, status, is_active FROM opportunities").fetchall()
    }
    applied = [merge for merge in merges if merge["status"] == APPLIED]
    applied_merged_ids = {int(merge["merged_opportunity_id"]) for merge in applied}
    incoherent_applied = [
        merge for merge in applied
        if opportunities.get(int(merge["merged_opportunity_id"])) != (MERGED_DUPLICATE_STATUS, 0)
    ]
    tombstones = {opportunity_id for opportunity_id, (status, _) in opportunities.items() if status == MERGED_DUPLICATE_STATUS}
    placeholders = ",".join("?" for _ in scope)
    duplicate_groups = 0 if not scope else connection.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT canonical_url FROM opportunities
                 WHERE id IN ({placeholders}) AND canonical_url IS NOT NULL
                 GROUP BY canonical_url HAVING COUNT(*) > 1)""",
        scope,
    ).fetchone()[0]
    return {
        "available": True,
        "error_code": None,
        "decision_count": len(decisions),
        "decision_status_counts": _counts(item.status for item in decisions),
        "merge_count": len(merges),
        "merge_status_counts": _counts(merge["status"] for merge in merges),
        "merge_source_move_count": int(moves),
        "applied_merge_count": len(applied),
        "applied_merges_with_incoherent_tombstone": len(incoherent_applied),
        "tombstones_without_applied_merge": len(tombstones - applied_merged_ids),
        # Exact equality of a persisted identity; no similarity is computed and no URL is quoted.
        "in_scope_duplicate_canonical_url_group_count": int(duplicate_groups),
        "end_to_end_merge_claim": DEMONSTRATED if applied and not incoherent_applied else NOT_DEMONSTRATED,
    }


# --------------------------------------------------------------------------
# Phase 8 persisted classification
# --------------------------------------------------------------------------


_PHASE8_SQL = f"""
    SELECT o.id, o.canonical_title, o.description, o.source_url, o.application_url, o.canonical_url,
           q.opportunity_id IS NOT NULL,
           q.qualification, q.primary_domain, q.opportunity_type, q.employment_type, q.listing_quality,
           {", ".join("q." + column for column in _COARSE_JSON_COLUMNS)},
           q.classifier_version, q.input_fingerprint,
           q.fine_primary_category, q.fine_secondary_categories_json, q.fine_category_evidence_json,
           q.fine_reasons_json, q.fine_classifier_version
      FROM opportunities AS o
      LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
     ORDER BY o.id
"""


@dataclass(frozen=True)
class QualificationRead:
    """One persisted qualification row, strictly decoded, or the reason it was refused."""

    opportunity_id: int
    present: bool
    malformed_code: str | None
    qualification: str | None = None
    primary_domain: str | None = None
    opportunity_type: str | None = None
    employment_type: str | None = None
    listing_quality: str | None = None
    classifier_version: str | None = None
    fine_classifier_version: str | None = None
    fine_state: str | None = None
    fine_primary_category: str | None = None
    fingerprint_current: bool | None = None
    classifier_version_current: bool | None = None
    fine_classifier_version_current: bool | None = None

    @property
    def current(self) -> bool | None:
        """The owner's own currency rule: fingerprint and both versions equal what runs now."""
        if self.fingerprint_current is None:
            return None
        return self.fingerprint_current and self.classifier_version_current and self.fine_classifier_version_current


def _string_array(raw: object) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        value = json.loads(raw)
    except ValueError:
        return False
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def decode_qualification_row(row: tuple) -> QualificationRead:
    """Decode one joined row as written, refusing anything outside the owners' vocabularies.

    Nothing is classified. The coarse values are resolved against the Phase 8
    taxonomy enums, the coarse JSON columns must be the string arrays persistence
    writes, and the fine half goes through the owner's strict decoder. The input
    fingerprint is recomputed with the owner's pure function over the stored
    classifier inputs, which is exactly what persistence compares.
    """
    opportunity_id = int(row[0])
    if not row[6]:
        return QualificationRead(opportunity_id, present=False, malformed_code=None)
    coarse_raw = row[7:12]
    json_raw = row[12:17]
    classifier_version, stored_fingerprint = row[17], row[18]
    fine_raw = row[19:24]

    decoded: dict[str, str] = {}
    for (name, vocabulary), raw in zip(_COARSE_VOCABULARIES, coarse_raw):
        try:
            decoded[name] = vocabulary(raw).value
        except ValueError:
            return QualificationRead(opportunity_id, True, f"INVALID_{name.upper()}")
    for name, raw in zip(_COARSE_JSON_COLUMNS, json_raw):
        if not _string_array(raw):
            return QualificationRead(opportunity_id, True, f"INVALID_{name.upper()}")
    if not isinstance(classifier_version, str) or not classifier_version.strip():
        return QualificationRead(opportunity_id, True, "INVALID_CLASSIFIER_VERSION")
    try:
        fine = decode_fine_classification(coarse_raw[0], *fine_raw)
    except FineClassificationDecodeError:
        return QualificationRead(opportunity_id, True, "INVALID_FINE_CLASSIFICATION")

    if fine.classifier_version is None:
        fine_state = "UNCLASSIFIED"
    elif fine.primary_category is None:
        fine_state = "CLASSIFIED_NO_CATEGORY"
    else:
        fine_state = "CLASSIFIED_WITH_CATEGORY"
    expected = input_fingerprint(*row[1:6])
    return QualificationRead(
        opportunity_id,
        True,
        None,
        classifier_version=classifier_version,
        fine_classifier_version=fine.classifier_version,
        fine_state=fine_state,
        fine_primary_category=None if fine.primary_category is None else fine.primary_category.value,
        fingerprint_current=stored_fingerprint == expected,
        classifier_version_current=classifier_version == CLASSIFIER_VERSION,
        fine_classifier_version_current=fine.classifier_version == FINE_CLASSIFIER_VERSION,
        **decoded,
    )


def read_qualifications(connection: sqlite3.Connection) -> dict[int, QualificationRead]:
    return {read.opportunity_id: read for read in map(decode_qualification_row, connection.execute(_PHASE8_SQL))}


def phase8_evidence(
    connection: sqlite3.Connection, scope: tuple[int, ...], reads: dict[int, QualificationRead]
) -> dict[str, Any]:
    scope_set = set(scope)
    orphans = connection.execute(
        """SELECT COUNT(*) FROM opportunity_qualifications AS q
            WHERE NOT EXISTS (SELECT 1 FROM opportunities AS o WHERE o.id = q.opportunity_id)"""
    ).fetchone()[0]
    present = [read for read in reads.values() if read.present]
    malformed = [read for read in present if read.malformed_code is not None]
    in_scope = [reads[opportunity_id] for opportunity_id in scope if opportunity_id in reads]
    missing = [read.opportunity_id for read in in_scope if not read.present]
    decoded = [read for read in in_scope if read.present and read.malformed_code is None]
    return {
        "coverage": {
            "in_scope_opportunity_count": len(scope),
            "qualification_row_count": len(present) + int(orphans),
            "in_scope_with_qualification_row": len(scope) - len(missing),
            "in_scope_without_qualification_row": len(missing),
            "in_scope_without_qualification_row_ids": _ids(missing),
            "qualification_rows_outside_scope": sum(1 for read in present if read.opportunity_id not in scope_set),
            "orphan_qualification_rows": int(orphans),
        },
        "decoding": {
            "decoded_row_count": len(present) - len(malformed),
            "malformed_row_count": len(malformed),
            "malformed_codes": _counts(read.malformed_code for read in malformed),
            "malformed_opportunity_ids": _ids(read.opportunity_id for read in malformed),
            "in_scope_qualification_counts": _counts(read.qualification for read in decoded),
            "in_scope_primary_domain_counts": _counts(read.primary_domain for read in decoded),
            "in_scope_opportunity_type_counts": _counts(read.opportunity_type for read in decoded),
            "in_scope_employment_type_counts": _counts(read.employment_type for read in decoded),
            "in_scope_listing_quality_counts": _counts(read.listing_quality for read in decoded),
            "in_scope_fine_state_counts": _counts(read.fine_state for read in decoded),
            "in_scope_fine_primary_category_counts": _counts(
                read.fine_primary_category for read in decoded if read.fine_primary_category is not None
            ),
            "classifier_version_counts": _counts(read.classifier_version for read in decoded),
            "fine_classifier_version_counts": _counts(
                "NULL" if read.fine_classifier_version is None else read.fine_classifier_version for read in decoded
            ),
        },
        "currency": {
            "running_classifier_version": CLASSIFIER_VERSION,
            "running_fine_classifier_version": FINE_CLASSIFIER_VERSION,
            "compared_row_count": len(decoded),
            "input_fingerprint_drift_count": sum(1 for read in decoded if not read.fingerprint_current),
            "classifier_version_drift_count": sum(1 for read in decoded if not read.classifier_version_current),
            "fine_classifier_version_drift_count": sum(1 for read in decoded if not read.fine_classifier_version_current),
            "not_current_count": sum(1 for read in decoded if not read.current),
            "not_current_opportunity_ids": _ids(read.opportunity_id for read in decoded if not read.current),
        },
    }


# --------------------------------------------------------------------------
# Matching boundary
# --------------------------------------------------------------------------


def matching_boundary_evidence(
    connection: sqlite3.Connection,
    profile_id: int,
    scope: tuple[int, ...],
    reads: dict[int, QualificationRead],
) -> dict[str, Any]:
    """Upstream identities of the current persisted Matching run. Matching is not run.

    Membership is never judged: whether a posting *should* be in the run is the
    selection's question, and answering it would recompute the selection.
    """
    try:
        current = read_current_matching(connection, profile_id)
    except MatchingReadError as error:
        return _unavailable(error)
    base = {
        "available": True,
        "error_code": None,
        "status": current.status,
        "current_run_id": current.current_run_id,
        "history_count": current.history_count,
        "selection_version": current.selection_version,
        "cohort_membership_freshness": NOT_ASSESSED,
        "recomputation": NOT_ASSESSED,
    }
    run = current.current_run
    if run is None:
        return {**base, "current_run": None}
    matched = [item.opportunity_id for item in run.assessments]
    scope_set = set(scope)
    orphan = [opportunity_id for opportunity_id in matched if opportunity_id not in reads]
    without_row = [opportunity_id for opportunity_id in matched if opportunity_id in reads and not reads[opportunity_id].present]
    malformed = [
        opportunity_id for opportunity_id in matched
        if opportunity_id in reads and reads[opportunity_id].present and reads[opportunity_id].malformed_code is not None
    ]
    decoded = [
        reads[opportunity_id] for opportunity_id in matched
        if opportunity_id in reads and reads[opportunity_id].present and reads[opportunity_id].malformed_code is None
    ]
    return {
        **base,
        "current_run": {
            "run_id": run.run_id,
            "run_fingerprint": run.run_fingerprint,
            "assessment_count": run.assessment_count,
            "created_at": run.created_at,
        },
        "matched_opportunity_count": len(matched),
        "distinct_matched_opportunity_count": len(set(matched)),
        "in_scope_opportunity_count": len(scope),
        "matched_orphan_count": len(orphan),
        "matched_orphan_ids": _ids(orphan),
        "matched_without_qualification_row_count": len(without_row),
        "matched_without_qualification_row_ids": _ids(without_row),
        "matched_with_malformed_qualification_count": len(malformed),
        "matched_outside_current_scope_count": sum(
            1 for opportunity_id in matched if opportunity_id in reads and opportunity_id not in scope_set
        ),
        # Persisted values of the matched postings, counted. Not a selection rule.
        "matched_persisted_qualification_counts": _counts(read.qualification for read in decoded),
        "matched_with_not_current_qualification_count": sum(1 for read in decoded if not read.current),
    }


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def _severity(integrity: str, demonstrability: str, operational_state: str | None, warn: bool = False) -> str:
    if integrity == FAIL:
        return BLOCKER
    if warn or demonstrability == NOT_DEMONSTRATED or operational_state in (NEVER_RUN, STALE, UNKNOWN):
        return WARNING
    return INFORMATIONAL


def make_check(
    name: str,
    integrity: str,
    demonstrability: str,
    evidence: dict[str, Any],
    operational_state: str | None = None,
    warn: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "integrity": integrity,
        "demonstrability": demonstrability,
        "operational_state": operational_state,
        "severity": _severity(integrity, demonstrability, operational_state, warn),
        "evidence": evidence,
    }


def _unavailable_check(name: str, part: dict[str, Any]) -> dict[str, Any]:
    return make_check(name, FAIL, NOT_ASSESSED, {"error_code": part["error_code"]})


def build_checks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: every verdict below is a function of the evidence dictionary alone."""
    checks = []
    query_only = evidence["database"]["query_only"]
    checks.append(make_check("SQLITE_QUERY_ONLY", PASS if query_only == 1 else FAIL, NOT_ASSESSED, {"query_only": query_only}))

    twin = evidence["digital_twin"]
    if not twin["available"]:
        checks.append(_unavailable_check("DIGITAL_TWIN_FACT_PROVENANCE", twin))
    else:
        broken = (
            not twin["owner_user_present"]
            or twin["accepted_facts_without_provenance"] > 0
            or twin["corrected_facts_without_resolvable_replacement"] > 0
        )
        checks.append(make_check(
            "DIGITAL_TWIN_FACT_PROVENANCE",
            FAIL if broken else PASS,
            DEMONSTRATED if twin["fact_status_counts"].get("ACCEPTED", 0) > 0 else NOT_DEMONSTRATED,
            {
                "owner_user_present": twin["owner_user_present"],
                "accepted_fact_provenance_consistent": twin["accepted_fact_provenance_consistent"],
                "corrected_facts_without_resolvable_replacement": twin["corrected_facts_without_resolvable_replacement"],
                "fact_status_counts": twin["fact_status_counts"],
                "distinct_cv_fingerprint_count": twin["distinct_cv_fingerprint_count"],
            },
            # A non-accepted fact without provenance is not a verified statement; it is flagged, not failed.
            warn=bool(twin["facts_without_provenance_by_status"]),
        ))

    projections = evidence["projections"]
    if not projections["available"]:
        checks.append(_unavailable_check("DIGITAL_TWIN_PROJECTIONS", projections))
    else:
        rows = sum(part["row_count"] for part in projections["structured"].values()) + projections["skills"]["skill_count"]
        stated = sum(1 for part in projections["explicit_input"].values() if part["stated"])
        unbacked = projections["unbacked_projection_row_count"]
        checks.append(make_check(
            "DIGITAL_TWIN_PROJECTIONS",
            PASS,
            DEMONSTRATED if rows + stated > 0 else NOT_DEMONSTRATED,
            {"structured_and_skill_rows": rows, "explicit_inputs_stated": stated, "unbacked_projection_row_count": unbacked},
            operational_state=STALE if unbacked else READY,
        ))

    targeting = evidence["targeting"]
    if not targeting["available"]:
        checks.append(_unavailable_check("PROFILE_TARGETING", targeting))
    else:
        known = targeting["geography"]["known"] and targeting["opportunity_type"]["known"]
        checks.append(make_check(
            "PROFILE_TARGETING",
            PASS,
            DEMONSTRATED if known else NOT_DEMONSTRATED,
            {
                "geography_rule_id": targeting["geography"]["rule_id"],
                "opportunity_type_rule_id": targeting["opportunity_type"]["rule_id"],
                "persisted_target_exists": targeting["persisted_target_exists"],
            },
            operational_state=None if known else UNKNOWN,
        ))

    collection = evidence["collection"]
    if not collection["available"]:
        checks.append(_unavailable_check("COLLECTION_SOURCES", collection))
    else:
        never_run = collection["configured_never_run_source_ids"]
        checks.append(make_check(
            "COLLECTION_SOURCES",
            FAIL if collection["runs_without_persisted_source_count"] else PASS,
            DEMONSTRATED if collection["run_status_counts"].get("SUCCESS", 0) > 0 else NOT_DEMONSTRATED,
            {
                "run_status_counts": collection["run_status_counts"],
                "configured_never_run_source_ids": never_run,
                "runs_without_persisted_source_count": collection["runs_without_persisted_source_count"],
                "freshness_claim": collection["freshness_claim"],
            },
            warn=bool(never_run),
        ))

    trace = evidence["traceability"]
    trace_broken = (
        trace["in_scope_without_source_link_count"]
        or trace["links_to_unpersisted_source_count"]
        or trace["links_to_missing_opportunity_count"]
    )
    checks.append(make_check(
        "COLLECTION_OPPORTUNITY_TRACEABILITY",
        FAIL if trace_broken else PASS,
        DEMONSTRATED if trace["in_scope_opportunity_count"] and not trace_broken else NOT_DEMONSTRATED,
        {key: trace[key] for key in (
            "in_scope_opportunity_count", "in_scope_without_source_link_count",
            "links_to_unpersisted_source_count", "links_to_missing_opportunity_count",
        )},
    ))

    dedup = evidence["dedup"]
    if not dedup["available"]:
        checks.append(_unavailable_check("DEDUP_PERSISTED_STATE", dedup))
    else:
        checks.append(make_check(
            "DEDUP_PERSISTED_STATE",
            FAIL if dedup["applied_merges_with_incoherent_tombstone"] or dedup["tombstones_without_applied_merge"] else PASS,
            dedup["end_to_end_merge_claim"],
            {key: dedup[key] for key in (
                "decision_count", "merge_count", "applied_merge_count",
                "applied_merges_with_incoherent_tombstone", "tombstones_without_applied_merge",
                "in_scope_duplicate_canonical_url_group_count",
            )},
        ))

    phase8 = evidence["phase8"]
    coverage, decoding, currency = phase8["coverage"], phase8["decoding"], phase8["currency"]
    complete = coverage["in_scope_opportunity_count"] > 0 and coverage["in_scope_without_qualification_row"] == 0
    checks.append(make_check(
        "PHASE8_QUALIFICATION_COVERAGE",
        FAIL if coverage["orphan_qualification_rows"] else PASS,
        DEMONSTRATED if complete else NOT_DEMONSTRATED,
        {key: coverage[key] for key in (
            "in_scope_opportunity_count", "in_scope_with_qualification_row",
            "in_scope_without_qualification_row", "orphan_qualification_rows",
        )},
        # An uncovered in-scope posting is one persistence has not reached yet.
        operational_state=STALE if coverage["in_scope_without_qualification_row"] else READY,
    ))
    checks.append(make_check(
        "PHASE8_PERSISTED_DECODING",
        FAIL if decoding["malformed_row_count"] else PASS,
        DEMONSTRATED if decoding["decoded_row_count"] else NOT_DEMONSTRATED,
        {key: decoding[key] for key in ("decoded_row_count", "malformed_row_count", "malformed_codes")},
    ))
    checks.append(make_check(
        "PHASE8_INPUT_CURRENCY",
        PASS,
        DEMONSTRATED if currency["compared_row_count"] else NOT_DEMONSTRATED,
        {key: currency[key] for key in (
            "compared_row_count", "input_fingerprint_drift_count", "classifier_version_drift_count",
            "fine_classifier_version_drift_count", "not_current_count",
        )},
        operational_state=None if not currency["compared_row_count"] else STALE if currency["not_current_count"] else READY,
    ))

    boundary = evidence["matching_boundary"]
    if not boundary["available"]:
        checks.append(_unavailable_check("MATCHING_UPSTREAM_BOUNDARY", boundary))
    elif boundary["current_run"] is None:
        checks.append(make_check(
            "MATCHING_UPSTREAM_BOUNDARY", PASS, NOT_DEMONSTRATED,
            {"status": boundary["status"], "cohort_membership_freshness": NOT_ASSESSED},
            operational_state=boundary["status"],
        ))
    else:
        broken = (
            boundary["matched_orphan_count"]
            or boundary["matched_without_qualification_row_count"]
            or boundary["matched_with_malformed_qualification_count"]
        )
        checks.append(make_check(
            "MATCHING_UPSTREAM_BOUNDARY",
            FAIL if broken else PASS,
            DEMONSTRATED if boundary["matched_opportunity_count"] and not broken else NOT_DEMONSTRATED,
            {key: boundary[key] for key in (
                "current_run_id", "matched_opportunity_count", "matched_orphan_count",
                "matched_without_qualification_row_count", "matched_with_malformed_qualification_count",
                "matched_outside_current_scope_count", "matched_with_not_current_qualification_count",
                "cohort_membership_freshness",
            )},
            operational_state=boundary["status"],
            warn=bool(boundary["matched_outside_current_scope_count"] or boundary["matched_with_not_current_qualification_count"]),
        ))

    database = evidence["database"]
    checks.append(make_check("DATABASE_UNCHANGED", PASS if database["unchanged"] else FAIL, NOT_ASSESSED, {
        "before_stable": database["before_stable"],
        "after_stable": database["after_stable"],
        "identical": database["identical"],
        "unchanged": database["unchanged"],
    }))
    return checks


def demonstrability_summary(checks: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Check names grouped by demonstrability, plus the dimensions never assessed."""
    summary: dict[str, list[str]] = {DEMONSTRATED: [], NOT_DEMONSTRATED: [], NOT_ASSESSED: []}
    for check in checks:
        summary[check["demonstrability"]].append(check["name"])
    summary[NOT_ASSESSED].extend(name for name, _ in NOT_ASSESSED_DIMENSIONS)
    return summary


# --------------------------------------------------------------------------
# the validation
# --------------------------------------------------------------------------


def _read_evidence(connection: sqlite3.Connection, profile_id: int, registry_path: Path) -> dict[str, Any]:
    """Everything read inside the one snapshot the caller holds open."""
    if connection.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone() is None:
        raise OperationalStateError("PROFILE_NOT_FOUND")
    scope = in_scope_opportunity_ids(connection)
    reads = read_qualifications(connection)
    return {
        "digital_twin": digital_twin_evidence(connection, profile_id),
        "projections": projection_evidence(connection, profile_id),
        "targeting": targeting_evidence(connection, profile_id),
        "collection": collection_evidence(connection, registry_path),
        "traceability": traceability_evidence(connection, scope),
        "dedup": dedup_evidence(connection, scope),
        "phase8": phase8_evidence(connection, scope, reads),
        "matching_boundary": matching_boundary_evidence(connection, profile_id, scope, reads),
    }


@dataclass(frozen=True)
class _ReadOutcome:
    read: dict[str, Any] | None
    query_only: int | None
    pending: Exception | None
    release_failed: bool


def _release(connection: sqlite3.Connection) -> bool:
    released = True
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        released = False
    try:
        connection.close()
    except sqlite3.Error:
        released = False
    return released


def _read_once(path: Path, reader: Callable[[sqlite3.Connection], dict[str, Any]]) -> _ReadOutcome:
    """Open `mode=ro`, confirm query_only, snapshot, read, then always release.

    Exceptions are carried back rather than raised, so the caller fingerprints
    the file before anything escapes.
    """
    connection = None
    pending: Exception | None = None
    read: dict[str, Any] | None = None
    query_only_value: int | None = None
    released = True
    try:
        try:
            connection = connect_readonly_database(path)
            connection.execute("PRAGMA query_only = ON")
            query_only = connection.execute("PRAGMA query_only").fetchone()
        except sqlite3.Error:
            raise OperationalStateError("DATABASE_OPEN_FAILED") from None
        if query_only is None or query_only[0] != 1:
            raise OperationalStateError("QUERY_ONLY_NOT_CONFIRMED")
        if connection.in_transaction:
            raise OperationalStateError("UNEXPECTED_OPEN_TRANSACTION")
        try:
            connection.execute("BEGIN")
        except sqlite3.Error:
            raise OperationalStateError("SNAPSHOT_OPEN_FAILED") from None
        try:
            read = reader(connection)
        except sqlite3.Error:
            raise OperationalStateError("SNAPSHOT_READ_FAILED") from None
        query_only_value = int(query_only[0])
    except Exception as error:  # carried, never translated
        pending, read, query_only_value = error, None, None
    finally:
        if connection is not None:
            released = _release(connection)
    return _ReadOutcome(read, query_only_value, pending, not released)


def validate_upstream_state(
    database_path: str | os.PathLike[str],
    profile_id: int,
    *,
    source_registry_path: str | os.PathLike[str] = DEFAULT_SOURCE_REGISTRY,
) -> dict[str, Any]:
    """Evidence about the persisted upstream chain of one profile, strictly read-only.

    Raises `OperationalStateError` when validation cannot run safely, with the
    same precedence as Phase 11.2A: a changed file first, then an unreleased
    snapshot, then the original safe error, then the original unexpected one.
    """
    profile_id = require_profile_id(profile_id)
    path = Path(database_path)
    if not path.exists():
        raise OperationalStateError("DATABASE_NOT_FOUND")
    if not path.is_file():
        raise OperationalStateError("DATABASE_NOT_A_FILE")
    registry_path = Path(source_registry_path)

    before = fingerprint_database(path)
    outcome = _read_once(path, lambda connection: _read_evidence(connection, profile_id, registry_path))
    after = fingerprint_database(path)
    database = database_unchanged(before, after)
    if outcome.pending is not None or outcome.release_failed:
        if not database["unchanged"]:
            raise OperationalStateError("DATABASE_CHANGED_DURING_VALIDATION") from None
        if outcome.release_failed:
            raise OperationalStateError("SNAPSHOT_RELEASE_FAILED") from None
        raise outcome.pending

    evidence = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "boundary": "STOPS_AT_CURRENT_MATCHING_RUN",
        "database": {
            "file_name": path.name,
            "before": before,
            "after": after,
            **database,
            "query_only": outcome.query_only,
        },
        **outcome.read,
        "not_assessed": [{"dimension": name, "reason_code": reason} for name, reason in NOT_ASSESSED_DIMENSIONS],
    }
    checks = build_checks(evidence)
    evidence["checks"] = checks
    evidence["demonstrability"] = demonstrability_summary(checks)
    evidence["integrity_result"] = PASS if all(check["integrity"] == PASS for check in checks) else FAIL
    return evidence


__all__ = [
    "CHECK_ORDER",
    "SCHEMA_VERSION",
    "OperationalStateError",
    "build_checks",
    "parse_profile_id",
    "serialize_evidence",
    "validate_upstream_state",
]
