"""Transaction-neutral, append-only persistence for Portfolio snapshots."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass

from services.collector.matching import MatchLane
from services.collector.matching.fingerprint import canonical_json
from services.eligibility import GlobalStatus
from services.priority import PriorityCategory

from .fingerprint import (
    canonical_portfolio_assessment_payload,
    portfolio_assessment_fingerprint,
)
from .input_assembly import PORTFOLIO_INPUT_ASSEMBLY_VERSION
from .models import (
    PORTFOLIO_ENGINE_VERSION,
    PORTFOLIO_RULES_VERSION,
    PortfolioAssessment,
    PortfolioBucket,
    PortfolioDisposition,
    PortfolioReasonCode,
)
from .persistence_fingerprint import (
    PORTFOLIO_PERSISTENCE_VERSION,
    canonical_portfolio_run_payload,
    portfolio_run_fingerprint,
)


class PortfolioPersistenceError(RuntimeError):
    """Raised when a Portfolio snapshot cannot be persisted safely."""


@dataclass(frozen=True)
class PortfolioStoreResult:
    run_id: int
    run_fingerprint: str
    created: bool
    state_changed: bool


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _prepare(profile_id: int, assessments: tuple[PortfolioAssessment, ...]):
    if (
        isinstance(profile_id, bool)
        or not isinstance(profile_id, int)
        or profile_id <= 0
    ):
        raise PortfolioPersistenceError("profile_id must be a positive integer")
    if not isinstance(assessments, tuple) or not assessments:
        raise PortfolioPersistenceError("assessment batch must be a non-empty tuple")
    prepared, ids = [], set()
    for item in assessments:
        if not isinstance(item, PortfolioAssessment) or item.profile_id != profile_id:
            raise PortfolioPersistenceError("assessment profile mismatch")
        if (
            isinstance(item.opportunity_id, bool)
            or not isinstance(item.opportunity_id, int)
            or item.opportunity_id <= 0
        ):
            raise PortfolioPersistenceError("invalid opportunity_id")
        if item.opportunity_id in ids:
            raise PortfolioPersistenceError("duplicate opportunity IDs")
        ids.add(item.opportunity_id)
        if (
            item.portfolio_engine_version != PORTFOLIO_ENGINE_VERSION
            or item.portfolio_rules_version != PORTFOLIO_RULES_VERSION
        ):
            raise PortfolioPersistenceError("unexpected Portfolio assessment versions")
        if not isinstance(item.disposition, PortfolioDisposition) or (
            item.disposition is PortfolioDisposition.INCLUDED
        ) != isinstance(item.bucket, PortfolioBucket):
            raise PortfolioPersistenceError("invalid disposition/bucket invariant")
        if not isinstance(item.reason_codes, tuple) or any(
            not isinstance(code, PortfolioReasonCode) for code in item.reason_codes
        ):
            raise PortfolioPersistenceError("invalid Portfolio reason code")
        if (
            not isinstance(item.eligibility_status, GlobalStatus)
            or not isinstance(item.matching_lane, MatchLane)
            or (
                item.priority_category is not None
                and not isinstance(item.priority_category, PriorityCategory)
            )
        ):
            raise PortfolioPersistenceError("invalid assessment enum")
        if type(item.safe_cap_applied) is not bool:
            raise PortfolioPersistenceError("safe_cap_applied must be a bool")
        matched, total, score = (
            item.required_skill_matched_count,
            item.required_skill_total_count,
            item.required_skill_score,
        )
        if (
            any(
                isinstance(v, bool) or not isinstance(v, int) or v < 0
                for v in (matched, total)
            )
            or matched > total
        ):
            raise PortfolioPersistenceError("invalid required-skill counts")
        if total == 0:
            valid_score = matched == 0 and score is None
        else:
            valid_score = (
                not isinstance(score, bool)
                and isinstance(score, (int, float))
                and math.isfinite(score)
                and 0 <= score <= 1
                and round(float(score), 12) == round(matched / total, 12)
            )
        if not valid_score:
            raise PortfolioPersistenceError("invalid required-skill score")
        expected = portfolio_assessment_fingerprint(item)
        if (
            not _sha(item.assessment_fingerprint)
            or item.assessment_fingerprint != expected
        ):
            raise PortfolioPersistenceError(
                "assessment fingerprint does not match canonical payload"
            )
        if not _sha(item.priority_assessment_fingerprint) or not _sha(
            item.matching_assessment_fingerprint
        ):
            raise PortfolioPersistenceError("invalid upstream assessment fingerprint")
        prepared.append(
            (item, canonical_json(canonical_portfolio_assessment_payload(item)))
        )
    return sorted(prepared, key=lambda value: value[0].opportunity_id)


def store_portfolio_batch(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    priority_run_id: int,
    priority_run_fingerprint: str,
    matching_run_id: int,
    matching_run_fingerprint: str,
    assessments: tuple[PortfolioAssessment, ...],
) -> PortfolioStoreResult:
    """Store or audit one semantic run, then promote its current pointer."""
    if not connection.in_transaction:
        raise PortfolioPersistenceError(
            "store_portfolio_batch requires an active transaction"
        )
    if (
        any(
            isinstance(v, bool) or not isinstance(v, int) or v <= 0
            for v in (priority_run_id, matching_run_id)
        )
        or not _sha(priority_run_fingerprint)
        or not _sha(matching_run_fingerprint)
    ):
        raise PortfolioPersistenceError("invalid run provenance")
    prepared = _prepare(profile_id, assessments)
    priority = connection.execute(
        "SELECT profile_id,matching_run_id,matching_run_fingerprint,run_fingerprint FROM priority_runs WHERE id=?",
        (priority_run_id,),
    ).fetchone()
    if (
        priority is None
        or priority[0] != profile_id
        or priority[3] != priority_run_fingerprint
    ):
        raise PortfolioPersistenceError("Priority run provenance is inconsistent")
    matching = connection.execute(
        "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?",
        (matching_run_id,),
    ).fetchone()
    if (
        matching is None
        or matching[0] != profile_id
        or matching[1] != matching_run_fingerprint
        or priority[1] != matching_run_id
        or priority[2] != matching_run_fingerprint
    ):
        raise PortfolioPersistenceError("Matching run provenance is inconsistent")
    wanted_priority = {
        a.opportunity_id: a.priority_assessment_fingerprint for a, _ in prepared
    }
    wanted_matching = {
        a.opportunity_id: a.matching_assessment_fingerprint for a, _ in prepared
    }
    stored_priority = dict(
        connection.execute(
            "SELECT opportunity_id,assessment_fingerprint FROM priority_assessments WHERE run_id=?",
            (priority_run_id,),
        )
    )
    stored_matching = dict(
        connection.execute(
            "SELECT opportunity_id,assessment_fingerprint FROM matching_assessments WHERE run_id=?",
            (matching_run_id,),
        )
    )
    if stored_priority != wanted_priority or stored_matching != wanted_matching:
        raise PortfolioPersistenceError("assessment cohort provenance is inconsistent")
    counts = Counter(a.bucket for a, _ in prepared if a.bucket is not None)
    included = sum(a.disposition is PortfolioDisposition.INCLUDED for a, _ in prepared)
    bucket_counts = {bucket.value: counts[bucket] for bucket in PortfolioBucket}
    values = dict(
        input_assembly_version=PORTFOLIO_INPUT_ASSEMBLY_VERSION,
        profile_id=profile_id,
        priority_run_fingerprint=priority_run_fingerprint,
        matching_run_fingerprint=matching_run_fingerprint,
        portfolio_engine_version=PORTFOLIO_ENGINE_VERSION,
        portfolio_rules_version=PORTFOLIO_RULES_VERSION,
        assessment_count=len(prepared),
        included_count=included,
        excluded_count=len(prepared) - included,
        bucket_counts=bucket_counts,
        assessments=tuple(
            (a.opportunity_id, a.assessment_fingerprint) for a, _ in prepared
        ),
    )
    payload = canonical_json(canonical_portfolio_run_payload(**values))
    fingerprint = portfolio_run_fingerprint(**values)
    found = connection.execute(
        "SELECT id,profile_id,priority_run_id,matching_run_id,persistence_version,input_assembly_version,portfolio_engine_version,portfolio_rules_version,priority_run_fingerprint,matching_run_fingerprint,assessment_count,included_count,excluded_count,safe_count,target_count,ambitious_count,run_payload_json FROM portfolio_runs WHERE run_fingerprint=?",
        (fingerprint,),
    ).fetchone()
    meta = (
        profile_id,
        priority_run_id,
        matching_run_id,
        PORTFOLIO_PERSISTENCE_VERSION,
        PORTFOLIO_INPUT_ASSEMBLY_VERSION,
        PORTFOLIO_ENGINE_VERSION,
        PORTFOLIO_RULES_VERSION,
        priority_run_fingerprint,
        matching_run_fingerprint,
        len(prepared),
        included,
        len(prepared) - included,
        bucket_counts["SAFE"],
        bucket_counts["TARGET"],
        bucket_counts["AMBITIOUS"],
        payload,
    )
    created = found is None
    wanted_rows = [
        (
            a.opportunity_id,
            a.disposition.value,
            None if a.bucket is None else a.bucket.value,
            None if a.priority_category is None else a.priority_category.value,
            a.eligibility_status.value,
            a.matching_lane.value,
            a.required_skill_score,
            a.required_skill_matched_count,
            a.required_skill_total_count,
            int(a.safe_cap_applied),
            a.assessment_fingerprint,
            p,
        )
        for a, p in prepared
    ]
    if found:
        if tuple(found[1:]) != meta:
            raise PortfolioPersistenceError(
                "existing Portfolio run metadata is corrupt"
            )
        run_id = int(found[0])
        rows = connection.execute(
            "SELECT opportunity_id,disposition,bucket,priority_category,eligibility_status,matching_lane,required_skill_score,required_skill_matched_count,required_skill_total_count,safe_cap_applied,assessment_fingerprint,assessment_payload_json FROM portfolio_assessments WHERE run_id=? ORDER BY opportunity_id",
            (run_id,),
        ).fetchall()
        if rows != wanted_rows:
            raise PortfolioPersistenceError(
                "existing Portfolio run assessments are corrupt"
            )
    else:
        run_id = int(
            connection.execute(
                "INSERT INTO portfolio_runs (profile_id,priority_run_id,matching_run_id,persistence_version,input_assembly_version,portfolio_engine_version,portfolio_rules_version,priority_run_fingerprint,matching_run_fingerprint,run_fingerprint,assessment_count,included_count,excluded_count,safe_count,target_count,ambitious_count,run_payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
                meta[:9] + (fingerprint,) + meta[9:],
            ).fetchone()[0]
        )
        connection.executemany(
            "INSERT INTO portfolio_assessments (run_id,opportunity_id,disposition,bucket,priority_category,eligibility_status,matching_lane,required_skill_score,required_skill_matched_count,required_skill_total_count,safe_cap_applied,assessment_fingerprint,assessment_payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, *row) for row in wanted_rows],
        )
    if connection.execute(
        "SELECT COUNT(*) FROM portfolio_assessments WHERE run_id=?", (run_id,)
    ).fetchone()[0] != len(prepared):
        raise PortfolioPersistenceError("stored assessment count mismatch")
    state = connection.execute(
        "SELECT current_run_id,persistence_version,input_assembly_version FROM portfolio_profile_state WHERE profile_id=?",
        (profile_id,),
    ).fetchone()
    wanted_state = (
        run_id,
        PORTFOLIO_PERSISTENCE_VERSION,
        PORTFOLIO_INPUT_ASSEMBLY_VERSION,
    )
    changed = state != wanted_state
    if state is None:
        connection.execute(
            "INSERT INTO portfolio_profile_state (profile_id,current_run_id,persistence_version,input_assembly_version) VALUES (?,?,?,?)",
            (profile_id, *wanted_state),
        )
    elif changed:
        connection.execute(
            "UPDATE portfolio_profile_state SET current_run_id=?,persistence_version=?,input_assembly_version=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=?",
            (*wanted_state, profile_id),
        )
    return PortfolioStoreResult(run_id, fingerprint, created, changed)
