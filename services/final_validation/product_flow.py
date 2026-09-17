"""Phase 11.2C: read-only evidence that persisted state reaches the product read paths.

Phase 11.2A proves the persisted chain from Matching downwards, and Phase 11.2B
the chain above Matching. This module proves the last step, where people
actually see that state, and stops there:

    canonical persisted Recommendation -> "Recommandé pour mon CV" read path
    -> explanation evidence -> persisted original URL evidence
    -> Explorer Data & AI read path -> Application Tracking read path
    -> Phase 5 proactivity reuse state

It answers one question: *does the product read exactly what was persisted, in
the persisted order, with the persisted explanations and real persisted links?*
It never ranks, recommends, classifies, matches, collects, sends, saves,
tracks, synchronizes or repairs anything, and it never contacts a URL.

Every product statement is produced by the official read path itself, called
inside this validator's own read-only snapshot:

    Recommendation   services.api.recommendation._read_one_snapshot (the
                     GET /api/recommendation projection), compared with
                     services.recommendation.read_model.read_current_recommendation
    Explanations     the projected snapshots, decoded against the Phase 9
                     partition constants in services.recommendation.models
    Original URLs    the `original_url` each read path chose through
                     services.api.link_priority; parsed, never requested
    Explorer         services.api.explorer._read_one_snapshot (GET /api/explorer),
                     paged, cross-checked with decode_fine_classification
    Applications     services.applications.read_model (the /applications reads)
    Proactivity      read_current_portfolio, read_current_matching,
                     gmail_digest.persistence.read_digest_status, and the
                     notification policy version constant

Both API helpers borrow a transaction the connection already holds, so they
read inside this validator's snapshot and leave it open. Direct SQL is used
only for structure: identity existence, persisted URL fields and their source
observations, persisted Phase 8 / constraint values beside Explorer items,
notification policy pointers, push subscription status counts (never
endpoints or keys), notification event/outbox/delivery status counts, and
portfolio run existence. None of it restates a business decision.

The status model, the snapshot and the fingerprints are the Phase 11.2A/11.2B
ones, reused. The evidence carries no CV text or hash, no email, no token, no
push endpoint or key, no application note or next action, no opportunity
title, organization or URL, and no exception message.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from services.api import explorer as explorer_api
from services.api import recommendation as recommendation_api
from services.applications.models import ApplicationError, ApplicationEventType
from services.applications.read_model import read_application_detail, read_applications
from services.applications.repository import preflight as applications_preflight
from services.collector.matching.read_model import MatchingReadError, read_current_matching
from services.collector.qualification.fine_read_model import (
    FineClassificationDecodeError,
    decode_fine_classification,
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
from services.final_validation.upstream_state import (
    DEMONSTRATED,
    NEVER_RUN,
    NOT_ASSESSED,
    NOT_DEMONSTRATED,
    READY,
    STALE,
    UNKNOWN,
    _read_once,
    make_check,
)
from services.gmail_digest.models import GmailDigestError
from services.gmail_digest.persistence import read_digest_status
from services.notifications.policy import NOTIFICATION_POLICY_VERSION
from services.portfolio.read_model import PortfolioReadError, read_current_portfolio
from services.recommendation.models import (
    CONFIRMED_GAP_CODES,
    STRENGTH_CODES,
    UNKNOWN_CODES,
    RecommendationDisposition,
)
from services.recommendation.read_model import RecommendationReadError, read_current_recommendation


SCHEMA_VERSION = "phase11.2c-product-flow-v1"

CHECK_ORDER = (
    "SQLITE_QUERY_ONLY",
    "PRODUCT_RECOMMENDATION_READ_PATH",
    "PRODUCT_EXPLANATION_EVIDENCE",
    "PRODUCT_REAL_URL_EVIDENCE",
    "EXPLORER_PRODUCT_READ_PATH",
    "APPLICATION_TRACKING_EVIDENCE",
    "PROACTIVITY_REUSE_STATE",
    "DATABASE_UNCHANGED",
)

#: Questions this validator deliberately does not answer.
NOT_ASSESSED_DIMENSIONS = (
    ("URL_NETWORK_LIVENESS", "NETWORK_ACCESS_OUT_OF_SCOPE"),
    ("RECOMMENDATION_RANKING_CORRECTNESS", "WOULD_RECOMPUTE_RECOMMENDATION"),
    ("EXPLORER_CLASSIFICATION_CORRECTNESS", "WOULD_RECLASSIFY"),
    ("BROWSER_RENDERING", "PHASE_11_3_MANUAL_DEMONSTRATION"),
    ("NOTIFICATION_AND_DIGEST_DELIVERY", "WOULD_SEND"),
)

#: The Phase 11.2C structural evidence criterion for a web-addressable product
#: URL. No repository owner defines an accepted scheme: this is this validator's
#: own criterion, applied because every surface renders `original_url` as a
#: link. It is a local parse only; network liveness stays NOT_ASSESSED.
ACCEPTED_URL_SCHEMES = ("http", "https")

REASON_PARTITIONS = {
    "strengths": frozenset(code.value for code in STRENGTH_CODES),
    "confirmed_gaps": frozenset(code.value for code in CONFIRMED_GAP_CODES),
    "unknowns": frozenset(code.value for code in UNKNOWN_CODES),
}

IDS_REPORTED = 20


class _ExplorerClockConsulted(RuntimeError):
    """The Explorer read path asked for a clock with no freshness filter active."""


def _explorer_clock() -> datetime:
    # Only a freshness filter reads the clock, and none is ever set here.
    raise _ExplorerClockConsulted("EXPLORER_CLOCK_CONSULTED")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _counts(values: Iterable[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(value) for value in values).items()))


def _ids(values: Iterable[int]) -> list[int]:
    return sorted(set(values))[:IDS_REPORTED]


def _value(item: Any) -> Any:
    return getattr(item, "value", item)


def _unavailable(error: BaseException) -> dict[str, Any]:
    """An owner refusal, reduced to its type name: never its message."""
    return {"available": False, "error_code": type(error).__name__}


def _plain(value: Any) -> Any:
    """A read model's frozen mappings and tuples as ordinary JSON containers."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _placeholders(values: list[int]) -> str:
    return ", ".join("?" for _ in values)


def _existing_opportunity_ids(connection: sqlite3.Connection, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return {
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM opportunities WHERE id IN ({_placeholders(ids)})", tuple(ids)
        ).fetchall()
    }


# --------------------------------------------------------------------------
# persisted URLs
# --------------------------------------------------------------------------


def persisted_links(connection: sqlite3.Connection, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Every persisted link value of each opportunity and its source observations."""
    if not ids:
        return {}
    links: dict[int, dict[str, Any]] = {opportunity_id: {"values": set(), "observations": 0} for opportunity_id in ids}
    for row in connection.execute(
        f"SELECT id, source_url, application_url, canonical_url FROM opportunities WHERE id IN ({_placeholders(ids)})",
        tuple(ids),
    ).fetchall():
        links[int(row[0])]["values"].update(value.strip() for value in row[1:] if isinstance(value, str) and value.strip())
    for row in connection.execute(
        f"""SELECT opportunity_id, source_url, application_url, canonical_url
              FROM opportunity_sources WHERE opportunity_id IN ({_placeholders(ids)})""",
        tuple(ids),
    ).fetchall():
        entry = links[int(row[0])]
        entry["observations"] += 1
        entry["values"].update(value.strip() for value in row[1:] if isinstance(value, str) and value.strip())
    return links


def classify_url(value: object) -> str:
    """Structural shape of one exposed link. Parsed locally; never requested."""
    if not isinstance(value, str) or not value.strip():
        return "MISSING"
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
    except ValueError:
        return "MALFORMED"
    if parts.scheme.lower() not in ACCEPTED_URL_SCHEMES or not host:
        return "MALFORMED"
    return "WELL_FORMED"


def url_evidence(exposed: list[tuple[int, object]], links: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Shape and persisted linkage of the links one read path exposed."""
    shapes = {opportunity_id: classify_url(url) for opportunity_id, url in exposed}
    unlinked = [
        opportunity_id for opportunity_id, url in exposed
        if shapes[opportunity_id] != "MISSING"
        and (not isinstance(url, str) or url.strip() not in links.get(opportunity_id, {}).get("values", set()))
    ]
    well_formed = [(opportunity_id, url) for opportunity_id, url in exposed if shapes[opportunity_id] == "WELL_FORMED"]
    return {
        "exposed_count": len(exposed),
        "shape_counts": _counts(shapes.values()),
        "missing_count": sum(1 for shape in shapes.values() if shape == "MISSING"),
        "malformed_count": sum(1 for shape in shapes.values() if shape == "MALFORMED"),
        "malformed_or_missing_opportunity_ids": _ids(i for i, shape in shapes.items() if shape != "WELL_FORMED"),
        "not_a_persisted_link_count": len(unlinked),
        "not_a_persisted_link_opportunity_ids": _ids(unlinked),
        "scheme_counts": _counts(urlsplit(str(url).strip()).scheme.lower() for _, url in well_formed),
        "distinct_host_count": len({urlsplit(str(url).strip()).hostname for _, url in well_formed}),
        "with_source_observation_count": sum(1 for i, _ in exposed if links.get(i, {}).get("observations", 0) > 0),
        "network_liveness": NOT_ASSESSED,
    }


# --------------------------------------------------------------------------
# Recommendation read path and explanations
# --------------------------------------------------------------------------


def recommendation_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    """The product projection, compared identity by identity with the canonical read model."""
    try:
        canonical = read_current_recommendation(connection, profile_id)
    except (RecommendationReadError, ValueError) as error:
        return {"canonical": _unavailable(error), "surface": None, "explanations": None, "urls": None}
    canonical_part = {
        "available": True,
        "error_code": None,
        "status": canonical.status,
        "current_run_id": canonical.current_run_id,
        "history_count": canonical.history_count,
    }
    try:
        # The official GET /api/recommendation projection, inside this snapshot.
        surface = recommendation_api._read_one_snapshot(connection, profile_id)
    except Exception as error:  # the public surface refuses every failure whole
        return {"canonical": canonical_part, "surface": _unavailable(error), "explanations": None, "urls": None}

    run = canonical.current_run
    surface_run = surface.current_run
    base_surface = {
        "available": True,
        "error_code": None,
        "status": surface.status,
        "integrity_ok": surface.integrity.ok,
        "status_matches_canonical": surface.status == canonical.status,
    }
    if run is None or surface_run is None:
        return {
            "canonical": canonical_part,
            "surface": {**base_surface, "run_present_in_both": run is None and surface_run is None},
            "explanations": None,
            "urls": None,
        }

    canonical_order = [(item.opportunity_id, item.rank_position) for item in run.assessments]
    surface_order = [(item.opportunity_id, item.rank_position) for item in surface_run.items]
    canonical_by_id = {item.opportunity_id: item for item in run.assessments}
    ids = [item.opportunity_id for item in surface_run.items]
    existing = _existing_opportunity_ids(connection, ids)
    fingerprint_mismatch = [
        item.opportunity_id for item in surface_run.items
        if item.opportunity_id in canonical_by_id
        and item.recommendation.assessment_fingerprint != canonical_by_id[item.opportunity_id].assessment_fingerprint
    ]
    disposition_mismatch = [
        item.opportunity_id for item in surface_run.items
        if item.opportunity_id in canonical_by_id
        and item.recommendation.disposition != _value(canonical_by_id[item.opportunity_id].disposition)
    ]
    identity_mismatch = [item.opportunity_id for item in surface_run.items if item.opportunity.id != item.opportunity_id]
    positions = [position for _, position in surface_order]
    surface_part = {
        **base_surface,
        "run_present_in_both": True,
        "run_id": surface_run.run_id,
        "run_id_matches_canonical": surface_run.run_id == run.run_id,
        "run_fingerprint_matches_canonical": surface_run.run_fingerprint == run.run_fingerprint,
        "source_matching_run_id": surface_run.source_matching_run_id,
        "item_count": len(surface_run.items),
        "canonical_assessment_count": run.assessment_count,
        "order_matches_canonical": surface_order == canonical_order,
        "rank_positions_contiguous": positions == list(range(1, len(positions) + 1)),
        "duplicate_opportunity_count": len(ids) - len(set(ids)),
        "orphan_opportunity_count": len(set(ids) - existing),
        "orphan_opportunity_ids": _ids(set(ids) - existing),
        "item_identity_mismatch_count": len(identity_mismatch),
        "assessment_fingerprint_mismatch_count": len(fingerprint_mismatch),
        "disposition_mismatch_count": len(disposition_mismatch),
        "disposition_counts": {
            disposition.value: sum(1 for item in surface_run.items if item.recommendation.disposition == disposition.value)
            for disposition in RecommendationDisposition
        },
        "ranking_recomputed": False,
    }

    partition_counts = {name: 0 for name in REASON_PARTITIONS}
    items_with = {name: 0 for name in REASON_PARTITIONS}
    code_counts: dict[str, Counter] = {name: Counter() for name in REASON_PARTITIONS}
    misplaced: list[int] = []
    payload_mismatch: list[int] = []
    for item in surface_run.items:
        snapshot = item.recommendation
        for name, allowed in REASON_PARTITIONS.items():
            codes = getattr(snapshot, name)
            partition_counts[name] += len(codes)
            items_with[name] += 1 if codes else 0
            code_counts[name].update(codes)
            if any(code not in allowed for code in codes):
                misplaced.append(item.opportunity_id)
        persisted = canonical_by_id.get(item.opportunity_id)
        if persisted is None or snapshot.explanation != _plain(persisted.assessment_payload):
            payload_mismatch.append(item.opportunity_id)
    explanations = {
        "item_count": len(surface_run.items),
        "reason_code_counts": {name: partition_counts[name] for name in REASON_PARTITIONS},
        "items_with_reason": items_with,
        "code_counts": {name: dict(sorted(code_counts[name].items())) for name in REASON_PARTITIONS},
        "items_with_code_outside_partition": len(set(misplaced)),
        "items_with_code_outside_partition_ids": _ids(misplaced),
        "explanation_not_persisted_payload_count": len(set(payload_mismatch)),
        "unknowns_are_questions_not_failures": True,
    }
    links = persisted_links(connection, sorted(existing))
    urls = url_evidence([(item.opportunity_id, item.opportunity.original_url) for item in surface_run.items], links)
    return {"canonical": canonical_part, "surface": surface_part, "explanations": explanations, "urls": urls}


# --------------------------------------------------------------------------
# Explorer read path
# --------------------------------------------------------------------------


def explorer_evidence(connection: sqlite3.Connection) -> dict[str, Any]:
    """Every Explorer page, read through the official projection, checked against Phase 8 rows."""
    items = []
    total = None
    offset = 0
    try:
        while True:
            page = explorer_api._read_one_snapshot(
                connection, explorer_api.ExplorerQuery(limit=explorer_api.MAX_LIMIT, offset=offset), _explorer_clock
            )
            total = page.total
            items.extend(page.items)
            offset += page.returned
            if page.returned == 0 or offset >= page.total:
                break
    except Exception as error:  # the public surface refuses every failure whole
        return {"surface": _unavailable(error), "urls": None}

    ids = [item.opportunity_id for item in items]
    unique = sorted(set(ids))
    existing = _existing_opportunity_ids(connection, unique)
    rows = {} if not unique else {
        int(row[0]): row[1:]
        for row in connection.execute(
            f"""SELECT o.id, o.status, o.is_active, q.qualification, q.fine_primary_category,
                       q.fine_secondary_categories_json, q.fine_category_evidence_json, q.fine_reasons_json,
                       q.fine_classifier_version, k.opportunity_type
                  FROM opportunities AS o
                  LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
                  LEFT JOIN opportunity_constraints AS k ON k.opportunity_id = o.id
                 WHERE o.id IN ({_placeholders(unique)})""",
            tuple(unique),
        ).fetchall()
    }
    without_row, outside_universe, malformed, category_mismatch, type_mismatch = [], [], [], [], []
    for item in items:
        row = rows.get(item.opportunity_id)
        if row is None:
            continue
        if row[2] is None:
            without_row.append(item.opportunity_id)
            continue
        if row[2] not in explorer_api.EXPLORER_QUALIFICATIONS or row[0] != "visible" or row[1] != 1:
            outside_universe.append(item.opportunity_id)
        try:
            fine = decode_fine_classification(*row[2:8])
        except FineClassificationDecodeError:
            malformed.append(item.opportunity_id)
            continue
        if _value(item.fine_primary_category) != _value(fine.primary_category):
            category_mismatch.append(item.opportunity_id)
        if _value(item.opportunity_type) != row[8]:
            type_mismatch.append(item.opportunity_id)
    links = persisted_links(connection, sorted(existing))
    return {
        "surface": {
            "available": True,
            "error_code": None,
            "total": total,
            "items_read": len(items),
            "pages_read_to_total": total == len(items),
            "duplicate_opportunity_count": len(ids) - len(unique),
            "orphan_opportunity_count": len(set(unique) - existing),
            "orphan_opportunity_ids": _ids(set(unique) - existing),
            "without_phase8_row_count": len(without_row),
            "outside_persisted_universe_count": len(outside_universe),
            "malformed_phase8_row_count": len(malformed),
            "malformed_phase8_opportunity_ids": _ids(malformed),
            "fine_category_not_persisted_value_count": len(category_mismatch),
            "opportunity_type_not_persisted_value_count": len(type_mismatch),
            "fine_primary_category_counts": _counts(
                "NONE" if item.fine_primary_category is None else _value(item.fine_primary_category) for item in items
            ),
            "live_classification": False,
        },
        "urls": url_evidence([(item.opportunity_id, item.original_url) for item in items], links),
    }


# --------------------------------------------------------------------------
# Application Tracking
# --------------------------------------------------------------------------


def _history_coherent(status: str, events: tuple) -> bool:
    """The recorded history begins with a creation and walks, change by change, to the current status."""
    if not events or _value(events[0].event_type) != ApplicationEventType.APPLICATION_CREATED.value:
        return False
    current = None
    for event in events:
        kind = _value(event.event_type)
        if kind == ApplicationEventType.APPLICATION_CREATED.value:
            if current is not None:
                return False
            current = _value(event.to_status)
        elif kind == ApplicationEventType.STATUS_CHANGED.value:
            if _value(event.from_status) != current:
                return False
            current = _value(event.to_status)
    return current == status


def application_evidence(
    connection: sqlite3.Connection, profile_id: int, recommended_ids: set[int]
) -> dict[str, Any]:
    """The /applications read models, described as ids, statuses and counts only."""
    try:
        applications_preflight(connection)
        views = read_applications(connection, profile_id=profile_id)
        details = [
            read_application_detail(connection, profile_id=profile_id, application_id=view.application.id)
            for view in views
        ]
    except (ApplicationError, ValueError, KeyError) as error:
        return {"surface": _unavailable(error), "urls": None}
    incoherent = [
        detail.application.id for detail in details
        if detail is None or not _history_coherent(_value(detail.application.status), detail.events)
    ]
    identity_mismatch = [view.application.id for view in views if view.opportunity.id != view.application.opportunity_id]
    events = [event for detail in details if detail is not None for event in detail.events]
    opportunity_ids = sorted({view.application.opportunity_id for view in views})
    links = persisted_links(connection, opportunity_ids)
    return {
        "surface": {
            "available": True,
            "error_code": None,
            "application_count": len(views),
            "status_counts": _counts(_value(view.application.status) for view in views),
            "event_count": len(events),
            "event_type_counts": _counts(_value(event.event_type) for event in events),
            "actor_type_counts": _counts(_value(event.actor_type) for event in events),
            "history_incoherent_count": len(incoherent),
            "history_incoherent_application_ids": _ids(incoherent),
            "opportunity_identity_mismatch_count": len(identity_mismatch),
            "tracked_opportunity_count": len(opportunity_ids),
            "tracked_in_current_recommendation_count": sum(1 for i in opportunity_ids if i in recommended_ids),
            "writes_performed": False,
        },
        "urls": url_evidence([(view.application.opportunity_id, view.opportunity.original_url) for view in views], links),
    }


# --------------------------------------------------------------------------
# Phase 5 proactivity reuse state
# --------------------------------------------------------------------------


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def notification_policy_evidence(state: tuple, portfolio_ids: set[int]) -> dict[str, Any]:
    """The stored notification policy row, checked against the invariants its owner refuses to run on.

    `sync_notification_policy` refuses a stored policy version other than the
    running one, and a high-water mark behind the baseline or the cursor. Those
    refusals are restated here as persisted-state invariants; no transition is
    evaluated and no event is derived. A pointer that is absent or not a
    positive integer makes the row incomplete, and an incomplete row is never
    read as healthy: every comparison that needs it answers False.
    """
    baseline, cursor, highest_seen, version = state
    pointers = (baseline, cursor, highest_seen)
    complete = all(_positive_int(value) for value in pointers)
    return {
        "baseline_portfolio_run_id": baseline,
        "last_processed_portfolio_run_id": cursor,
        "highest_seen_portfolio_run_id": highest_seen,
        "pointers_complete": complete,
        "pointers_reference_existing_runs": complete and all(value in portfolio_ids for value in pointers),
        "policy_version_matches_running_policy": version == NOTIFICATION_POLICY_VERSION,
        "high_water_not_behind_baseline": complete and highest_seen >= baseline,
        "high_water_not_behind_cursor": complete and highest_seen >= cursor,
    }


#: Every one of these must hold for a stored policy row to be one its owner would run on.
NOTIFICATION_POLICY_INVARIANTS = (
    "pointers_complete",
    "pointers_reference_existing_runs",
    "policy_version_matches_running_policy",
    "high_water_not_behind_baseline",
    "high_water_not_behind_cursor",
)


def proactivity_evidence(connection: sqlite3.Connection, profile_id: int, recommendation_source_matching_run_id: int | None) -> dict[str, Any]:
    """Which persisted snapshots notifications and the digest were built from, compared by identity.

    Proactivity is built on the audited Portfolio, an auxiliary domain, and not
    on Recommendation. That is stated, and nothing here re-derives an event, a
    digest, a delivery or a cursor.
    """
    try:
        portfolio = read_current_portfolio(connection, profile_id)
        matching = read_current_matching(connection, profile_id)
        digest = read_digest_status(connection, profile_id=profile_id)
    except (PortfolioReadError, MatchingReadError, GmailDigestError, ValueError) as error:
        return _unavailable(error)
    portfolio_run = portfolio.current_run
    portfolio_run_id = None if portfolio_run is None else portfolio_run.run_id
    portfolio_ids = {int(row[0]) for row in connection.execute(
        "SELECT id FROM portfolio_runs WHERE profile_id = ?", (profile_id,)
    ).fetchall()}
    state = connection.execute(
        """SELECT baseline_portfolio_run_id, last_processed_portfolio_run_id,
                  highest_seen_portfolio_run_id, policy_version
             FROM notification_policy_state WHERE profile_id = ?""",
        (profile_id,),
    ).fetchone()
    policy = None if state is None else notification_policy_evidence(tuple(state), portfolio_ids)
    event_max = connection.execute(
        "SELECT COALESCE(MAX(id), 0), COUNT(*) FROM notification_events WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    outbox = connection.execute(
        """SELECT COUNT(*) FROM notification_outbox AS x
             JOIN notification_events AS e ON e.id = x.event_id WHERE e.profile_id = ?""",
        (profile_id,),
    ).fetchone()[0]
    batches = connection.execute(
        """SELECT b.status, COUNT(*) FROM notification_delivery_batches AS b
             JOIN notification_outbox AS x ON x.id = b.outbox_id
             JOIN notification_events AS e ON e.id = x.event_id
            WHERE e.profile_id = ? GROUP BY b.status""",
        (profile_id,),
    ).fetchall()
    subscriptions = connection.execute(
        "SELECT status, notification_event_watermark FROM push_subscriptions WHERE profile_id = ?", (profile_id,)
    ).fetchall()
    latest = digest.latest
    digest_part = {
        "total_count": digest.total_count,
        "status_counts": dict(digest.status_counts),
        "latest_digest_date": None if latest is None else latest.digest_date,
        "latest_status": None if latest is None else _value(latest.status),
        "latest_portfolio_run_id": None if latest is None else latest.portfolio_run_id,
        "latest_item_count": None if latest is None else latest.item_count,
        "latest_sent_digest_date": None if digest.latest_sent is None else digest.latest_sent.digest_date,
        "digests_referencing_missing_portfolio_run": 0 if latest is None else int(latest.portfolio_run_id not in portfolio_ids),
    }
    comparisons = {
        "notification_cursor_is_current_portfolio_run": None if policy is None or portfolio_run_id is None
        else policy["last_processed_portfolio_run_id"] == portfolio_run_id,
        "latest_digest_is_current_portfolio_run": None if latest is None or portfolio_run_id is None
        else latest.portfolio_run_id == portfolio_run_id,
        "portfolio_built_from_current_matching_run": None if portfolio_run is None or matching.current_run_id is None
        else portfolio_run.matching_run_id == matching.current_run_id,
        "portfolio_matching_run_is_recommendation_source_run": None
        if portfolio_run is None or recommendation_source_matching_run_id is None
        else portfolio_run.matching_run_id == recommendation_source_matching_run_id,
    }
    return {
        "available": True,
        "error_code": None,
        "basis": "PORTFOLIO_AUXILIARY_NOT_RANKING_AUTHORITY",
        "portfolio_status": _value(portfolio.status),
        "portfolio_current_run_id": portfolio_run_id,
        "portfolio_matching_run_id": None if portfolio_run is None else portfolio_run.matching_run_id,
        "matching_current_run_id": matching.current_run_id,
        "recommendation_source_matching_run_id": recommendation_source_matching_run_id,
        "notification_policy": policy,
        "notification_event_count": int(event_max[1]),
        "notification_outbox_count": int(outbox),
        "delivery_batch_status_counts": _counts_from_rows(batches),
        "push_subscription_status_counts": _counts(row[0] for row in subscriptions),
        "push_watermarks_above_event_stream": sum(1 for row in subscriptions if int(row[1]) > int(event_max[0])),
        "digest": digest_part,
        "comparisons": comparisons,
        "regenerated": False,
        "sent": False,
    }


def _counts_from_rows(rows: list[tuple]) -> dict[str, int]:
    return dict(sorted((str(row[0]), int(row[1])) for row in rows))


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def _refused(name: str, part: dict[str, Any]) -> dict[str, Any]:
    return make_check(name, FAIL, NOT_ASSESSED, {"error_code": part["error_code"]})


def build_checks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure: every verdict below is a function of the evidence dictionary alone."""
    checks = []
    query_only = evidence["database"]["query_only"]
    checks.append(make_check("SQLITE_QUERY_ONLY", PASS if query_only == 1 else FAIL, NOT_ASSESSED, {"query_only": query_only}))

    recommendation = evidence["recommendation"]
    canonical, surface = recommendation["canonical"], recommendation["surface"]
    if not canonical["available"]:
        checks.append(_refused("PRODUCT_RECOMMENDATION_READ_PATH", canonical))
    elif not surface["available"]:
        checks.append(_refused("PRODUCT_RECOMMENDATION_READ_PATH", surface))
    elif not surface["run_present_in_both"] or surface.get("run_id") is None:
        state = {"READY": READY, "NOT_SYNCED": NEVER_RUN}.get(canonical["status"], canonical["status"])
        coherent = surface["status_matches_canonical"] and surface["run_present_in_both"]
        checks.append(make_check(
            "PRODUCT_RECOMMENDATION_READ_PATH", PASS if coherent else FAIL, NOT_DEMONSTRATED,
            {"canonical_status": canonical["status"], "surface_status": surface["status"],
             "run_present_in_both": surface["run_present_in_both"]},
            operational_state=state,
        ))
    else:
        broken = not (
            surface["integrity_ok"] and surface["status_matches_canonical"]
            and surface["run_id_matches_canonical"] and surface["run_fingerprint_matches_canonical"]
            and surface["order_matches_canonical"] and surface["rank_positions_contiguous"]
            and surface["item_count"] == surface["canonical_assessment_count"]
        ) or any(surface[key] for key in (
            "duplicate_opportunity_count", "orphan_opportunity_count", "item_identity_mismatch_count",
            "assessment_fingerprint_mismatch_count", "disposition_mismatch_count",
        ))
        checks.append(make_check(
            "PRODUCT_RECOMMENDATION_READ_PATH",
            FAIL if broken else PASS,
            DEMONSTRATED if surface["item_count"] and not broken else NOT_DEMONSTRATED,
            {key: surface[key] for key in (
                "run_id", "item_count", "order_matches_canonical", "run_fingerprint_matches_canonical",
                "orphan_opportunity_count", "assessment_fingerprint_mismatch_count", "disposition_counts",
                "ranking_recomputed",
            )},
            operational_state=READY,
        ))

    explanations = recommendation["explanations"]
    if explanations is None:
        checks.append(make_check("PRODUCT_EXPLANATION_EVIDENCE", PASS, NOT_ASSESSED, {"reason": "NO_READY_PRODUCT_RUN"}))
    else:
        broken = explanations["items_with_code_outside_partition"] or explanations["explanation_not_persisted_payload_count"]
        checks.append(make_check(
            "PRODUCT_EXPLANATION_EVIDENCE",
            FAIL if broken else PASS,
            DEMONSTRATED if explanations["item_count"] and not broken else NOT_DEMONSTRATED,
            {key: explanations[key] for key in (
                "item_count", "reason_code_counts", "items_with_reason",
                "items_with_code_outside_partition", "explanation_not_persisted_payload_count",
            )},
        ))

    url_parts = {
        "recommendation": recommendation["urls"],
        "explorer": evidence["explorer"]["urls"],
        "applications": evidence["applications"]["urls"],
    }
    assessed = {name: part for name, part in url_parts.items() if part is not None}
    if not assessed:
        checks.append(make_check("PRODUCT_REAL_URL_EVIDENCE", PASS, NOT_ASSESSED, {"network_liveness": NOT_ASSESSED}))
    else:
        bad = {name: part["missing_count"] + part["malformed_count"] + part["not_a_persisted_link_count"] for name, part in assessed.items()}
        exposed = sum(part["exposed_count"] for part in assessed.values())
        checks.append(make_check(
            "PRODUCT_REAL_URL_EVIDENCE",
            FAIL if any(bad.values()) else PASS,
            DEMONSTRATED if exposed and not any(bad.values()) else NOT_DEMONSTRATED,
            {
                "exposed_by_surface": {name: part["exposed_count"] for name, part in assessed.items()},
                "invalid_by_surface": bad,
                "surfaces_not_assessed": sorted(set(url_parts) - set(assessed)),
                "network_liveness": NOT_ASSESSED,
            },
        ))

    explorer = evidence["explorer"]["surface"]
    if not explorer["available"]:
        checks.append(_refused("EXPLORER_PRODUCT_READ_PATH", explorer))
    else:
        broken = not explorer["pages_read_to_total"] or any(explorer[key] for key in (
            "duplicate_opportunity_count", "orphan_opportunity_count", "without_phase8_row_count",
            "outside_persisted_universe_count", "malformed_phase8_row_count",
            "fine_category_not_persisted_value_count", "opportunity_type_not_persisted_value_count",
        ))
        checks.append(make_check(
            "EXPLORER_PRODUCT_READ_PATH",
            FAIL if broken else PASS,
            DEMONSTRATED if explorer["items_read"] and not broken else NOT_DEMONSTRATED,
            {key: explorer[key] for key in (
                "total", "items_read", "orphan_opportunity_count", "without_phase8_row_count",
                "malformed_phase8_row_count", "fine_category_not_persisted_value_count", "live_classification",
            )},
        ))

    applications = evidence["applications"]["surface"]
    if not applications["available"]:
        checks.append(_refused("APPLICATION_TRACKING_EVIDENCE", applications))
    else:
        broken = applications["history_incoherent_count"] or applications["opportunity_identity_mismatch_count"]
        checks.append(make_check(
            "APPLICATION_TRACKING_EVIDENCE",
            FAIL if broken else PASS,
            DEMONSTRATED if applications["application_count"] and not broken else NOT_DEMONSTRATED,
            {key: applications[key] for key in (
                "application_count", "status_counts", "event_count", "history_incoherent_count",
                "opportunity_identity_mismatch_count", "writes_performed",
            )},
        ))

    proactivity = evidence["proactivity"]
    if not proactivity["available"]:
        checks.append(_refused("PROACTIVITY_REUSE_STATE", proactivity))
    else:
        policy = proactivity["notification_policy"]
        violated_policy_invariants = [] if policy is None else [
            name for name in NOTIFICATION_POLICY_INVARIANTS if policy.get(name) is not True
        ]
        broken = bool(
            violated_policy_invariants
            # The owner refuses notification state that exists without a current Portfolio snapshot.
            or (policy is not None and proactivity["portfolio_current_run_id"] is None)
            or proactivity["digest"]["digests_referencing_missing_portfolio_run"]
            or proactivity["push_watermarks_above_event_stream"]
        )
        comparisons = proactivity["comparisons"]
        known = [value for value in comparisons.values() if value is not None]
        if broken:
            # An owner-invalid persisted state is a failure, never an ordinary currency state.
            state = None
        elif policy is None and proactivity["digest"]["total_count"] == 0:
            state = NEVER_RUN
        elif proactivity["portfolio_current_run_id"] is None or not known:
            state = UNKNOWN
        elif all(known):
            state = READY
        else:
            state = STALE
        checks.append(make_check(
            "PROACTIVITY_REUSE_STATE",
            FAIL if broken else PASS,
            DEMONSTRATED if state == READY and not broken else NOT_DEMONSTRATED,
            {
                "basis": proactivity["basis"],
                "portfolio_current_run_id": proactivity["portfolio_current_run_id"],
                "portfolio_matching_run_id": proactivity["portfolio_matching_run_id"],
                "matching_current_run_id": proactivity["matching_current_run_id"],
                "comparisons": comparisons,
                "notification_policy_present": policy is not None,
                "violated_notification_policy_invariants": violated_policy_invariants,
                "digest_status_counts": proactivity["digest"]["status_counts"],
                "push_subscription_status_counts": proactivity["push_subscription_status_counts"],
            },
            operational_state=state,
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
    summary: dict[str, list[str]] = {DEMONSTRATED: [], NOT_DEMONSTRATED: [], NOT_ASSESSED: []}
    for check in checks:
        summary[check["demonstrability"]].append(check["name"])
    summary[NOT_ASSESSED].extend(name for name, _ in NOT_ASSESSED_DIMENSIONS)
    return summary


# --------------------------------------------------------------------------
# the validation
# --------------------------------------------------------------------------


def _read_evidence(connection: sqlite3.Connection, profile_id: int) -> dict[str, Any]:
    """Everything read inside the one snapshot the caller holds open."""
    if connection.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone() is None:
        raise OperationalStateError("PROFILE_NOT_FOUND")
    recommendation = recommendation_evidence(connection, profile_id)
    surface = recommendation["surface"] or {}
    recommended_ids: set[int] = set()
    if surface.get("available") and surface.get("run_id") is not None:
        recommended_ids = {
            int(row[0]) for row in connection.execute(
                "SELECT opportunity_id FROM recommendation_assessments WHERE run_id = ?", (surface["run_id"],)
            ).fetchall()
        }
    return {
        "recommendation": recommendation,
        "explorer": explorer_evidence(connection),
        "applications": application_evidence(connection, profile_id, recommended_ids),
        "proactivity": proactivity_evidence(connection, profile_id, surface.get("source_matching_run_id")),
    }


def validate_product_flow(database_path: str | os.PathLike[str], profile_id: int) -> dict[str, Any]:
    """Evidence that persisted state reaches the product read paths, strictly read-only.

    Raises `OperationalStateError` when validation cannot run safely, with the
    Phase 11.2A precedence: a changed file first, then an unreleased snapshot,
    then the original safe error, then the original unexpected one.
    """
    profile_id = require_profile_id(profile_id)
    path = Path(database_path)
    if not path.exists():
        raise OperationalStateError("DATABASE_NOT_FOUND")
    if not path.is_file():
        raise OperationalStateError("DATABASE_NOT_A_FILE")

    before = fingerprint_database(path)
    outcome = _read_once(path, lambda connection: _read_evidence(connection, profile_id))
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
        "boundary": "STOPS_AT_PRODUCT_READ_PATHS",
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
    "classify_url",
    "parse_profile_id",
    "serialize_evidence",
    "validate_product_flow",
]
