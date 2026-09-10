"""Read-only integrity audit of persisted recommendation history.

Phase 9B.1 gave an already-computed `RecommendationBatchResult` somewhere to
live. Phase 9B.2a added the strict read model, which answers *"can I safely read
this?"*. This module answers a third and deliberately different question:

    does the persisted recommendation history still cryptographically and
    structurally contain exactly what its stored identities claim?

The distinction from the read model is the whole point of the split, and it is
not a matter of strictness. The read model refuses at the first value it cannot
hand back safely, because handing back half a run would be worse than handing
back nothing. An audit that stopped there would report one defect and hide the
rest, so nothing below raises on corrupt *data*: every finding becomes a
structured `RecommendationPersistenceAuditIssue`, the run carries on being
audited, and the profile's remaining runs are audited too. Only a failure of the
audit *itself* — an invalid argument, an absent profile, a broken schema —
raises, because then there is no report to give.

What this module does **not** do, and must never be extended to do:

    it recomputes no recommendation
        no score, no disposition, no reason, no ranking. Phase 9A's product is
        read exactly as persisted. The three digests are re-derived from the
        *stored* payloads, which proves the stored bytes still hash to the
        stored identities — it does not re-run the engine and could not.

    it repairs nothing
        not the ranking, not a state row, not a digest. The audit observes
        corruption and reports it. A repair path here would destroy the only
        evidence that corruption ever existed.

    it does not judge freshness
        a historical run whose `source_matching_run_id` is no longer the
        profile's *current* Matching run is valid history, and saying otherwise
        would rewrite the past. `matching_profile_state.current_run_id` is never
        read below, and requiring it to equal a recommendation run's source is
        explicitly forbidden: that is Phase 9B.3's question, not this one's.

    it does not compare stored versions to today's constants
        the state row and the run it names must agree with *each other*. Neither
        is compared to what this build ships, because an old run read under a
        new build is history, not corruption.

The ranking is the product. Assessments are read `ORDER BY rank_position ASC`
and are never re-sorted: the batch content statement lists the assessment
digests in exactly that order, and the operational run fingerprint consumes the
`(opportunity_id, assessment_fingerprint)` pairs in exactly that order. Sorting
either would make a re-ranked history digest identically to the original, which
is precisely the corruption this module exists to catch.

Source Matching provenance is audited per run and never per profile. Each run
copied a `source_matching_run_id` and a `source_matching_run_fingerprint`; both
are checked against the referenced `matching_runs` row, and that row's *own*
internal validity is taken from Phase 4's audit — from the **per-run** result,
never from the Matching report's global `ok`. Corruption in an unrelated
historical Matching run must not poison a recommendation run whose own source is
healthy.

The whole profile audit is one SQLite snapshot. It is many SELECTs across
`profiles`, `recommendation_profile_state`, `recommendation_runs`,
`recommendation_assessments`, `matching_runs` and Matching's own audit, and
Python's `sqlite3` opens a transaction only for DML — so without a boundary each
statement would be its own implicit read transaction and the report could
describe a database state that never existed. `_audit_snapshot` opens one
*deferred* transaction, never `BEGIN IMMEDIATE` (which reserves the write lock
and fails on a `mode=ro` connection), borrows a transaction the caller already
owns and never finishes it, and releases only what it opened — with `ROLLBACK`,
the one ending that cannot write even by accident. Matching's audit uses this
same connection and holds no transaction of its own, so its SELECTs join this
snapshot rather than opening one beside it.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from services.collector.matching.fingerprint import canonical_json
from services.collector.matching.persistence_audit import (
    MatchingPersistenceAuditError,
    MatchingRunAuditResult,
    audit_matching_profile_history,
)

from .input_assembly import RecommendationReadinessIssueCode
from .models import RecommendationDisposition
from .persistence_audit_fingerprint import (
    recommendation_persistence_audit_fingerprint,
)
from .persistence_fingerprint import recommendation_run_fingerprint

__all__ = [
    "RECOMMENDATION_PERSISTENCE_AUDIT_VERSION",
    "RecommendationPersistenceAuditError",
    "RecommendationPersistenceAuditIssue",
    "RecommendationPersistenceAuditReport",
    "RecommendationRankedAssessmentIdentity",
    "RecommendationRunAuditResult",
    "audit_recommendation_profile_history",
]

#: This audit's own contract: the checks below, the issue vocabulary, the report
#: shape and the stable fingerprint payload. It is independent of the engine,
#: rules, assembly and persistence versions the other phases own.
RECOMMENDATION_PERSISTENCE_AUDIT_VERSION = "recommendation-persistence-audit-v1"

#: NOT_SYNCED is the absence of a state row, never a stored value.
_STATUS_NOT_SYNCED = "NOT_SYNCED"
_STATUS_READY = "READY"
_STATUS_INCOMPLETE = "INCOMPLETE"
#: What a state column that is not even one of the two stored values reports as.
_STATUS_UNKNOWN = "UNKNOWN"

#: The canonical encoding of "no readiness issue", byte for byte as 9B.1 writes
#: it for READY. READY is not merely *an empty list*: it is this exact text.
_NO_READINESS_ISSUES = canonical_json([])

_DISPOSITIONS = frozenset(item.value for item in RecommendationDisposition)
_READINESS_CODES = frozenset(item.value for item in RecommendationReadinessIssueCode)
_READINESS_KEYS = {"code", "message", "opportunity_id"}

#: Findings are grouped before they are ordered: what is wrong with the profile
#: comes before what is wrong with a run, which comes before what is wrong with
#: one ranked assessment. A profile-scoped finding may still name an
#: opportunity — a readiness issue does — so the scope is carried explicitly
#: rather than inferred from which identity happens to be present.
_SCOPE_PROFILE = "PROFILE"
_SCOPE_RUN = "RUN"
_SCOPE_ASSESSMENT = "ASSESSMENT"
_SCOPE_ORDER = {_SCOPE_PROFILE: 0, _SCOPE_RUN: 1, _SCOPE_ASSESSMENT: 2}


class RecommendationPersistenceAuditError(RuntimeError):
    """Raised when the audit itself cannot be performed.

    Reserved for the three cases where there is no honest report to return: an
    invalid public argument, a profile that does not exist, and a SQLite or
    schema failure. Persisted corruption is never raised — it is what the report
    is for.
    """


@dataclass(frozen=True)
class RecommendationPersistenceAuditIssue:
    """One structured, deterministic finding.

    `detail` is a fixed sentence over stored values, never a timestamp, a path
    or anything else that would differ between two audits of one database.
    """

    scope: str
    code: str
    run_id: int | None
    opportunity_id: int | None
    detail: str


@dataclass(frozen=True)
class RecommendationRankedAssessmentIdentity:
    """One ranked assessment, as the audit was able to establish it.

    A field that failed its structural check is exposed as `None` and the raw
    stored value is carried in the corresponding issue's `detail`. Nothing here
    is normalized into something plausible: an unreadable identity reads as
    unreadable.
    """

    rank_position: int | None
    opportunity_id: int | None
    assessment_fingerprint: str | None


@dataclass(frozen=True)
class RecommendationRunAuditResult:
    """Everything needed to describe one audited run without the report."""

    run_id: int
    run_fingerprint: str | None
    batch_fingerprint: str | None
    source_matching_run_id: int | None
    source_matching_run_fingerprint: str | None
    ranked_assessments: tuple[RecommendationRankedAssessmentIdentity, ...]
    assessment_count: int
    ok: bool
    issues: tuple[RecommendationPersistenceAuditIssue, ...]


@dataclass(frozen=True)
class RecommendationPersistenceAuditReport:
    """One profile's whole persisted recommendation history, judged.

    `assessment_count` is the number of assessment rows actually found across
    the audited runs, not the sum of what the runs *claim*; a run whose stored
    count disagrees with its rows carries that disagreement as an issue.
    """

    audit_version: str
    profile_id: int
    status: str
    current_run_id: int | None
    run_count: int
    audited_run_count: int
    assessment_count: int
    ok: bool
    issues: tuple[RecommendationPersistenceAuditIssue, ...]
    runs: tuple[RecommendationRunAuditResult, ...]
    audit_fingerprint: str


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


def _issue(
    scope: str,
    code: str,
    run_id: int | None,
    opportunity_id: int | None,
    detail: str,
) -> RecommendationPersistenceAuditIssue:
    return RecommendationPersistenceAuditIssue(
        scope, code, run_id, opportunity_id, detail
    )


def _issue_sort_key(issue: RecommendationPersistenceAuditIssue) -> tuple:
    """One stable order for every finding, with `None` handled on purpose.

    Ordering `(run_id, opportunity_id, ...)` tuples directly would compare
    `None` with `int` and raise as soon as two findings of one code differed in
    whether they name a run — which is exactly the situation an audit produces.
    Each nullable identity therefore contributes two components: whether it is
    present, then its value, with a fixed stand-in that is only ever compared
    against other absent values.
    """
    return (
        _SCOPE_ORDER.get(issue.scope, len(_SCOPE_ORDER)),
        issue.run_id is not None,
        issue.run_id if issue.run_id is not None else 0,
        issue.opportunity_id is not None,
        issue.opportunity_id if issue.opportunity_id is not None else 0,
        issue.code,
        issue.detail,
    )


def _ordered(
    issues: list[RecommendationPersistenceAuditIssue],
) -> tuple[RecommendationPersistenceAuditIssue, ...]:
    return tuple(sorted(issues, key=_issue_sort_key))


# --------------------------------------------------------------------------
# value checks
#
# `True` is an `int` in Python and compares equal to `1`, so every operational
# integer refuses `bool` outright, exactly as 9B.1's persistence and 9B.2a's
# read model do.
# --------------------------------------------------------------------------


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_ratio(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def _is_utf8(text: str) -> bool:
    """Whether this project's strict UTF-8 canonical encoding can carry `text`.

    Not every Python `str` has one. `json.loads` accepts an escaped unpaired
    surrogate — `"\\ud800"` is syntactically valid JSON — and hands back a
    string that strict UTF-8 refuses to encode, so a *decoded* persisted payload
    can hold a character the encoding the whole project fingerprints under has
    no bytes for. A stored column can never hold one (SQLite is handed UTF-8 and
    the write would already have failed), which is exactly why the escape is the
    interesting case: it survives storage and only surfaces at digest time.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _canonical_utf8(payload: Any) -> tuple[str, bytes] | None:
    """Canonical JSON for `payload`, and the exact bytes a digest is taken over.

    The audit's one canonicalization: `canonical_json(...).encode("utf-8")`
    written once, where the two steps can fail together, instead of at each
    digest site where the encoding could abort the whole audit over a single
    corrupt payload. Both results are returned because both are needed — the
    text for the canonical-form comparison, the bytes for SHA-256 — and they
    must describe the same value.

    `None` means the canonical text has no strict UTF-8 encoding. That is a
    finding about persisted data, and it is deliberately not repaired here: no
    `errors="ignore"`, no `errors="replace"`, no `"surrogatepass"`. Each would
    hand back bytes this project never agreed to fingerprint — the first two by
    losing the evidence, the third by inventing an encoding the canonical
    contract excludes — and a digest computed over them would be a claim about
    something that was never stored.
    """
    text = canonical_json(payload)
    try:
        return text, text.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


#: How a value strict UTF-8 cannot carry appears in the stable audit payload.
#: Tagging replaces the string with a single-key *object*, which no valid stored
#: string can digest as, so a tagged value never collides with an ordinary one.
_UNENCODABLE_KEY = "__not_utf8__"


def _escaped(value: str) -> str:
    """A deterministic, always-encodable rendering of an unencodable string.

    Every character strict UTF-8 has no bytes for is written as its own
    `\\uXXXX` escape and every other character is kept exactly as stored. The
    result is a pure function of the value, is always encodable, and states what
    was found instead of replacing it — no character is dropped, none is
    substituted, and nothing becomes a plausible-looking business string.
    """
    return "".join(
        character if _is_utf8(character) else f"\\u{ord(character):04x}"
        for character in value
    )


def _stable_value(value: object) -> Any:
    """A JSON-safe, deterministic rendering of one stored value.

    Everything SQLite stores in the audited TEXT and INTEGER columns is already
    JSON-safe; a BLOB left behind by a restore is not, and letting one reach
    `canonical_json` would crash the audit over the very corruption it exists to
    report. Such a value is rendered with `repr`, which is deterministic and is
    not a normalization: nothing is coerced into a plausible-looking version of
    itself, and the finding that named the value still stands beside it.

    A `str` is JSON-safe but not necessarily *encodable*, so one that strict
    UTF-8 refuses is tagged rather than carried — the audit fingerprint is taken
    over these bytes and an unpaired surrogate would abort it. Decoded readiness
    entries arrive here as lists and objects, so containers are walked rather
    than `repr`-ed, and a value that is encodable is returned exactly as before:
    valid data digests to what it always digested to.
    """
    if isinstance(value, str):
        return value if _is_utf8(value) else {_UNENCODABLE_KEY: _escaped(value)}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) and _is_utf8(key) for key in value):
            return {key: _stable_value(item) for key, item in value.items()}
        # An unencodable *key* cannot be tagged in place without two distinct
        # keys collapsing into one key, so the object is carried whole as the
        # deterministic rendering of what was stored.
        return {_UNENCODABLE_KEY: _escaped(repr(value))}
    return repr(value)


# --------------------------------------------------------------------------
# the one snapshot
# --------------------------------------------------------------------------


def _query(
    connection: sqlite3.Connection, sql: str, parameters: tuple, description: str
) -> list[tuple]:
    """Run one SELECT; a `sqlite3.Error` is a failed audit, not a finding."""
    try:
        return connection.execute(sql, parameters).fetchall()
    except sqlite3.Error as error:
        raise RecommendationPersistenceAuditError(
            f"cannot read {description}"
        ) from error


@contextmanager
def _audit_snapshot(connection: sqlite3.Connection, description: str) -> Iterator[None]:
    """Hold one SQLite read transaction across the whole profile audit.

    Same semantics as 9B.2a's `_read_snapshot`, restated here rather than
    imported so that the audit's transaction boundary is the audit's own and can
    never be changed out from under it by a read-model refactor.

    A plain `BEGIN` is *deferred*: it takes no lock until the first read, and
    from that read onwards every statement — including Matching's audit, which
    runs on this same connection and opens nothing itself — sees one snapshot.
    Never `BEGIN IMMEDIATE`: reserving the write lock is what a reader must not
    do, and it would fail outright on a `mode=ro` connection.

    A transaction the caller already owns is borrowed and never finished:
    committing or rolling back someone else's work would silently end something
    this module cannot see. Only a transaction opened here is ended here, and it
    is always ended with `ROLLBACK`.
    """
    if connection.in_transaction:
        yield
        return
    try:
        connection.execute("BEGIN")
    except sqlite3.Error as error:
        raise RecommendationPersistenceAuditError(
            f"cannot open a read snapshot for {description}"
        ) from error
    try:
        yield
    except BaseException:
        # The failure inside the body is the report the caller needs; a failure
        # while releasing the snapshot must not replace it. The snapshot is
        # still released, so the connection never keeps holding a read lock.
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        raise RecommendationPersistenceAuditError(
            f"cannot release the read snapshot for {description}"
        ) from error


# --------------------------------------------------------------------------
# assessments
# --------------------------------------------------------------------------

_ASSESSMENT_COLUMNS = (
    "opportunity_id,rank_position,disposition,recommendation_score,"
    "evidence_coverage,assessment_fingerprint,assessment_payload_json"
)


def _assessment_columns_against_payload(
    payload: dict[str, Any],
    run_id: int,
    opportunity_id: int | None,
    disposition: object,
    score: object,
    coverage: object,
    engine_version: object,
    rules_version: object,
) -> list[RecommendationPersistenceAuditIssue]:
    """Judge the duplicated columns against the payload they were copied from.

    `recommendation_assessments` duplicates three values the payload already
    carries — the disposition, the score and the evidence coverage — so a reader
    can filter and sort without decoding every envelope. A duplicate is only
    useful while it still agrees with the original, and a payload whose
    `result` or `versions` section is missing or is not an object cannot be
    compared at all: that is reported once, as itself, instead of being read
    through and surfacing as a `KeyError` or a `TypeError` from somewhere else.
    """
    issues: list[RecommendationPersistenceAuditIssue] = []

    def finding(code: str, detail: str) -> None:
        issues.append(
            _issue(_SCOPE_ASSESSMENT, code, run_id, opportunity_id, detail)
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        finding(
            "ASSESSMENT_RESULT_SECTION_INVALID",
            "assessment payload carries no result object",
        )
    else:
        for code, key, stored in (
            ("ASSESSMENT_DISPOSITION_MISMATCH", "disposition", disposition),
            ("ASSESSMENT_SCORE_MISMATCH", "recommendation_score", score),
            (
                "ASSESSMENT_EVIDENCE_COVERAGE_MISMATCH",
                "recommendation_evidence_coverage",
                coverage,
            ),
        ):
            if key not in result:
                finding(code, f"assessment payload result carries no {key}")
            elif result[key] != stored:
                finding(
                    code,
                    f"column {key} is {stored!r}, payload result says"
                    f" {result[key]!r}",
                )

    versions = payload.get("versions")
    if not isinstance(versions, dict):
        finding(
            "ASSESSMENT_VERSIONS_SECTION_INVALID",
            "assessment payload carries no versions object",
        )
        return issues
    for code, key, stored in (
        ("ASSESSMENT_ENGINE_VERSION_MISMATCH", "recommendation_engine", engine_version),
        ("ASSESSMENT_RULES_VERSION_MISMATCH", "recommendation_rules", rules_version),
    ):
        if key not in versions:
            finding(code, f"assessment payload versions carry no {key}")
        elif versions[key] != stored:
            finding(
                code,
                f"run {key} version is {stored!r}, payload says {versions[key]!r}",
            )
    return issues


def _audit_assessment(
    row: tuple,
    run_id: int,
    expected_position: int,
    engine_version: object,
    rules_version: object,
) -> tuple[
    RecommendationRankedAssessmentIdentity,
    list[RecommendationPersistenceAuditIssue],
]:
    """Audit one persisted assessment row, at the rank position it claims."""
    (
        opportunity_id,
        rank_position,
        disposition,
        score,
        coverage,
        fingerprint,
        payload_json,
    ) = row
    issues: list[RecommendationPersistenceAuditIssue] = []
    identity = opportunity_id if _is_positive_int(opportunity_id) else None

    def finding(code: str, detail: str) -> None:
        issues.append(_issue(_SCOPE_ASSESSMENT, code, run_id, identity, detail))

    where = f"rank {rank_position!r}"
    if identity is None:
        finding(
            "ASSESSMENT_OPPORTUNITY_ID_INVALID",
            f"opportunity_id {opportunity_id!r} at {where} is not a positive integer",
        )
    if not _is_positive_int(rank_position):
        finding(
            "ASSESSMENT_RANK_POSITION_INVALID",
            f"rank_position {rank_position!r} is not a positive integer",
        )
    elif rank_position != expected_position:
        # `ORDER BY rank_position ASC` still reads as an ordered list when a
        # position is missing or repeated, and every later position would
        # silently shift. The stored ranking is required to be exactly 1..N.
        finding(
            "ASSESSMENT_RANK_NOT_CONTIGUOUS",
            f"expected rank position {expected_position}, stored {rank_position!r}",
        )
    if disposition not in _DISPOSITIONS:
        finding(
            "ASSESSMENT_DISPOSITION_UNKNOWN",
            f"unknown disposition {disposition!r}",
        )
    if not _is_ratio(coverage):
        finding(
            "ASSESSMENT_EVIDENCE_COVERAGE_INVALID",
            f"evidence_coverage {coverage!r} is not a ratio in [0, 1]",
        )
    if score is not None and not _is_ratio(score):
        finding(
            "ASSESSMENT_SCORE_INVALID",
            f"recommendation_score {score!r} is not a ratio in [0, 1]",
        )
    if _is_ratio(coverage) and (score is None or _is_ratio(score)):
        # Phase 9A's contract restated where it cannot be bypassed: no evidence
        # means no score, and a score means some evidence. A stored `0.0` under
        # no coverage would read as "measured and bad", not "not measured".
        if (coverage == 0.0) != (score is None):
            finding(
                "ASSESSMENT_SCORE_AVAILABILITY_MISMATCH",
                f"evidence_coverage {coverage!r} disagrees with"
                f" recommendation_score {score!r}",
            )
    digest = fingerprint if _is_fingerprint(fingerprint) else None
    if digest is None:
        finding(
            "ASSESSMENT_FINGERPRINT_INVALID",
            f"assessment_fingerprint {fingerprint!r} is not a SHA-256 digest",
        )

    payload: dict[str, Any] | None = None
    try:
        decoded = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        finding(
            "ASSESSMENT_PAYLOAD_INVALID_JSON",
            "assessment payload is not valid JSON",
        )
    else:
        if not isinstance(decoded, dict):
            finding(
                "ASSESSMENT_PAYLOAD_NOT_OBJECT",
                "assessment payload is not a JSON object",
            )
        else:
            payload = decoded
            canonical = _canonical_utf8(payload)
            if canonical is None:
                # Syntactically valid JSON that decodes to a string strict UTF-8
                # cannot carry. Neither statement below can be made honestly:
                # the canonical form of this payload has no bytes, so there is
                # nothing to compare its stored text against and nothing to
                # hash. Both are skipped rather than reported as a second,
                # invented mismatch, and the payload is reported under the code
                # the vocabulary already has for one that cannot be read as this
                # project's JSON. The audit is not aborted, no assessment is
                # recomputed, and the remaining checks below still run.
                finding(
                    "ASSESSMENT_PAYLOAD_INVALID_JSON",
                    "assessment payload is not representable as canonical"
                    " UTF-8 JSON",
                )
            else:
                canonical_text, canonical_bytes = canonical
                if canonical_text != payload_json:
                    finding(
                        "ASSESSMENT_PAYLOAD_NOT_CANONICAL",
                        "assessment payload is not canonical JSON",
                    )
                # The persisted payload *is* Phase 9A's canonical business
                # payload, so verifying its digest means hashing what is stored
                # — never rebuilding an assessment and re-deriving a score from
                # it. No identity is injected: the content digest holds no
                # profile id and no opportunity id, by construction.
                if digest is not None and _digest(canonical_bytes) != digest:
                    finding(
                        "ASSESSMENT_FINGERPRINT_MISMATCH",
                        "assessment fingerprint differs from the stored payload",
                    )

    if payload is not None:
        issues.extend(
            _assessment_columns_against_payload(
                payload,
                run_id,
                identity,
                disposition,
                score,
                coverage,
                engine_version,
                rules_version,
            )
        )
    return (
        RecommendationRankedAssessmentIdentity(
            rank_position if _is_positive_int(rank_position) else None,
            identity,
            digest,
        ),
        issues,
    )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

_RUN_COLUMNS = (
    "id,profile_id,source_matching_run_id,persistence_version,input_assembly_version,"
    "recommendation_engine_version,recommendation_rules_version,"
    "source_matching_run_fingerprint,batch_fingerprint,run_fingerprint,"
    "assessment_count,batch_payload_json"
)


def _audit_source_matching_provenance(
    connection: sqlite3.Connection,
    run_id: int,
    profile_id: int,
    source_run_id: int | None,
    source_fingerprint: str | None,
    matching_results: dict[int, MatchingRunAuditResult],
) -> list[RecommendationPersistenceAuditIssue]:
    """Audit the Matching snapshot this run says it was produced over.

    Four questions, and deliberately not a fifth. The referenced row must exist,
    must belong to this same profile, must still carry the fingerprint that was
    copied onto the recommendation run, and must itself survive Phase 4's own
    persistence audit.

    The fifth question — is this the profile's *current* Matching run — is never
    asked. `M1 -> R1`, then a newer `M2` becoming current, leaves `R1` exactly as
    valid as it was: it *was* produced over `M1`, and that is a fact about the
    past that a later Matching run cannot falsify. Requiring
    `source_matching_run_id == matching_profile_state.current_run_id` would turn
    every superseded run into corruption. Freshness is Phase 9B.3's question.

    Internal validity is taken from the **per-run** Matching audit result, never
    from the Matching report's global `ok`: a corrupt unrelated historical
    Matching run says nothing about this run's source, and letting it decide
    would report corruption where none exists.
    """
    if source_run_id is None:
        # The prerequisite failed its structural check and was already reported;
        # a second finding about a row that cannot be looked up would be noise.
        return []
    issues: list[RecommendationPersistenceAuditIssue] = []

    def finding(code: str, detail: str) -> None:
        issues.append(_issue(_SCOPE_RUN, code, run_id, None, detail))

    rows = _query(
        connection,
        "SELECT profile_id,run_fingerprint FROM matching_runs WHERE id=?",
        (source_run_id,),
        "matching_runs",
    )
    if not rows:
        finding(
            "SOURCE_MATCHING_RUN_MISSING",
            f"source matching run {source_run_id} does not exist",
        )
        return issues
    matching_profile_id, matching_fingerprint = rows[0]
    if matching_profile_id != profile_id:
        finding(
            "SOURCE_MATCHING_RUN_PROFILE_MISMATCH",
            f"source matching run {source_run_id} belongs to profile"
            f" {matching_profile_id!r}, not {profile_id}",
        )
        # The map below only holds this profile's Matching history, and auditing
        # another profile's run from here would widen what this audit reads. The
        # cross-profile reference is the finding; nothing more can be said.
        return issues
    if source_fingerprint is None:
        finding(
            "SOURCE_MATCHING_RUN_FINGERPRINT_MISMATCH",
            f"copied source matching run fingerprint is unusable, stored matching"
            f" run {source_run_id} carries {matching_fingerprint!r}",
        )
    elif matching_fingerprint != source_fingerprint:
        finding(
            "SOURCE_MATCHING_RUN_FINGERPRINT_MISMATCH",
            f"copied {source_fingerprint!r} differs from matching run"
            f" {source_run_id} fingerprint {matching_fingerprint!r}",
        )
    audited = matching_results.get(source_run_id)
    if audited is None:
        finding(
            "SOURCE_MATCHING_RUN_MISSING",
            f"source matching run {source_run_id} is not in profile"
            f" {profile_id}'s audited matching history",
        )
    elif not audited.ok:
        finding(
            "SOURCE_MATCHING_RUN_INVALID",
            f"source matching run {source_run_id} does not survive the matching"
            f" persistence audit",
        )
    return issues


def _audit_run(
    connection: sqlite3.Connection,
    row: tuple,
    matching_results: dict[int, MatchingRunAuditResult],
) -> RecommendationRunAuditResult:
    (
        run_id,
        profile_id,
        source_run_id,
        persistence_version,
        input_assembly_version,
        engine_version,
        rules_version,
        source_fingerprint,
        batch_fingerprint,
        run_fingerprint,
        stored_count,
        batch_payload_json,
    ) = row
    issues: list[RecommendationPersistenceAuditIssue] = []

    def finding(code: str, detail: str) -> None:
        issues.append(_issue(_SCOPE_RUN, code, run_id, None, detail))

    # -- structural identity, before anything is recomputed from it ---------
    source_run_id = source_run_id if _is_positive_int(source_run_id) else None
    if source_run_id is None:
        finding(
            "RUN_SOURCE_MATCHING_RUN_ID_INVALID",
            f"source_matching_run_id {row[2]!r} is not a positive integer",
        )
    for code, description, value in (
        ("RUN_PERSISTENCE_VERSION_INVALID", "persistence_version", persistence_version),
        (
            "RUN_INPUT_ASSEMBLY_VERSION_INVALID",
            "input_assembly_version",
            input_assembly_version,
        ),
        (
            "RUN_ENGINE_VERSION_INVALID",
            "recommendation_engine_version",
            engine_version,
        ),
        ("RUN_RULES_VERSION_INVALID", "recommendation_rules_version", rules_version),
    ):
        if not _is_text(value):
            finding(code, f"{description} {value!r} is not stored text")
    versions_valid = all(
        _is_text(value)
        for value in (
            persistence_version,
            input_assembly_version,
            engine_version,
            rules_version,
        )
    )
    stored_source_fingerprint = (
        source_fingerprint if _is_fingerprint(source_fingerprint) else None
    )
    if stored_source_fingerprint is None:
        finding(
            "RUN_SOURCE_MATCHING_RUN_FINGERPRINT_INVALID",
            f"source_matching_run_fingerprint {source_fingerprint!r} is not a"
            " SHA-256 digest",
        )
    stored_batch_fingerprint = (
        batch_fingerprint if _is_fingerprint(batch_fingerprint) else None
    )
    if stored_batch_fingerprint is None:
        finding(
            "RUN_BATCH_FINGERPRINT_INVALID",
            f"batch_fingerprint {batch_fingerprint!r} is not a SHA-256 digest",
        )
    stored_run_fingerprint = (
        run_fingerprint if _is_fingerprint(run_fingerprint) else None
    )
    if stored_run_fingerprint is None:
        finding(
            "RUN_FINGERPRINT_INVALID",
            f"run_fingerprint {run_fingerprint!r} is not a SHA-256 digest",
        )
    if not _is_positive_int(stored_count):
        finding(
            "RUN_ASSESSMENT_COUNT_INVALID",
            f"assessment_count {stored_count!r} is not a positive integer",
        )

    # -- the ranking, exactly as persisted ----------------------------------
    rows = _query(
        connection,
        f"SELECT {_ASSESSMENT_COLUMNS} FROM recommendation_assessments"
        " WHERE run_id=? ORDER BY rank_position ASC",
        (run_id,),
        f"assessments of recommendation run {run_id}",
    )
    if len(rows) != stored_count:
        finding(
            "ASSESSMENT_COUNT_MISMATCH",
            f"stored assessment_count {stored_count!r} differs from row count"
            f" {len(rows)}",
        )
    identities: list[RecommendationRankedAssessmentIdentity] = []
    for position, assessment_row in enumerate(rows, start=1):
        identity, found = _audit_assessment(
            assessment_row, run_id, position, engine_version, rules_version
        )
        identities.append(identity)
        issues.extend(found)

    # A ranking is only reproducible when every position, posting and digest it
    # is built from was itself readable. Where it was not, the precise
    # prerequisite is already reported above and the two derived statements
    # below are skipped rather than reported as a second, misleading failure.
    positions = [item.rank_position for item in identities]
    ranking_sound = (
        bool(rows)
        and all(
            item.opportunity_id is not None
            and item.assessment_fingerprint is not None
            for item in identities
        )
        and positions == list(range(1, len(identities) + 1))
    )

    # -- the batch content statement ----------------------------------------
    _audit_batch_payload(
        batch_payload_json,
        run_id,
        engine_version,
        rules_version,
        stored_count,
        stored_batch_fingerprint,
        identities,
        ranking_sound,
        issues,
    )

    # -- the operational run identity ---------------------------------------
    if (
        stored_run_fingerprint is not None
        and stored_batch_fingerprint is not None
        and stored_source_fingerprint is not None
        and source_run_id is not None
        and versions_valid
        and ranking_sound
    ):
        # Persisted values only, and the ranked pairs in the persisted order.
        # Sorting them would let a re-ranked history reproduce the original
        # identity, which is exactly the corruption this recomputation exists
        # to detect. No timestamp, no database path, no current run id.
        recomputed = recommendation_run_fingerprint(
            profile_id=profile_id,
            source_matching_run_id=source_run_id,
            source_matching_run_fingerprint=stored_source_fingerprint,
            persistence_version=persistence_version,
            input_assembly_version=input_assembly_version,
            recommendation_engine_version=engine_version,
            recommendation_rules_version=rules_version,
            batch_fingerprint=stored_batch_fingerprint,
            ranked_assessments=tuple(
                (item.opportunity_id, item.assessment_fingerprint)
                for item in identities
            ),
        )
        if recomputed != stored_run_fingerprint:
            finding(
                "RUN_FINGERPRINT_MISMATCH",
                "run fingerprint differs from the stored run content",
            )

    issues.extend(
        _audit_source_matching_provenance(
            connection,
            run_id,
            profile_id,
            source_run_id,
            stored_source_fingerprint,
            matching_results,
        )
    )
    ordered = _ordered(issues)
    return RecommendationRunAuditResult(
        run_id,
        stored_run_fingerprint,
        stored_batch_fingerprint,
        source_run_id,
        stored_source_fingerprint,
        tuple(identities),
        len(rows),
        not ordered,
        ordered,
    )


def _audit_batch_payload(
    batch_payload_json: object,
    run_id: int,
    engine_version: object,
    rules_version: object,
    stored_count: object,
    stored_batch_fingerprint: str | None,
    identities: list[RecommendationRankedAssessmentIdentity],
    ranking_sound: bool,
    issues: list[RecommendationPersistenceAuditIssue],
) -> None:
    """Audit `recommendation_runs.batch_payload_json` against the stored rows.

    The expected content is the batch's own statement — its two versions, the
    size it declares, and its ranked assessment digests **in persisted rank
    order**. The order is never normalized: sorting the digests alphabetically
    or by opportunity id would make two differently-ranked histories agree, and
    the ranking is the product of Phase 9A.

    The stored `batch_fingerprint` is a *content* identity and is checked as the
    digest of exactly what is stored, so it stays comparable to the digest Phase
    9A computed. It is never used as the run's operational identity.
    """

    def finding(code: str, detail: str) -> None:
        issues.append(_issue(_SCOPE_RUN, code, run_id, None, detail))

    try:
        decoded = json.loads(batch_payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        finding("BATCH_PAYLOAD_INVALID_JSON", "batch payload is not valid JSON")
        return
    if not isinstance(decoded, dict):
        finding("BATCH_PAYLOAD_NOT_OBJECT", "batch payload is not a JSON object")
        return
    canonical = _canonical_utf8(decoded)
    if canonical is None:
        # As for an assessment payload: the canonical form of this batch has no
        # UTF-8 bytes, so it can neither be compared with what is stored nor
        # hashed, and both statements are skipped instead of being invented.
        # The comparison against the stored rows below needs neither and still
        # runs, and so does the rest of this profile's audit.
        finding(
            "BATCH_PAYLOAD_INVALID_JSON",
            "batch payload is not representable as canonical UTF-8 JSON",
        )
    else:
        canonical_text, canonical_bytes = canonical
        if canonical_text != batch_payload_json:
            finding(
                "BATCH_PAYLOAD_NOT_CANONICAL", "batch payload is not canonical JSON"
            )
        if stored_batch_fingerprint is not None and _digest(canonical_bytes) != (
            stored_batch_fingerprint
        ):
            finding(
                "BATCH_FINGERPRINT_MISMATCH",
                "batch fingerprint differs from the stored batch payload",
            )
    if not ranking_sound:
        return
    expected = {
        "recommendation_engine_version": engine_version,
        "recommendation_rules_version": rules_version,
        "assessment_count": stored_count,
        "ranked_assessment_fingerprints": [
            item.assessment_fingerprint for item in identities
        ],
    }
    if decoded != expected:
        finding(
            "BATCH_CONTENT_MISMATCH",
            "batch payload differs from the stored run and assessment rows",
        )


# --------------------------------------------------------------------------
# readiness issues
# --------------------------------------------------------------------------

#: The order `input_assembly._incomplete` sorts its issues into before anything
#: ever stores them: profile-wide issues first, then per-opportunity ones grouped
#: by posting, then by code. It is restated here as a *verification* key and is
#: never used to re-sort what was read — repairing the order would hide exactly
#: the corruption it detects.
def _stored_readiness_sort_key(entry: dict[str, Any]) -> tuple:
    opportunity_id = entry["opportunity_id"]
    return (
        opportunity_id is not None,
        opportunity_id or 0,
        entry["code"],
    )


def _audit_readiness_issues(
    raw: object,
    profile_id: int,
    issues: list[RecommendationPersistenceAuditIssue],
) -> list[dict[str, Any]] | None:
    """Audit `readiness_issues_json` under the vocabulary Phase 9A already owns.

    `RecommendationReadinessIssueCode` is the one readiness vocabulary in this
    project and no second one is invented here. Each stored entry is exactly
    `{"code", "message", "opportunity_id"}`, the code is one this build knows,
    the message is non-blank text, and `opportunity_id` is null or a positive
    integer that is not a boolean.

    Unlike 9B.2a's reader this accumulates: every malformed entry is reported,
    not just the first, because an audit that stopped at the first bad value
    would answer "can I read this?" rather than "what is corrupt here?".

    Returns the decoded entries when the array itself was readable, so the
    caller can ask whether it is empty, and `None` when it was not.
    """

    def finding(code: str, opportunity_id: int | None, detail: str) -> None:
        issues.append(
            _issue(_SCOPE_PROFILE, code, None, opportunity_id, detail)
        )

    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        finding(
            "READINESS_ISSUES_INVALID_JSON",
            None,
            "readiness_issues_json is not valid JSON",
        )
        return None
    if not isinstance(decoded, list):
        finding(
            "READINESS_ISSUES_NOT_ARRAY",
            None,
            "readiness_issues_json is not a JSON array",
        )
        return None
    canonical = _canonical_utf8(decoded)
    if canonical is None:
        # The array parsed, but one of its strings decodes to something strict
        # UTF-8 cannot carry, so it has no canonical form to be compared with.
        # The entries below are still audited — a code, a posting and the stored
        # order can all be judged without encoding anything — and the entry is
        # never rewritten into a valid-looking readiness issue: this finding
        # stands, and `_stable_value` carries the unencodable string into the
        # audit fingerprint tagged as what it is.
        finding(
            "READINESS_ISSUES_INVALID_JSON",
            None,
            "readiness_issues_json is not representable as canonical UTF-8 JSON",
        )
    elif canonical[0] != raw:
        finding(
            "READINESS_ISSUES_NOT_CANONICAL",
            None,
            "readiness_issues_json is not canonical JSON",
        )
    orderable: list[dict[str, Any]] = []
    for index, entry in enumerate(decoded):
        # The index makes each finding distinguishable from an identical one
        # about another entry, and it is a property of the stored array rather
        # than of this execution, so it stays deterministic.
        where = f"readiness issue at index {index}"
        if not isinstance(entry, dict) or set(entry) != _READINESS_KEYS:
            finding(
                "READINESS_ISSUE_MALFORMED",
                None,
                f"{where} is not a {{code, message, opportunity_id}} object",
            )
            continue
        code, message, opportunity_id = (
            entry["code"],
            entry["message"],
            entry["opportunity_id"],
        )
        identity = opportunity_id if _is_positive_int(opportunity_id) else None
        usable = True
        if not isinstance(code, str) or code not in _READINESS_CODES:
            finding(
                "READINESS_ISSUE_CODE_UNKNOWN",
                identity,
                f"{where} carries unknown code {code!r}",
            )
            usable = False
        if not isinstance(message, str) or not message.strip():
            finding(
                "READINESS_ISSUE_MESSAGE_INVALID",
                identity,
                f"{where} carries a message that is not text: {message!r}",
            )
        if opportunity_id is not None and identity is None:
            finding(
                "READINESS_ISSUE_OPPORTUNITY_ID_INVALID",
                None,
                f"{where} carries opportunity_id {opportunity_id!r},"
                " which is neither null nor a positive integer",
            )
            usable = False
        if usable:
            orderable.append(entry)
    # Ordering is only decidable over entries whose code and posting were
    # themselves readable; where they were not, the precise defect is already
    # reported and a derived ordering complaint would be noise.
    if len(orderable) == len(decoded) and sorted(
        orderable, key=_stored_readiness_sort_key
    ) != orderable:
        finding(
            "READINESS_ISSUES_NOT_ORDERED",
            None,
            "readiness issues are not in input assembly order",
        )
    return decoded


# --------------------------------------------------------------------------
# profile state
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProfileStateAudit:
    """What the state audit establishes, kept internal on purpose.

    `status` and `current_run_id` are what the report exposes. `identity` is the
    *stable persisted identity* of `recommendation_profile_state` and exists for
    the audit fingerprint, which must distinguish two states that are both
    perfectly valid and therefore produce no finding at all — two INCOMPLETE
    rows carrying different readiness issues, say, or the same state written
    under different stored versions. Judging the digest by `status`,
    `current_run_id` and the findings alone would collapse those into one.

    It carries no timestamp: `created_at` and `updated_at` are never read.
    """

    status: str
    current_run_id: int | None
    identity: dict[str, Any]


def _readiness_identity(raw: object, entries: list[Any] | None) -> dict[str, Any]:
    """The stored readiness array's deterministic contribution to the digest.

    A decodable array contributes its decoded entries **in stored order**. The
    order is part of the state contract — `input_assembly` sorts the issues once,
    before they are ever stored — so it is preserved here rather than sorted, for
    the same reason the audit verifies that order instead of repairing it. Among
    valid states the decoded value and the stored bytes determine each other,
    canonical JSON being a bijection, so nothing is lost by carrying the
    structure rather than the text.

    The entries go through `_stable_value`, which is a no-op for every value a
    valid state can hold and tags the one thing this digest cannot be taken
    over: a decoded string strict UTF-8 refuses to encode.

    A value that is not a decodable array has no structure to carry, so the
    stored text itself is the identity; the findings already say what is wrong
    with it.
    """
    if entries is None:
        return {"decoded": False, "raw": _stable_value(raw)}
    return {"decoded": True, "issues": _stable_value(entries)}


def _audit_state(
    connection: sqlite3.Connection,
    profile_id: int,
    run_versions: dict[int, tuple[object, object]],
    issues: list[RecommendationPersistenceAuditIssue],
) -> _ProfileStateAudit:
    """Audit `recommendation_profile_state`, and never write or repair it.

    NOT_SYNCED is the absence of a row, and it is only honest when nothing has
    ever run: history without a state row is a pointer that was lost, and is a
    finding rather than a fourth state.

    READY and INCOMPLETE are held to the same semantics 9B.2a reads them under —
    READY names a run this profile owns and carries the canonical empty issue
    array; INCOMPLETE names none and carries a canonical, non-empty, correctly
    ordered one. Historical runs may exist beside an INCOMPLETE state: the last
    synchronization failed, the ones before it did not, and their runs are still
    exactly what was recommended then.

    The stored versions are compared to the current run's, never to the
    constants this build ships. An old run read under a new build is history,
    not corruption, and that comparison would be freshness — Phase 9B.3's
    question.
    """

    def finding(code: str, run_id: int | None, detail: str) -> None:
        issues.append(_issue(_SCOPE_PROFILE, code, run_id, None, detail))

    rows = _query(
        connection,
        """SELECT state,current_run_id,persistence_version,input_assembly_version,
        readiness_issues_json FROM recommendation_profile_state WHERE profile_id=?""",
        (profile_id,),
        "recommendation_profile_state",
    )
    if not rows:
        if run_versions:
            finding(
                "STATE_ROW_MISSING",
                None,
                f"profile {profile_id} has {len(run_versions)} recommendation"
                " run(s) and no state row",
            )
        # The absence of the row is itself a state, and it is stated rather
        # than left as a gap: `present: False` cannot be reached by any stored
        # row, so NOT_SYNCED can never digest as some other state.
        return _ProfileStateAudit(_STATUS_NOT_SYNCED, None, {"present": False})
    (
        state,
        current_run_id,
        persistence_version,
        input_assembly_version,
        readiness_issues_json,
    ) = rows[0]
    current = current_run_id if _is_positive_int(current_run_id) else None
    for code, description, value in (
        (
            "STATE_PERSISTENCE_VERSION_INVALID",
            "persistence_version",
            persistence_version,
        ),
        (
            "STATE_INPUT_ASSEMBLY_VERSION_INVALID",
            "input_assembly_version",
            input_assembly_version,
        ),
    ):
        if not _is_text(value):
            finding(code, None, f"state {description} {value!r} is not stored text")
    entries = _audit_readiness_issues(readiness_issues_json, profile_id, issues)
    # Every stored column of the row, as stored — not as the report chose to
    # summarize it. `current_run_id` is the raw value rather than the gated one
    # so that two differently malformed pointers stay distinguishable.
    identity = {
        "present": True,
        "state": _stable_value(state),
        "current_run_id": _stable_value(current_run_id),
        "persistence_version": _stable_value(persistence_version),
        "input_assembly_version": _stable_value(input_assembly_version),
        "readiness_issues": _readiness_identity(readiness_issues_json, entries),
    }

    if state == _STATUS_READY:
        if current is None:
            finding(
                "STATE_CURRENT_RUN_INVALID",
                None,
                f"READY current_run_id {current_run_id!r} is not a positive integer",
            )
        elif current not in run_versions:
            finding(
                "STATE_CURRENT_RUN_UNKNOWN",
                current,
                f"current run {current} is not in profile {profile_id}'s"
                " recommendation history",
            )
        elif (persistence_version, input_assembly_version) != run_versions[current]:
            finding(
                "STATE_VERSION_MISMATCH",
                current,
                f"state versions {(persistence_version, input_assembly_version)!r}"
                f" disagree with current run {run_versions[current]!r}",
            )
        if readiness_issues_json != _NO_READINESS_ISSUES:
            finding(
                "STATE_READY_HAS_READINESS_ISSUES",
                current,
                "READY state does not carry the canonical empty readiness array",
            )
        return _ProfileStateAudit(_STATUS_READY, current, identity)
    if state == _STATUS_INCOMPLETE:
        if current_run_id is not None:
            finding(
                "STATE_INCOMPLETE_NAMES_RUN",
                current,
                f"INCOMPLETE state names current run {current_run_id!r}",
            )
        if entries is not None and not entries:
            finding(
                "READINESS_ISSUES_EMPTY",
                None,
                "INCOMPLETE state carries no readiness issue",
            )
        return _ProfileStateAudit(_STATUS_INCOMPLETE, current, identity)
    finding(
        "STATE_UNKNOWN",
        current,
        f"unknown recommendation state {state!r}",
    )
    return _ProfileStateAudit(_STATUS_UNKNOWN, current, identity)


# --------------------------------------------------------------------------
# the public audit
# --------------------------------------------------------------------------


def _matching_run_results(
    connection: sqlite3.Connection, profile_id: int
) -> dict[int, MatchingRunAuditResult]:
    """Phase 4's audit of this profile's Matching history, run once, keyed by id.

    One call inside this audit's own snapshot, so every recommendation run's
    provenance is judged against the same Matching picture. Matching's audit
    holds no transaction of its own and reads through this connection, so its
    SELECTs join the snapshot already open rather than opening one beside it.

    The *report* is only a carrier here; what each recommendation run consults
    is the per-run result below. Matching's global `ok` is deliberately not
    stored: it would let one corrupt unrelated historical Matching run invalidate
    every recommendation run in this profile's history.
    """
    try:
        report = audit_matching_profile_history(connection, profile_id)
    except (MatchingPersistenceAuditError, sqlite3.Error) as error:
        # Both, because Matching reads some of its rows outside its own
        # `sqlite3.Error` boundary: schema damage there — a `matching_assessments`
        # table a restore never recreated, say — surfaces as a raw
        # `sqlite3.OperationalError`, and letting it out of this function would
        # break the one promise this module's callers are given, that a failed
        # audit is a `RecommendationPersistenceAuditError`. It is translated at
        # this boundary rather than fixed in Matching, whose audit is its own
        # phase's contract. Nothing wider is caught: an unexpected failure is
        # still a bug and must still look like one, and the cause is always
        # chained so the original error survives.
        raise RecommendationPersistenceAuditError(
            f"cannot audit the matching history of profile {profile_id}: {error}"
        ) from error
    return {item.run_id: item for item in report.runs}


def _stable_payload(
    profile_id: int,
    state: _ProfileStateAudit,
    runs: tuple[RecommendationRunAuditResult, ...],
    issues: tuple[RecommendationPersistenceAuditIssue, ...],
) -> dict[str, Any]:
    """Everything the audit fingerprint may depend on, and nothing else.

    Stable persisted identities and deterministic structured findings, in the
    orders the report itself carries. No audit timestamp, no duration, no
    database path, no process value and no wall-clock time reaches this — and
    nothing about Matching's *current* run either, so a superseding Matching run
    cannot move a recommendation history's digest.

    `state` carries the persisted state row whole, not only the two fields the
    report surfaces. Findings alone are not enough to identify a state: two
    INCOMPLETE rows naming different readiness issues, or one state row rewritten
    under a different stored version, can both be entirely valid and produce no
    finding at all — and a digest that could not tell them apart would be
    claiming an equivalence that does not hold.
    """
    return dict(
        audit_version=RECOMMENDATION_PERSISTENCE_AUDIT_VERSION,
        profile_id=profile_id,
        status=state.status,
        current_run_id=state.current_run_id,
        state=state.identity,
        runs=[
            {
                "run_id": run.run_id,
                "run_fingerprint": run.run_fingerprint,
                "batch_fingerprint": run.batch_fingerprint,
                "source_matching_run_id": run.source_matching_run_id,
                "source_matching_run_fingerprint": (
                    run.source_matching_run_fingerprint
                ),
                "assessment_count": run.assessment_count,
                "ranked_assessments": [
                    {
                        "rank_position": item.rank_position,
                        "opportunity_id": item.opportunity_id,
                        "assessment_fingerprint": item.assessment_fingerprint,
                    }
                    for item in run.ranked_assessments
                ],
            }
            for run in runs
        ],
        issues=[
            {
                "scope": item.scope,
                "code": item.code,
                "run_id": item.run_id,
                "opportunity_id": item.opportunity_id,
                "detail": item.detail,
            }
            for item in issues
        ],
    )


def audit_recommendation_profile_history(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationPersistenceAuditReport:
    """Audit one profile's whole persisted recommendation history, read-only.

    Every persisted run is audited — never only the current one — in a
    deterministic order, and one corrupt run never stops the rest from being
    audited. The report is a statement about what is stored: it recomputes no
    recommendation, repairs nothing, writes nothing, and asks no freshness
    question.

    Raises `RecommendationPersistenceAuditError` only when the audit itself
    cannot be performed: an invalid `profile_id`, a profile that does not exist,
    or a SQLite/schema failure. Persisted corruption becomes structured issues
    and `ok = False`.
    """
    if not _is_positive_int(profile_id):
        raise RecommendationPersistenceAuditError(
            "profile_id must be a positive integer"
        )
    with _audit_snapshot(
        connection, f"recommendation persistence audit of profile {profile_id}"
    ):
        if not _query(
            connection, "SELECT 1 FROM profiles WHERE id=?", (profile_id,), "profiles"
        ):
            raise RecommendationPersistenceAuditError(
                f"profile {profile_id} does not exist"
            )
        # Newest first, the same deterministic history order Matching's audit
        # and 9B.2a's listing use. It orders the runs; it never re-orders the
        # ranking inside one.
        raw_runs = _query(
            connection,
            f"SELECT {_RUN_COLUMNS} FROM recommendation_runs WHERE profile_id=?"
            " ORDER BY id DESC",
            (profile_id,),
            "recommendation_runs",
        )
        matching_results = (
            _matching_run_results(connection, profile_id) if raw_runs else {}
        )
        runs = tuple(_audit_run(connection, row, matching_results) for row in raw_runs)
        run_versions = {row[0]: (row[3], row[4]) for row in raw_runs}
        issues: list[RecommendationPersistenceAuditIssue] = []
        state = _audit_state(connection, profile_id, run_versions, issues)
        issues.extend(issue for run in runs for issue in run.issues)
        ordered = _ordered(issues)
        fingerprint = recommendation_persistence_audit_fingerprint(
            **_stable_payload(profile_id, state, runs, ordered)
        )
    return RecommendationPersistenceAuditReport(
        RECOMMENDATION_PERSISTENCE_AUDIT_VERSION,
        profile_id,
        state.status,
        state.current_run_id,
        len(raw_runs),
        len(runs),
        sum(run.assessment_count for run in runs),
        not ordered,
        ordered,
        runs,
        fingerprint,
    )
