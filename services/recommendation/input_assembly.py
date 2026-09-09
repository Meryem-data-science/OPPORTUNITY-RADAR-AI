"""Read-only assembly, freshness preflight and readiness for Phase 9A inputs.

Six persisted sources are read here and **nothing is written, classified,
geocoded, evaluated or repaired**:

    1. the profile and the user that owns it        `profiles`
    2. the current Matching snapshot                Phase 4, `read_current_matching`
    3. the profile's declared preferences           Phase 3, via Matching's loader
    4. the persisted fine classification            Phase 8, decoded as written
    5. the persisted location resolutions           Phase 7, plus the profile target
    6. the stored eligibility decision              Phase 3.6, plus its reasons

A recommendation is only as honest as the age of what it reads, so each upstream
is checked against the evidence *it* defines as current, using that phase's own
primitives — never a rule invented here:

    Matching persistence   `audit_matching_profile_history`, restricted to the
                           run the profile currently points at. A corrupt
                           historical run is somebody else's problem; a corrupt
                           current run is this one's.
    Qualification          the persisted Phase 8 row's `(input_fingerprint,
                           classifier_version, fine_classifier_version)` must
                           equal what `persist_qualifications` would leave
                           alone: Phase 8's own `input_fingerprint` over the
                           posting's fields today, plus the two rule versions
                           running now. No classifier runs.
    Matching semantic      two digests, because one cannot answer both
                           questions. `semantic_corpus_fingerprint` asks *is
                           this the same corpus content* and is ID-free by
                           design; `semantic_binding_fingerprint` asks *is each
                           document still attached to the same posting*, which
                           is what catches two postings exchanging their texts.
                           Both are compared against the persisted run. No
                           TF-IDF is fitted for either.
    Matching structured    the recomputed `role-domain-preferences-v2` result
                           must fingerprint to the snapshot's own
                           `domain.upstream_fingerprint`.
    Matching skill fit     the recomputed `skill-fit-v1` result must fingerprint
                           to the snapshot's `required_skill.upstream_fingerprint`.
    Geography              every current `opportunity_constraint_locations` row
                           of the posting must already be projected under
                           `(location_fingerprint(text), RESOLVER_VERSION)`, and
                           the projection must cover exactly those rows.
    Eligibility            the stored decision's `(engine_version,
                           input_fingerprint)` must equal what the **current**
                           inputs digest to, computed with Phase 3.6's own
                           `eligibility_fingerprint`. No verdict is recomputed.

Any of those failing makes the cohort `INCOMPLETE` with **no records at all**.
That is deliberate on both counts: a stale `INELIGIBLE` must never be published
as a `KNOWN_BLOCKER`, a stale projection must never be published as
`OUT_OF_TARGET`, a domain must never be read off a classification of text the
posting no longer carries, and a half-assembled cohort would rank an opportunity
against a corpus missing its competitors. Nothing is resynchronized to fix it —
an operator runs the phase that owns the data.

Three of the checks are cohort-wide and run before any assessment is assembled:
qualification provenance, then the semantic corpus it lets us believe, then the
binding of that corpus to the postings it was fitted for. Content before
binding, so an ordinary title or description edit still reports the corpus code
rather than the identity one. The rest are per-posting.

A Matching run persisted before the binding provenance existed is valid history
— `read_current_matching` returns it and Phase 4's own audit passes it — and is
still not a basis for recommending: nothing in it shows its percentiles belong
to these postings. That is `STALE_MATCHING_SEMANTIC_BINDING` too, and the cure
is `sync_matching`, never a repair applied here. Note that the qualification check is stricter than the *public*
read model on purpose: `fine_read_model` still reports a NULL fine classifier
version as the documented legacy state, because that is a true statement about a
row, and `/api/opportunities` is unchanged. A row nothing has fine-classified is
simply not a *current* Phase 8 projection, and recommending a domain off it would
be using Phase 8 as though it had run.

The shape follows `services/priority/input_assembly.py` — a readiness status, a
list of explicit issues, a stable order, no partial output. Two of its rules are
deliberately different, and both come from Phase 9's own contract:

* **a missing eligibility decision is not an issue.** Priority refuses to
  prioritize a pair it has no decision for; recommendation carries the absence
  through as `EligibilitySignalStatus.MISSING`, which routes the opportunity to
  UNCERTAIN. Refusing the whole cohort because one posting was never evaluated
  would hide the other ninety-nine, and turning the absence into a verdict is
  exactly what Phase 3.6 forbids. A decision that exists but no longer describes
  the current inputs is the opposite case, and it stops the cohort;
* **a legacy fine classification is not a current Phase 8 projection.** A row
  migration `0025` reached and the fine classifier never did — a NULL
  `fine_classifier_version` — is a documented persisted state and stays a valid
  *public* read: `fine_read_model` reports it as "never fine-classified" and
  `/api/opportunities` publishes it unchanged, because that is a true statement
  about the row. Recommendation asks a stricter question, *is this the reading
  the classifier running now would leave alone*, and a row nothing has
  fine-classified is not one. It therefore stops the cohort with
  `QUALIFICATION_PROJECTION_STALE` rather than recommending a domain off Phase 8
  as though Phase 8 had run.

  This is about stale provenance and not about the fine-or-coarse algorithm: a
  **current** fine category that simply has no honest canonical family —
  `NLP`, `COMPUTER_VISION`, `OTHER` — is not stale, and still takes the approved
  coarse-domain fallback.
"""

from __future__ import annotations

import math
import numbers
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    AlignmentReason,
    AlignmentStatus,
    MatchingInput,
    MatchingInputError,
    MatchingReadError,
    MatchLane,
    SemanticSimilarityStatus,
    MatchingPersistenceAuditError,
    audit_matching_profile_history,
    build_opportunity_skill_signals,
    build_role_domain_preference_signals,
    build_skill_fit,
    load_opportunity_matching_input,
    load_profile_matching_input,
    SEMANTIC_BINDING_VERSION,
    build_opportunity_semantic_document,
    read_current_matching,
    select_matching_opportunity_ids,
    semantic_binding_fingerprint,
    semantic_corpus_fingerprint,
    skill_fit_fingerprint,
)
from services.collector.matching.skill_fit import SkillFitInputError
from services.collector.matching.role_domain_preferences import (
    RoleDomainPreferencesInputError,
)
from services.collector.matching.role_domain_preferences_fingerprint import (
    role_domain_preferences_fingerprint,
)
from services.collector.matching.models import MatchingOpportunityInput
from services.collector.qualification.classifier import CLASSIFIER_VERSION
from services.collector.qualification.fine_classifier import FINE_CLASSIFIER_VERSION
from services.collector.qualification.fine_read_model import (
    FineClassificationDecodeError,
    decode_fine_classification,
)
from services.collector.qualification.persistence import input_fingerprint
from services.digital_twin.preferences.models import OpportunityPreferences
from services.digital_twin.preferences.repository import get_profile_preferences
from services.eligibility.fingerprint import eligibility_fingerprint
from services.eligibility.inputs import (
    EligibilityInputError,
    load_opportunity_input,
    load_profile_input,
)
from services.eligibility.models import EligibilityInput
from services.eligibility.repository import read_eligibility, read_rule_results
from services.geography.models import (
    RESOLVER_VERSION,
    LocationResolution,
    LocationSource,
)
from services.geography.profile_target import resolve_profile_target
from services.geography.repository import (
    read_opportunity_resolutions,
    stored_signature,
)
from services.geography.resolver import location_fingerprint
from services.geography.service import load_location_sources

from .models import (
    RecommendationEligibilityInput,
    RecommendationFineClassification,
    RecommendationGeographyInput,
    RecommendationInput,
    RecommendationMatchingSnapshot,
)

RECOMMENDATION_INPUT_ASSEMBLY_VERSION = "recommendation-input-assembly-v1"

__all__ = [
    "RECOMMENDATION_INPUT_ASSEMBLY_VERSION",
    "RecommendationInputAssemblyResult",
    "current_location_signature",
    "current_semantic_binding_fingerprint",
    "current_semantic_corpus_fingerprint",
    "RecommendationInputRecord",
    "RecommendationOpportunityContext",
    "RecommendationReadinessIssue",
    "RecommendationReadinessIssueCode",
    "RecommendationReadinessStatus",
    "assemble_recommendation_inputs",
]


class RecommendationReadinessStatus(StrEnum):
    READY = "READY"
    INCOMPLETE = "INCOMPLETE"


class RecommendationReadinessIssueCode(StrEnum):
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    PROFILE_OWNER_INVALID = "PROFILE_OWNER_INVALID"
    MATCHING_NOT_READY = "MATCHING_NOT_READY"
    MATCHING_VERSION_STALE = "MATCHING_VERSION_STALE"
    STALE_MATCHING_COHORT = "STALE_MATCHING_COHORT"
    #: The current Matching run does not survive Phase 4's own persistence
    #: audit: a payload, a fingerprint or a version disagrees with itself.
    MATCHING_PERSISTENCE_INVALID = "MATCHING_PERSISTENCE_INVALID"
    #: The recomputed role/domain/preference alignment no longer fingerprints to
    #: the one the snapshot was built from.
    #: The persisted semantic corpus was fitted over documents the current
    #: postings no longer produce, even though the cohort's ids did not move.
    STALE_MATCHING_SEMANTIC_CORPUS = "STALE_MATCHING_SEMANTIC_CORPUS"
    #: The corpus content still matches, but the documents no longer belong to
    #: the same postings — or the run predates that provenance entirely.
    STALE_MATCHING_SEMANTIC_BINDING = "STALE_MATCHING_SEMANTIC_BINDING"
    STALE_MATCHING_SNAPSHOT = "STALE_MATCHING_SNAPSHOT"
    #: The recomputed skill fit no longer fingerprints to the one the snapshot's
    #: required-skill ratio was computed from.
    STALE_MATCHING_SKILL_FIT = "STALE_MATCHING_SKILL_FIT"
    OPPORTUNITY_MISSING = "OPPORTUNITY_MISSING"
    INVALID_MATCHING_PAYLOAD = "INVALID_MATCHING_PAYLOAD"
    FINE_CLASSIFICATION_INVALID = "FINE_CLASSIFICATION_INVALID"
    #: The persisted Phase 8 row was derived from other opportunity fields, or
    #: by another coarse or fine rule system, than the ones running now.
    QUALIFICATION_PROJECTION_STALE = "QUALIFICATION_PROJECTION_STALE"
    #: A Phase 7 projection that no longer reads the posting's current location
    #: rows, or reads them under another resolver version.
    GEOGRAPHY_PROJECTION_STALE = "GEOGRAPHY_PROJECTION_STALE"
    #: A stored Eligibility decision whose engine version or input fingerprint
    #: no longer matches what the current inputs produce.
    ELIGIBILITY_SNAPSHOT_STALE = "ELIGIBILITY_SNAPSHOT_STALE"
    #: A stored Eligibility decision exists, but the inputs needed to prove it
    #: is still current can no longer be assembled.
    ELIGIBILITY_INPUT_INCOMPLETE = "ELIGIBILITY_INPUT_INCOMPLETE"
    INVALID_UPSTREAM_VALUE = "INVALID_UPSTREAM_VALUE"


@dataclass(frozen=True)
class RecommendationReadinessIssue:
    code: RecommendationReadinessIssueCode
    message: str
    opportunity_id: int | None = None


@dataclass(frozen=True)
class RecommendationOpportunityContext:
    """Presentation-only identity, carried beside the input and never scored."""

    opportunity_id: int
    canonical_title: str
    organization: str
    source_url: str
    application_url: str | None
    canonical_url: str | None


@dataclass(frozen=True)
class RecommendationInputRecord:
    recommendation_input: RecommendationInput
    context: RecommendationOpportunityContext


@dataclass(frozen=True)
class RecommendationInputAssemblyResult:
    assembly_version: str
    status: RecommendationReadinessStatus
    profile_id: int
    user_id: int | None
    matching_run_id: int | None
    issues: tuple[RecommendationReadinessIssue, ...]
    records: tuple[RecommendationInputRecord, ...]


def _issue(code, message, opportunity_id=None) -> RecommendationReadinessIssue:
    return RecommendationReadinessIssue(code, message, opportunity_id)


def _incomplete(profile_id, issues, user_id=None, run_id=None):
    ordered = tuple(
        sorted(
            issues,
            key=lambda item: (
                item.opportunity_id is not None,
                item.opportunity_id or 0,
                item.code.value,
            ),
        )
    )
    return RecommendationInputAssemblyResult(
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        RecommendationReadinessStatus.INCOMPLETE,
        profile_id,
        user_id,
        run_id,
        ordered,
        (),
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _ratio(value: object, *, optional: bool = True) -> float | None:
    """Accept a persisted `[0, 1]` number, or `None` when one is allowed."""
    if value is None:
        if not optional:
            raise ValueError("a required ratio is missing")
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError("value is not a ratio within [0, 1]")
    return float(value)


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("value is not a non-negative integer")
    return value


def _section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = payload.get(name)
    if not isinstance(section, Mapping):
        raise ValueError(f"persisted assessment has no {name} section")
    return section


def _snapshot(
    profile_id: int, assessment, run
) -> RecommendationMatchingSnapshot:
    """Read one persisted matching assessment payload, strictly, as written.

    Every value is validated against the contract `matching-engine-v1` wrote it
    under. Nothing is defaulted: a payload this cannot read is an issue for the
    caller to report, never a component quietly treated as unavailable.
    """
    payload = assessment.assessment_payload
    if not isinstance(payload, Mapping):
        raise ValueError("persisted assessment payload is not an object")
    required = _section(payload, "required_skill")
    semantic = _section(payload, "semantic")
    domain = _section(payload, "domain")
    opportunity_type = _section(payload, "opportunity_type")
    if not _is_sha256(assessment.assessment_fingerprint):
        raise ValueError("persisted assessment fingerprint is not a SHA-256 digest")
    if not _is_sha256(domain.get("upstream_fingerprint")):
        raise ValueError("persisted domain upstream fingerprint is not a SHA-256 digest")
    if not _is_sha256(required.get("upstream_fingerprint")):
        raise ValueError(
            "persisted required-skill upstream fingerprint is not a SHA-256 digest"
        )
    coverage = _ratio(assessment.evidence_coverage, optional=False)
    quality = _ratio(assessment.match_quality)
    if (coverage == 0.0) != (quality is None):
        raise ValueError("match quality and evidence coverage disagree")
    semantic_status = SemanticSimilarityStatus(semantic.get("status"))
    percentile = _ratio(semantic.get("percentile"))
    if (semantic_status is SemanticSimilarityStatus.AVAILABLE) != (
        percentile is not None
    ):
        raise ValueError("semantic percentile and status disagree")
    return RecommendationMatchingSnapshot(
        profile_id=profile_id,
        opportunity_id=assessment.opportunity_id,
        lane=MatchLane(assessment.lane),
        match_quality=quality,
        evidence_coverage=coverage,
        assessment_fingerprint=assessment.assessment_fingerprint,
        required_skill_score=_ratio(required.get("normalized_score")),
        required_skill_matched_count=_count(required.get("matched_count")),
        required_skill_total_count=_count(required.get("total_count")),
        required_skill_upstream_fingerprint=str(required.get("upstream_fingerprint")),
        semantic_status=semantic_status,
        semantic_percentile=percentile,
        domain_status=AlignmentStatus(domain.get("status")),
        domain_reason=AlignmentReason(domain.get("reason")),
        domain_preferred_rank=(
            None
            if domain.get("preferred_rank") is None
            else _count(domain.get("preferred_rank"))
        ),
        domain_normalized_score=_ratio(domain.get("normalized_score")),
        opportunity_type_status=AlignmentStatus(opportunity_type.get("status")),
        opportunity_type_reason=AlignmentReason(opportunity_type.get("reason")),
        structured_upstream_fingerprint=str(domain.get("upstream_fingerprint")),
        matching_engine_version=run.matching_engine_version,
        matching_rules_version=run.matching_rules_version,
        semantic_percentile_version=run.semantic_percentile_version,
    )


def _fine_classification(
    connection: sqlite3.Connection, opportunity_id: int
) -> RecommendationFineClassification:
    """Decode the persisted Phase 8 half with the qualification's own reader.

    Only two of its five values are read. The other three are decoded all the
    same, because the reader's coherence rules — `OTHER` is never evidenced, an
    unqualified row carries no category, a qualified one carries exactly one —
    are what makes reading the two safe. That reader lives beside the classifier
    it reads, so this domain does not import the HTTP layer to reach it.
    """
    row = connection.execute(
        """SELECT q.qualification, q.fine_primary_category,
                  q.fine_secondary_categories_json, q.fine_category_evidence_json,
                  q.fine_reasons_json, q.fine_classifier_version
             FROM opportunities AS o
             LEFT JOIN opportunity_qualifications AS q ON q.opportunity_id = o.id
            WHERE o.id = ?""",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        raise FineClassificationDecodeError(f"opportunity {opportunity_id} disappeared")
    decoded = decode_fine_classification(*row)
    return RecommendationFineClassification(
        primary_category=decoded.primary_category,
        classifier_version=decoded.classifier_version,
    )


def current_location_signature(location_text: str) -> tuple[str, str]:
    """The `(fingerprint, version)` a current Phase 7 projection must carry.

    Phase 7's own idempotence key, restated by calling Phase 7's own function.
    A projection stored under any other pair was read from another string, or by
    another resolver, and is therefore not a reading of what the posting says
    now.
    """
    return location_fingerprint(location_text), RESOLVER_VERSION


def _geography_is_current(
    connection: sqlite3.Connection,
    opportunity_id: int,
    sources: Sequence[LocationSource],
    resolutions: Sequence[LocationResolution],
) -> str | None:
    """Return why the projection is stale, or None when it is current.

    Three ways it can be stale, and none of them is repaired here:

    * a current source row carries no stored signature at all — never projected,
      or projected into rows that disagree with each other;
    * a current source row is projected under another text or another resolver
      version — `stored_signature` says which pair it holds;
    * the projection covers a set of source rows other than the current one, so
      a segment survives from a location the posting no longer lists.

    A posting with no location rows at all is **not** stale: it has nothing to
    project, and the evaluator answers UNKNOWN about it, which is correct.
    """
    for source in sources:
        stored = stored_signature(connection, source.source_location_id)
        if stored is None:
            return (
                f"location row {source.source_location_id} has no coherent stored "
                f"resolution"
            )
        expected = current_location_signature(source.location_text)
        if stored != expected:
            return (
                f"location row {source.source_location_id} is projected under "
                f"{stored[1]} for another string or version"
            )
    projected = {item.source_location_id for item in resolutions}
    current = {source.source_location_id for source in sources}
    if projected != current:
        return (
            f"the projection of opportunity {opportunity_id} covers "
            f"{sorted(projected)} and its current location rows are {sorted(current)}"
        )
    return None


def _current_eligibility_signature(
    connection: sqlite3.Connection,
    profile_id: int,
    opportunity_id: int,
    profile_input,
) -> tuple[str, str] | None:
    """The `(engine_version, fingerprint)` a decision must carry to be current.

    Phase 3.6's own digest over Phase 3.6's own current inputs. **No verdict is
    computed**: `evaluate_eligibility` is not called and could not be, because
    the answer is not the question here — the question is whether the stored
    answer was given about what the database says today.

    `None` means the current inputs can no longer be assembled at all, which is
    reported separately: an unanswerable question is not a matching answer.
    """
    opportunity_input = load_opportunity_input(connection, opportunity_id)
    if opportunity_input is None:
        return None
    current = EligibilityInput(opportunity_input, profile_input)
    return current.engine_version, eligibility_fingerprint(current)


def _qualification_is_current(
    connection: sqlite3.Connection, opportunity_id: int
) -> str | None:
    """Return why the Phase 8 row is stale, or None when it is current.

    Phase 8's persistence defines what "current" means, in one line of
    `persist_qualifications`: a row is left alone exactly when its stored
    `(input_fingerprint, classifier_version, fine_classifier_version)` equals the
    fingerprint of the posting's fields today plus the two versions running now.
    That triple is restated here by **calling Phase 8's own**
    `input_fingerprint` and importing its two version constants, so this check
    cannot drift from the reconciliation it mirrors.

    Nothing is classified and nothing is written: `persist_qualifications` is not
    called, and a stale row is reported rather than refreshed.
    """
    row = connection.execute(
        """SELECT canonical_title, description, source_url, application_url,
                  canonical_url FROM opportunities WHERE id = ?""",
        (opportunity_id,),
    ).fetchone()
    if row is None:
        return "the opportunity no longer exists"
    expected = (input_fingerprint(*row), CLASSIFIER_VERSION, FINE_CLASSIFIER_VERSION)
    stored = connection.execute(
        """SELECT input_fingerprint, classifier_version, fine_classifier_version
             FROM opportunity_qualifications WHERE opportunity_id = ?""",
        (opportunity_id,),
    ).fetchone()
    if stored is None:
        return "it has no qualification row"
    stored = tuple(stored)
    if stored == expected:
        return None
    if stored[1] != CLASSIFIER_VERSION:
        return (
            f"it was classified by {stored[1]!r}, and {CLASSIFIER_VERSION!r} is "
            f"running"
        )
    if stored[2] != FINE_CLASSIFIER_VERSION:
        # NULL is the documented legacy state, and it is still a valid *public*
        # read — see `fine_read_model`. It is not a current projection, which is
        # a stricter question and the only one asked here.
        return (
            "its fine classification is not recorded"
            if stored[2] is None
            else f"it was fine-classified by {stored[2]!r}, and "
            f"{FINE_CLASSIFIER_VERSION!r} is running"
        )
    return "it was classified from other opportunity fields"


def current_semantic_corpus_fingerprint(
    opportunities: Sequence[MatchingOpportunityInput],
) -> str:
    """The corpus fingerprint the current postings would produce, read-only.

    `fit_tfidf_corpus` computes exactly this, with exactly this function, before
    it fits anything — so comparing it to the persisted `run.corpus_fingerprint`
    proves the stored percentiles were ranked against the documents the postings
    produce **today**, without fitting a vectorizer, touching sklearn, or scoring
    a single similarity.

    The payload is an order-insensitive multiset of document hashes carrying no
    opportunity id, which is the point: an unchanged cohort whose titles or
    descriptions moved produces a different fingerprint, and that is the drift
    the cohort-id check cannot see.
    """
    return semantic_corpus_fingerprint(
        tuple(build_opportunity_semantic_document(item) for item in opportunities)
    )


def _binding_is_current(run, opportunities) -> str | None:
    """Return why the semantic binding is unusable, or None when it is current.

    Three ways it fails, and a legacy run is one of them. A Matching run
    persisted before this provenance existed is perfectly valid history —
    `read_current_matching` returns it and Phase 4's own audit passes it — but
    it carries no evidence that its percentiles still belong to these postings,
    and Recommendation does not guess. The operator runs `sync_matching`;
    nothing here upgrades or repairs the old run.
    """
    try:
        version = run.semantic_binding_version
        stored = run.semantic_binding_fingerprint
    except MatchingReadError as error:
        return f"the persisted semantic binding provenance is unreadable: {error}"
    if version is None or stored is None:
        return (
            "the current matching run predates semantic binding provenance, so "
            "nothing proves its percentiles still belong to these postings"
        )
    if version != SEMANTIC_BINDING_VERSION:
        return (
            f"the current matching run carries semantic binding provenance "
            f"{version!r}, and {SEMANTIC_BINDING_VERSION!r} is running"
        )
    if current_semantic_binding_fingerprint(opportunities) != stored:
        return (
            "the current postings no longer carry the semantic documents the "
            "persisted percentiles were ranked for"
        )
    return None


def current_semantic_binding_fingerprint(
    opportunities: Sequence[MatchingOpportunityInput],
) -> str:
    """Which document each current posting produces, read-only.

    The complement of the corpus fingerprint above and the reason both exist.
    The corpus digest is an unordered multiset of texts, so two postings that
    exchange their titles and descriptions leave it untouched while every stored
    percentile ends up describing the other posting. This digest is ordered by
    `opportunity_id` and moves the moment that assignment does.

    Computed with Matching's own `semantic_binding_fingerprint` over Matching's
    own documents: no second hash format, no second normalization, and still no
    vectorizer anywhere near it.
    """
    return semantic_binding_fingerprint(
        tuple(build_opportunity_semantic_document(item) for item in opportunities)
    )


def assemble_recommendation_inputs(
    connection: sqlite3.Connection, profile_id: int
) -> RecommendationInputAssemblyResult:
    """Read and validate the complete current Matching cohort without scoring it.

    Read-only from beginning to end: every statement below is a `SELECT`, and
    every upstream is consulted through the package that owns it.
    """
    owner = connection.execute(
        "SELECT user_id FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if owner is None:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.PROFILE_NOT_FOUND,
                    f"profile {profile_id} was not found",
                )
            ],
        )
    user_id = owner[0]
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.PROFILE_OWNER_INVALID,
                    f"profile {profile_id} has an invalid owner",
                )
            ],
        )
    try:
        matching = read_current_matching(connection, profile_id)
    except MatchingReadError as error:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_NOT_READY,
                    f"matching snapshot is not readable: {error}",
                )
            ],
            user_id,
        )
    run = matching.current_run
    if matching.status != "READY" or run is None:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_NOT_READY,
                    f"matching status is {matching.status}; synchronize Matching first",
                )
            ],
            user_id,
        )
    expected_versions = (
        ("persistence_version", run.persistence_version, MATCHING_PERSISTENCE_VERSION),
        ("selection_version", run.selection_version, MATCHING_SELECTION_VERSION),
        (
            "matching_engine_version",
            run.matching_engine_version,
            MATCHING_ENGINE_VERSION,
        ),
        ("matching_rules_version", run.matching_rules_version, MATCHING_RULES_VERSION),
        (
            "semantic_percentile_version",
            run.semantic_percentile_version,
            SEMANTIC_PERCENTILE_VERSION,
        ),
    )
    stale = [name for name, actual, expected in expected_versions if actual != expected]
    if stale:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_VERSION_STALE,
                    "matching run has unsupported versions: " + ", ".join(stale),
                )
            ],
            user_id,
            run.run_id,
        )
    # Phase 4 owns what a coherent persisted run is, so its own audit answers.
    # Only the run the profile currently points at is judged: an older run that
    # rotted is a real problem, and not this cohort's.
    try:
        audit = audit_matching_profile_history(connection, profile_id)
    except MatchingPersistenceAuditError as error:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID,
                    f"matching persistence cannot be audited: {error}",
                )
            ],
            user_id,
            run.run_id,
        )
    current = next(
        (item for item in audit.runs if item.run_id == run.run_id), None
    )
    profile_level = [item for item in audit.issues if item.run_id is None]
    if (
        audit.current_run_id != run.run_id
        or current is None
        or not current.ok
        or profile_level
    ):
        detail = (
            "current matching run is missing from the audit"
            if current is None
            else ", ".join(
                sorted({item.code for item in (*current.issues, *profile_level)})
            )
            or "profile state does not reference the current run"
        )
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.MATCHING_PERSISTENCE_INVALID,
                    f"current matching run {run.run_id} failed its persistence "
                    f"audit: {detail}",
                )
            ],
            user_id,
            run.run_id,
        )
    persisted_ids = tuple(item.opportunity_id for item in run.assessments)
    if persisted_ids != select_matching_opportunity_ids(connection):
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_COHORT,
                    "matching cohort is stale; synchronize Matching first",
                )
            ],
            user_id,
            run.run_id,
        )
    try:
        profile_input = load_profile_matching_input(connection, profile_id)
    except MatchingInputError as error:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"profile projections are not readable: {error}",
                )
            ],
            user_id,
            run.run_id,
        )
    profile_target = resolve_profile_target(connection, profile_id)
    # The free-text constraints live on the preferences row and are not part of
    # `MatchingPreferences`, so they are read from the Digital Twin's own
    # repository. They are carried verbatim and never parsed.
    declared_constraints: tuple[str, ...] = ()
    preference_row = get_profile_preferences(connection, profile_id)
    if preference_row is not None:
        value = preference_row.value
        if not isinstance(value, OpportunityPreferences):
            return _incomplete(
                profile_id,
                [
                    _issue(
                        RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                        "profile preferences projection has an invalid value",
                    )
                ],
                user_id,
                run.run_id,
            )
        declared_constraints = tuple(value.constraints)
    # One read for the whole profile side of every eligibility digest below.
    try:
        eligibility_profile = load_profile_input(connection, profile_id)
    except EligibilityInputError as error:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.ELIGIBILITY_INPUT_INCOMPLETE,
                    f"eligibility profile inputs are not readable: {error}",
                )
            ],
            user_id,
            run.run_id,
        )
    # One read for the whole corpus, indexed by posting: the projection is
    # judged against the location rows that exist right now.
    sources_by_opportunity: dict[int, list[LocationSource]] = {}
    for source in load_location_sources(connection):
        sources_by_opportunity.setdefault(source.opportunity_id, []).append(source)

    # Two cohort-wide preflights, in this order, before a single assessment is
    # assembled. Both are all-or-nothing: the second reads the whole corpus, and
    # the first decides whether that corpus may be believed at all.
    #
    # The opportunity inputs are loaded once here and reused by the loop below,
    # so proving the corpus costs no extra read.
    opportunity_inputs: dict[int, MatchingOpportunityInput] = {}
    qualification_issues: list[RecommendationReadinessIssue] = []
    for opportunity_id in persisted_ids:
        stale_qualification = _qualification_is_current(connection, opportunity_id)
        if stale_qualification is not None:
            qualification_issues.append(
                _issue(
                    RecommendationReadinessIssueCode.QUALIFICATION_PROJECTION_STALE,
                    f"opportunity {opportunity_id} has a stale Phase 8 "
                    f"classification: {stale_qualification}; synchronize "
                    f"Qualification first",
                    opportunity_id,
                )
            )
            continue
        try:
            opportunity_inputs[opportunity_id] = load_opportunity_matching_input(
                connection, opportunity_id
            )
        except MatchingInputError as error:
            qualification_issues.append(
                _issue(
                    RecommendationReadinessIssueCode.OPPORTUNITY_MISSING,
                    f"opportunity {opportunity_id} was not found: {error}",
                    opportunity_id,
                )
            )
    if qualification_issues:
        return _incomplete(profile_id, qualification_issues, user_id, run.run_id)

    # The cohort's ids can be identical while a posting's title or description
    # has moved, and the persisted percentiles were then ranked against a corpus
    # nobody would produce now. This compares the fingerprint Matching itself
    # computes before fitting; nothing is fitted, scored or refreshed here.
    # A READY run always carries at least one assessment — `build_matching_assessments`
    # refuses an empty corpus — so the guard is for a database that contradicts
    # that: it answers with a readiness issue rather than a bare ValueError.
    documents = tuple(opportunity_inputs.values())
    if not documents or (
        current_semantic_corpus_fingerprint(documents) != run.corpus_fingerprint
    ):
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_SEMANTIC_CORPUS,
                    "the current postings no longer produce the semantic corpus "
                    "the persisted percentiles were ranked against; synchronize "
                    "Matching first",
                )
            ],
            user_id,
            run.run_id,
        )

    # The corpus content matching is not enough. The corpus digest is an
    # unordered multiset by design, so two postings that exchanged their titles
    # and descriptions pass the check above while every persisted percentile now
    # describes the other posting. The binding provenance is what sees that, and
    # it is checked second so an ordinary text edit still reports the corpus
    # code rather than this one.
    stale_binding = _binding_is_current(run, documents)
    if stale_binding is not None:
        return _incomplete(
            profile_id,
            [
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_SEMANTIC_BINDING,
                    f"{stale_binding}; synchronize Matching first",
                )
            ],
            user_id,
            run.run_id,
        )

    issues: list[RecommendationReadinessIssue] = []
    records: list[RecommendationInputRecord] = []
    for assessment in run.assessments:
        opportunity_id = assessment.opportunity_id
        row = connection.execute(
            """SELECT canonical_title, organization, source_url, application_url,
                      canonical_url FROM opportunities WHERE id = ?""",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.OPPORTUNITY_MISSING,
                    f"opportunity {opportunity_id} was not found",
                    opportunity_id,
                )
            )
            continue
        try:
            snapshot = _snapshot(profile_id, assessment, run)
        except (ValueError, TypeError) as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.INVALID_MATCHING_PAYLOAD,
                    f"opportunity {opportunity_id} has an unreadable matching "
                    f"assessment: {error}",
                    opportunity_id,
                )
            )
            continue
        try:
            opportunity_input = opportunity_inputs[opportunity_id]
            matching_input = MatchingInput(profile_input, opportunity_input)
            structured = build_role_domain_preference_signals(matching_input)
            # Recomputed for its per-skill detail only. The ratio that reaches
            # the score is always the persisted one; this fit has to prove it
            # describes that same ratio, which the fingerprint below does.
            skill_fit = build_skill_fit(
                matching_input, build_opportunity_skill_signals(opportunity_input)
            )
        except (
            MatchingInputError,
            RoleDomainPreferencesInputError,
            SkillFitInputError,
        ) as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has unreadable structured "
                    f"signals: {error}",
                    opportunity_id,
                )
            )
            continue
        # The recomputed alignment is only usable while it still describes the
        # snapshot it will be mixed with. A disagreement means the persisted
        # matching predates a change to the qualification, the preferences or
        # the posting, and Phase 9 repairs nothing.
        if (
            role_domain_preferences_fingerprint(structured)
            != snapshot.structured_upstream_fingerprint
        ):
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_SNAPSHOT,
                    f"opportunity {opportunity_id} has structured signals that "
                    f"no longer match its persisted assessment; synchronize "
                    f"Matching first",
                    opportunity_id,
                )
            )
            continue
        if (
            skill_fit_fingerprint(skill_fit)
            != snapshot.required_skill_upstream_fingerprint
        ):
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.STALE_MATCHING_SKILL_FIT,
                    f"opportunity {opportunity_id} has a skill fit that no longer "
                    f"matches its persisted required-skill component; synchronize "
                    f"Matching first",
                    opportunity_id,
                )
            )
            continue
        try:
            fine = _fine_classification(connection, opportunity_id)
        except FineClassificationDecodeError as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.FINE_CLASSIFICATION_INVALID,
                    f"opportunity {opportunity_id} has an unreadable fine "
                    f"classification: {error}",
                    opportunity_id,
                )
            )
            continue
        resolutions = read_opportunity_resolutions(connection, opportunity_id)
        stale_geography = _geography_is_current(
            connection,
            opportunity_id,
            sources_by_opportunity.get(opportunity_id, ()),
            resolutions,
        )
        if stale_geography is not None:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.GEOGRAPHY_PROJECTION_STALE,
                    f"opportunity {opportunity_id} has a stale geographic "
                    f"projection: {stale_geography}; synchronize Geography first",
                    opportunity_id,
                )
            )
            continue
        try:
            stored = read_eligibility(connection, user_id, opportunity_id)
        except (ValueError, TypeError) as error:
            issues.append(
                _issue(
                    RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                    f"opportunity {opportunity_id} has an invalid eligibility "
                    f"decision: {error}",
                    opportunity_id,
                )
            )
            continue
        rule_results = ()
        if stored is not None:
            if not _is_sha256(stored.input_fingerprint) or not stored.engine_version:
                issues.append(
                    _issue(
                        RecommendationReadinessIssueCode.INVALID_UPSTREAM_VALUE,
                        f"opportunity {opportunity_id} has invalid eligibility "
                        f"provenance",
                        opportunity_id,
                    )
                )
                continue
            try:
                signature = _current_eligibility_signature(
                    connection, profile_id, opportunity_id, eligibility_profile
                )
            except EligibilityInputError as error:
                signature = None
                message = str(error)
            else:
                message = "the posting's Phase 3.5 requirement reading is gone"
            if signature is None:
                # A decision exists and its question can no longer be asked. The
                # old verdict is not evidence about inputs nobody can read.
                issues.append(
                    _issue(
                        RecommendationReadinessIssueCode.ELIGIBILITY_INPUT_INCOMPLETE,
                        f"opportunity {opportunity_id} has a stored eligibility "
                        f"decision whose current inputs cannot be assembled: "
                        f"{message}",
                        opportunity_id,
                    )
                )
                continue
            if (stored.engine_version, stored.input_fingerprint) != signature:
                # The decision was taken about other inputs. Publishing it now —
                # an INELIGIBLE above all — would be a blocker nobody decided.
                issues.append(
                    _issue(
                        RecommendationReadinessIssueCode.ELIGIBILITY_SNAPSHOT_STALE,
                        f"opportunity {opportunity_id} has an eligibility decision "
                        f"that no longer describes its current inputs; "
                        f"synchronize Eligibility first",
                        opportunity_id,
                    )
                )
                continue
            rule_results = read_rule_results(connection, stored.id)
        records.append(
            RecommendationInputRecord(
                RecommendationInput(
                    matching=snapshot,
                    preferences=profile_input.preferences,
                    structured=structured,
                    skill_fit=skill_fit,
                    fine=fine,
                    geography=RecommendationGeographyInput(
                        profile_target=profile_target,
                        resolutions=resolutions,
                    ),
                    eligibility=RecommendationEligibilityInput(
                        status=None if stored is None else stored.status,
                        input_fingerprint=(
                            None if stored is None else stored.input_fingerprint
                        ),
                        engine_version=(
                            None if stored is None else stored.engine_version
                        ),
                        results=rule_results,
                    ),
                    declared_constraints=declared_constraints,
                ),
                RecommendationOpportunityContext(opportunity_id, *row),
            )
        )
    if issues:
        return _incomplete(profile_id, issues, user_id, run.run_id)
    return RecommendationInputAssemblyResult(
        RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        RecommendationReadinessStatus.READY,
        profile_id,
        user_id,
        run.run_id,
        (),
        tuple(records),
    )
