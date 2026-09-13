"""The versioned vocabulary of a business / data-quality metric — Phase 10.4a.

This module is the *contract* and nothing else: which closed universe each
metric is defined over, which evidence each one is allowed to rest on, which
refusals each one may state, and the validation that decides whether such an
artefact is well formed. It opens no file, opens no database connection, makes
no HTTP request and computes no digest. **It computes no metric either** — there
is no formula in this package, and `BusinessMetricResult` holds a number
somebody else produced.

Phase 10.3 lives next door and answers a different question. `evaluation/metrics`
says how good the *ranking* is, against human judgements, and states in its own
docstring that it holds no business KPI. This package says how good the *data*
is. The two are siblings rather than layers, and they deliberately share no
abstraction: a validator duplicated in twenty lines is cheaper than a coupling
that lets the ranking contract and the business contract move each other.

Seven rules shape everything below.

* **The cohort is a bound artefact, not two strings.** `FrozenCohortBinding` is
  derived from the verified Phase 10.1 records and manifest and carries the
  *facts* every later check needs: how many records there are, when the snapshot
  was taken, the as-of date that follows from it, the complete set of frozen
  action URLs it implies, and what its records say about deduplication. No
  caller states a cohort size and no caller chooses an as-of date.
* **Every metric has exactly one universe, and its size is checked.**
  `BUSINESS_METRIC_DEFINITIONS` fixes the pairing; a result pairing a metric with
  another universe is a contract failure, and a result whose `universe_size` is
  not the real size of that universe is a binding failure. Neither is `N_A`.
* **Every metric declares which refusals it may state.** A closed enum of reason
  codes is not enough: `TARGET_COUNTRY_UNKNOWN` on a `DATA_AI_RATE` is as wrong
  as a free-form string, so each definition names its permitted reasons and the
  conditions under which each is the *only* correct one.
* **UNKNOWN is not FALSE.** A posting nobody could place stays in the
  denominator of `TARGET_COUNTRY_MATCH_RATE` and is counted as an UNKNOWN
  verdict. A URL that was refused, rate-limited or never reached is
  `INCONCLUSIVE`, never `BROKEN`.
* **N_A is not 0, and a contradiction is refused rather than reported.** An
  unavailable metric carries a reason and no number — not a zero, not a `-1`,
  not a NaN, and not a structured block it was never allowed to compute.
* **Identity is per metric, never per report.** A result's `scope_fingerprint`
  covers only what *this* metric asked; its `evidence_fingerprint` covers only
  the artefacts *this* metric rests on, and no output of any computation. The
  whole-run artefact has an identity of its own, deliberately not called a scope.
* **The payload bound is the payload fingerprinted.** Each object here has
  exactly one canonical payload function, and `fingerprint.py` digests that same
  function's output.

**No human label ever enters this package.** Nothing here imports
`evaluation.labeling`, and no scope, context or evidence field can hold a
labelset fingerprint, a grade, a judged count or a list of unjudged ids.

The dependency direction, unchanged since Phase 10.1:

    operational SQLite / production outputs
        -> frozen evaluation dataset (Phase 10.1)
            -> business / data-quality evidence (here)
                -> business metric results

and never the reverse. Nothing in `services/` imports this package.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from evaluation.dataset import (
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationDatasetError,
)

__all__ = [
    "ALWAYS_UNAVAILABLE_METRICS",
    "BENCHMARK_BINDING_VERSION",
    "BROKEN_STATUS_CODES",
    "BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION",
    "BUSINESS_METRIC_CONTRACT_VERSION",
    "BUSINESS_METRIC_DEFINITIONS",
    "BUSINESS_METRIC_RESULT_SCHEMA_VERSION",
    "BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION",
    "BUSINESS_METRIC_RUN_SCHEMA_VERSION",
    "BUSINESS_METRIC_SCOPE_SCHEMA_VERSION",
    "BenchmarkBinding",
    "BusinessEvidenceClass",
    "BusinessEvidenceClassError",
    "BusinessEvidenceMember",
    "BusinessMetricArgumentError",
    "BusinessMetricBindingError",
    "BusinessMetricContractError",
    "BusinessMetricDefinition",
    "BusinessMetricDimension",
    "BusinessMetricEvidenceView",
    "BusinessMetricKey",
    "BusinessMetricName",
    "BusinessMetricResult",
    "BusinessMetricRun",
    "BusinessMetricRunContext",
    "BusinessMetricRunProvenance",
    "BusinessMetricScope",
    "BusinessMetricStatus",
    "BusinessMetricSupport",
    "BusinessMetricUnavailableReason",
    "BusinessMetricUniverse",
    "BusinessMetricsError",
    "COMPUTED_ONLY_SUPPORT_BLOCKS",
    "DECLARED_SOURCE_ACTIVE_STATUS",
    "DECLARED_SOURCE_UNIVERSE_VERSION",
    "DEDUP_EVIDENCE_VERSION",
    "DIMENSIONAL_BUSINESS_METRICS",
    "DataAiQualificationBreakdown",
    "DeclaredSourceEntry",
    "DeclaredSourceUniverseEvidence",
    "DedupEvidence",
    "EVIDENCE_MEMBER_ATTRIBUTES",
    "EXPECTED_FRESHNESS_POLICY_VERSION",
    "FRESHNESS_AGE_BUCKETS",
    "FRESHNESS_BINDING_VERSION",
    "FRESHNESS_BUCKET_DAY_BOUNDS",
    "FRESHNESS_BUCKET_ORDER",
    "FRESHNESS_DISTRIBUTION_HOST_METRIC",
    "FROZEN_ACTION_URL_PRECEDENCE",
    "FROZEN_ACTION_URL_PROTOCOL_VERSION",
    "FROZEN_COHORT_BINDING_VERSION",
    "FreshnessAvailabilityWitness",
    "FreshnessBinding",
    "FreshnessBucket",
    "FreshnessBucketCount",
    "FreshnessBucketDistribution",
    "FrozenCohortBinding",
    "HTTP_URL_FORBIDDEN_REASONS",
    "INCONCLUSIVE_STATUS_REASONS",
    "ListingQualityBreakdown",
    "MEMBER_ABSENCE_REASONS",
    "MERGED_DUPLICATE_EXCLUSION_KEY",
    "NON_HTTP_SCHEME_URL_REASONS",
    "NO_RESPONSE_REASONS",
    "NO_SCHEME_URL_REASONS",
    "OpportunityTypeBreakdown",
    "PROFILE_TARGET_BINDING_VERSION",
    "ProfileTargetBindingEvidence",
    "REQUIRED_SUPPORT_BLOCKS",
    "SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS",
    "SUPPORTED_BUSINESS_METRIC_RUN_SCHEMA_VERSIONS",
    "SUPPORT_BLOCK_HOSTS",
    "TARGET_VERDICT_HOST_METRIC",
    "TargetVerdictBreakdown",
    "UNATTEMPTED_REASONS",
    "UNIVERSE_DEFINING_MEMBERS",
    "URL_AUDIT_BINDING_VERSION",
    "URL_AUDIT_MAX_REDIRECTS",
    "URL_AUDIT_METHOD",
    "URL_AUDIT_POLICY_VERSION",
    "URL_AUDIT_RETRIES",
    "URL_AUDIT_TIMEOUT_SECONDS",
    "URL_AUDIT_USER_AGENT",
    "UnknownLocationDiagnostics",
    "UrlAuditBinding",
    "UrlAuditInconclusiveReason",
    "UrlAuditObservation",
    "UrlAuditOutcome",
    "UrlShape",
    "benchmark_binding_payload",
    "business_metric_evidence_payload",
    "business_metric_key_payload",
    "business_metric_key_sort_key",
    "business_metric_result_payload",
    "business_metric_run_context_payload",
    "business_metric_run_fingerprint_payload",
    "business_metric_run_payload",
    "business_metric_scope_payload",
    "business_metric_support_payload",
    "calendar_date_of",
    "canonical_business_metric_keys",
    "data_ai_qualification_breakdown_payload",
    "declared_source_entry_payload",
    "declared_source_universe_payload",
    "dedup_evidence_payload",
    "evidence_member_of",
    "freshness_availability_witness_payload",
    "freshness_binding_payload",
    "freshness_bucket_distribution_payload",
    "frozen_action_url",
    "frozen_cohort_binding_payload",
    "is_http_url",
    "listing_quality_breakdown_payload",
    "member_fingerprint",
    "member_value",
    "metric_definition",
    "opportunity_type_breakdown_payload",
    "outcome_for_status",
    "profile_target_binding_payload",
    "require_metric_universe",
    "require_permitted_reason",
    "require_supported_business_metric_contract_version",
    "require_supported_business_metric_run_schema_version",
    "target_verdict_breakdown_payload",
    "unknown_location_diagnostics_payload",
    "url_audit_binding_payload",
    "url_audit_observation_payload",
    "url_shape",
    "validate_benchmark_binding_structure",
    "validate_business_metric_evidence_structure",
    "validate_business_metric_result_structure",
    "validate_business_metric_run_context_structure",
    "validate_business_metric_run_structure",
    "validate_business_metric_scope_structure",
    "validate_count",
    "validate_country_code",
    "validate_declared_source_universe_structure",
    "validate_dedup_evidence_structure",
    "validate_fingerprint",
    "validate_finite_number",
    "validate_freshness_binding_structure",
    "validate_frozen_cohort_binding_structure",
    "validate_git_commit",
    "validate_http_url",
    "validate_iso_date",
    "validate_optional_text",
    "validate_profile_id",
    "validate_profile_target_binding_structure",
    "validate_status_code",
    "validate_text",
    "validate_timestamp",
    "validate_url_audit_binding_structure",
]


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------

#: The *semantic* contract: what these metrics are defined to mean, which
#: universe each is stated over, which evidence each may rest on, which refusals
#: each may state, and what makes a number reportable at all. Deliberately
#: **not** `evaluation-metric-contract-v1`: that version governs Precision@K,
#: Recall@K and NDCG@K against human judgements, and a business KPI shares none
#: of its rules.
BUSINESS_METRIC_CONTRACT_VERSION = "business-data-quality-metric-contract-v1"

#: The contract versions this build can interpret. Exactly one, as in Phase
#: 10.3, and for the same reason.
SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS: tuple[str, ...] = (
    BUSINESS_METRIC_CONTRACT_VERSION,
)

#: The *structural* versions, separate from the semantic contract exactly as
#: Phase 10.3 separates `evaluation-run-v1` from its metric contract.
BUSINESS_METRIC_SCOPE_SCHEMA_VERSION = "business-metric-scope-v1"
BUSINESS_METRIC_RESULT_SCHEMA_VERSION = "business-metric-result-v1"
BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION = "business-metric-evidence-view-v1"
BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION = "business-metric-run-context-v1"

#: The shape of a whole persisted run — Phase 10.4b. Deliberately **not**
#: `BUSINESS_METRIC_CONTRACT_VERSION`: that version governs what the numbers
#: *mean*, and this one governs how a run of them is laid out. Widening the
#: document (a new provenance field, a new block) moves this and leaves the
#: semantics alone; changing what a rate is defined as moves the other and would
#: invalidate every stored run whatever its layout.
BUSINESS_METRIC_RUN_SCHEMA_VERSION = "business-metric-run-v1"

#: The run layouts this build can read. Exactly one, for the same reason the
#: contract has exactly one: a stored run under an unknown version is refused
#: rather than guessed at.
SUPPORTED_BUSINESS_METRIC_RUN_SCHEMA_VERSIONS: tuple[str, ...] = (
    BUSINESS_METRIC_RUN_SCHEMA_VERSION,
)

#: The evidence artefacts, each versioned on its own so that widening one does
#: not invalidate the others.
FROZEN_COHORT_BINDING_VERSION = "business-frozen-cohort-binding-v1"
PROFILE_TARGET_BINDING_VERSION = "business-profile-target-binding-v1"
DECLARED_SOURCE_UNIVERSE_VERSION = "business-declared-source-universe-v1"
URL_AUDIT_BINDING_VERSION = "business-url-audit-binding-v1"
DEDUP_EVIDENCE_VERSION = "business-dedup-evidence-v1"
FRESHNESS_BINDING_VERSION = "business-freshness-binding-v1"
BENCHMARK_BINDING_VERSION = "business-benchmark-binding-v1"

#: How a future audit decides *which* URL of a posting it is auditing. Frozen
#: here, in the contract, rather than in the audit that will use it.
FROZEN_ACTION_URL_PROTOCOL_VERSION = "frozen-action-url-v1"

#: That precedence, stated once and applied by `frozen_action_url` below.
FROZEN_ACTION_URL_PRECEDENCE: tuple[str, ...] = (
    "application_url",
    "source_url",
    "canonical_url",
)

# --------------------------------------------------------------------------
# the frozen URL audit policy
# --------------------------------------------------------------------------
#
# **Phase 10.4a encodes this policy and performs none of it.** There is no HTTP
# client in this package; what lives here is the identity of the protocol a
# later slice must implement, so that an audit artefact says which rules
# produced it and two artefacts made under different rules can never be compared
# by accident.

#: The audit policy `UrlAuditBinding.audit_policy_version` must state, exactly.
#: A free string was accepted before this version, which meant an artefact could
#: name a policy nobody had written down — and a `BROKEN_URL_RATE` is only
#: comparable across two audits if both followed the same redirect bound, the
#: same timeout and the same robots discipline.
URL_AUDIT_POLICY_VERSION = "business-url-audit-policy-v1"

#: The frozen parameters of that policy, stated as constants so the later slice
#: has nothing to choose and this contract has something to be checked against.
URL_AUDIT_METHOD = "GET"
URL_AUDIT_TIMEOUT_SECONDS = 20
URL_AUDIT_RETRIES = 0
URL_AUDIT_MAX_REDIRECTS = 3
URL_AUDIT_USER_AGENT = "OpportunityRadarAI-UrlAudit/1.0 (+evaluation; GET-only)"

#: The manifest exclusion key whose count the dedup evidence must agree with.
MERGED_DUPLICATE_EXCLUSION_KEY = "merged_duplicate"

#: The `integration_status` a declared source entry must carry to be counted in
#: the numerator of `ACTIVE_DECLARED_SOURCE_RATE`.
#:
#: Written as a literal for the same reason as the freshness policy below: this
#: module imports no other package. The *agreement* with the source map
#: validator's own `SourceMap.active_sources` — which selects on exactly this
#: status — is asserted by the unit tests, which may import it freely. "Active"
#: is that file's word and not this contract's invention: an entry only earns
#: the status when a strategy that already runs names a real row of
#: `config/sources.yaml`, so the rate is a coverage claim about collectors that
#: exist rather than about entries somebody intends to write.
DECLARED_SOURCE_ACTIVE_STATUS = "ACTIVE"

#: The production freshness policy a freshness metric is defined against.
#: Written as a literal so that this module imports no production package and
#: stays free of anything that could open a connection; the *agreement* with
#: `services.priority.models.PRIORITY_FRESHNESS_VERSION` is asserted by the unit
#: tests, which may import production freely.
EXPECTED_FRESHNESS_POLICY_VERSION = "priority-freshness-v1"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class BusinessMetricsError(EvaluationDatasetError):
    """Raised when a business metric artefact cannot be built or read safely.

    A subclass of the Phase 10.1 error, as Phase 10.2's and Phase 10.3's are.
    Everything this package raises is one of the four subclasses below; no
    low-level `KeyError`, `ValueError`, `TypeError` or `AttributeError` escapes a
    public function.
    """


class BusinessMetricContractError(BusinessMetricsError):
    """The artefact is not something this contract can express.

    An unsupported version, a metric paired with a universe it is not defined
    over, a reason code belonging to another metric, a status/value combination
    the contract forbids, an audit observation whose verdict contradicts its own
    status code.
    """


class BusinessMetricBindingError(BusinessMetricsError):
    """Two artefacts do not agree about what is being measured.

    A dataset fingerprint that moved, a profile fingerprint that no longer
    matches the frozen one, a universe size that is not the size of that
    universe, a scope bound to source map A beside evidence from source map B,
    an audit artefact that omits an eligible URL, a dedup count that contradicts
    the manifest, a self-declared fingerprint that does not survive
    recomputation. **Never an `N_A`.**
    """


class BusinessEvidenceClassError(BusinessMetricsError):
    """The evidence provided cannot support the class the metric claims."""


class BusinessMetricArgumentError(BusinessMetricsError):
    """A caller asked a malformed question — a NaN value, a negative count."""


def require_supported_business_metric_contract_version(value: Any) -> str:
    """Refuse an artefact recorded under business rules this build lacks."""
    if value not in SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS:
        raise BusinessMetricContractError(
            f"unsupported business metric contract version: {value!r} (this "
            f"build implements "
            f"{list(SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS)})"
        )
    return str(value)


def require_supported_business_metric_run_schema_version(value: Any) -> str:
    """Refuse a run laid out in a shape this build cannot read.

    Separate from the contract version above because the two fail for different
    reasons and a reader needs to know which: an unsupported *run schema* means
    this build cannot parse the document, and an unsupported *contract* means it
    could parse it and must not believe what it says.
    """
    if value not in SUPPORTED_BUSINESS_METRIC_RUN_SCHEMA_VERSIONS:
        raise BusinessMetricContractError(
            f"unsupported business metric run schema version: {value!r} (this "
            f"build reads {list(SUPPORTED_BUSINESS_METRIC_RUN_SCHEMA_VERSIONS)})"
        )
    return str(value)


# --------------------------------------------------------------------------
# primitive validation
# --------------------------------------------------------------------------

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
#: RFC 3339 with a mandatory offset — `Z` or `±HH:MM`. A local timestamp with no
#: offset is refused: an audit performed "at 14:03" is not a reproducible fact.
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d{1,9})?([Zz]|[+-]\d{2}:\d{2})$"
)


def validate_fingerprint(value: Any, *, subject: str) -> str:
    """A SHA-256 digest as every other Phase 10 artefact spells one."""
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.match(value):
        raise BusinessMetricBindingError(
            f"{subject} is not a SHA-256 fingerprint: {value!r}"
        )
    return value


def validate_text(value: Any, *, subject: str) -> str:
    """A non-empty, unpadded string. Padding is a difference nobody sees."""
    if not isinstance(value, str) or not value.strip():
        raise BusinessMetricBindingError(
            f"{subject} must be a non-empty string, not {value!r} "
            f"({type(value).__name__})"
        )
    if value != value.strip():
        raise BusinessMetricBindingError(
            f"{subject} must not be padded with whitespace: {value!r}"
        )
    return value


def validate_optional_text(value: Any, *, subject: str) -> str | None:
    """`None` means *not stated* and stays `None`; anything else is text."""
    if value is None:
        return None
    return validate_text(value, subject=subject)


def validate_count(value: Any, *, subject: str, minimum: int = 0) -> int:
    """A count: an integer at least `minimum`, and `bool` is not one.

    `bool` is refused first because `isinstance(True, int)` is true in Python, so
    a `True` arriving through a JSON round trip or a careless caller would
    otherwise be read as the count 1 — a denominator of one, confirmed by nothing
    having been counted.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessMetricBindingError(
            f"{subject} must be an integer, not {value!r} "
            f"({type(value).__name__})"
        )
    if value < minimum:
        raise BusinessMetricBindingError(
            f"{subject} must be at least {minimum}, not {value!r}"
        )
    return value


def validate_profile_id(value: Any, *, subject: str) -> int:
    """A positive integer profile id, with `bool` refused first."""
    return validate_count(value, subject=subject, minimum=1)


def validate_finite_number(value: Any, *, subject: str) -> float:
    """A real, finite number, as a float. NaN, ±inf and `bool` are refused.

    NaN is the single most dangerous value this package could carry: it is not
    equal to itself, it survives every arithmetic operation it touches, and it
    formats as a word a reader mistakes for a measurement. Both it and `inf` are
    the shape "no answer" takes when nobody wrote a reason code, which is exactly
    what `BusinessMetricStatus.N_A` exists to make impossible.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessMetricArgumentError(
            f"{subject} must be a real number, not {value!r} "
            f"({type(value).__name__})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise BusinessMetricArgumentError(
            f"{subject} must be finite, not {value!r}; a metric that cannot be "
            f"computed is {BusinessMetricStatus.N_A} with a reason code, never a "
            "NaN and never an infinity"
        )
    return number


def validate_iso_date(value: Any, *, subject: str) -> str:
    """A calendar date as `YYYY-MM-DD`, checked against the calendar.

    No clock is consulted, here or anywhere in this package: every date is
    *derived* from a frozen artefact, and a metric whose value moved with the
    wall clock would be unreproducible by construction.
    """
    if not isinstance(value, str) or not _ISO_DATE_PATTERN.match(value):
        raise BusinessMetricBindingError(
            f"{subject} is not an ISO calendar date (YYYY-MM-DD): {value!r}"
        )
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise BusinessMetricBindingError(
            f"{subject} is not a real calendar date: {value!r}"
        ) from error
    return value


def validate_timestamp(value: Any, *, subject: str) -> str:
    """An RFC 3339 instant with an explicit offset, checked against the calendar.

    Required of the dataset's `generated_at`, of an audit campaign, and of every
    attempted observation. A timestamp with no offset does not identify a moment
    — it identifies a moment per timezone.
    """
    if not isinstance(value, str) or not _TIMESTAMP_PATTERN.match(value):
        raise BusinessMetricBindingError(
            f"{subject} is not an RFC 3339 timestamp with an explicit offset "
            f"(e.g. 2026-03-01T09:00:00Z): {value!r}"
        )
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise BusinessMetricBindingError(
            f"{subject} is not a real instant: {value!r}"
        ) from error
    if parsed.tzinfo is None:
        raise BusinessMetricBindingError(
            f"{subject} states no UTC offset: {value!r}"
        )
    return value


def calendar_date_of(timestamp: str, *, subject: str) -> str:
    """The calendar date an RFC 3339 instant falls on, as `YYYY-MM-DD`.

    The one derivation behind `freshness_as_of_date`. It exists so that no
    caller ever *chooses* that date: it follows from the snapshot's own
    `generated_at` and from nothing else.
    """
    validated = validate_timestamp(timestamp, subject=subject)
    return validated[:10]


def validate_git_commit(value: Any, *, subject: str) -> str:
    """A full 40-character Git object name, lowercase hexadecimal.

    Required of a declared source map because that artefact is an authored file
    people edit in place: `version: v1` does not identify a revision, and an
    abbreviated or absent commit would leave the binding naming a file rather
    than a state of it. A short SHA is refused too — it is ambiguous by design.
    """
    if not isinstance(value, str) or not _GIT_COMMIT_PATTERN.match(value):
        raise BusinessMetricBindingError(
            f"{subject} is not a full 40-character Git commit id: {value!r}"
        )
    return value


def validate_country_code(value: Any, *, subject: str) -> str:
    """An ISO-3166-1 alpha-2 code, uppercase. No country is named here.

    Deliberately a *shape* check. This package holds no country of its own:
    Morocco is a property of one profile and of one declared source map, and
    hardcoding it in the generic contract would make every later metric a
    Morocco metric whether or not the profile said so.
    """
    if not isinstance(value, str) or not _COUNTRY_CODE_PATTERN.match(value):
        raise BusinessMetricBindingError(
            f"{subject} is not an ISO-3166-1 alpha-2 country code: {value!r}"
        )
    return value


class UrlShape(StrEnum):
    """What kind of string a selected action URL is. A *classification*.

    Never a refusal: the frozen protocol selects whatever field a posting
    carried, and this says what was selected so the audit contract can require
    the honest reason for it.
    """

    #: An absolute http(s) URL with a host. Requestable, subject to preflight.
    HTTP = "HTTP"

    #: An absolute URL in a scheme this audit does not speak — `mailto:`, `ftp:`.
    NON_HTTP_SCHEME = "NON_HTTP_SCHEME"

    #: Not an absolute URL at all — a relative path, a fragment, a bare word.
    NO_SCHEME = "NO_SCHEME"


def url_shape(value: Any) -> UrlShape:
    """Classify a selected action URL. Parsed with the standard library.

    Nothing here opens a socket. The distinction between the last two members is
    what lets the contract require `UNSUPPORTED_SCHEME` for a `mailto:` and
    `INVALID_URL` for a `/careers/3`, rather than letting an audit call either
    of them whatever it liked.
    """
    text = validate_text(value, subject="a selected action URL")
    try:
        parts = urlsplit(text)
    except ValueError:
        return UrlShape.NO_SCHEME
    if not parts.scheme:
        return UrlShape.NO_SCHEME
    if parts.scheme in ("http", "https") and parts.netloc:
        return UrlShape.HTTP
    if parts.scheme in ("http", "https"):
        # A scheme with no host: `http:///x`. Not requestable, and not a
        # scheme problem either.
        return UrlShape.NO_SCHEME
    return UrlShape.NON_HTTP_SCHEME


def is_http_url(value: Any) -> bool:
    """Is this an absolute http(s) URL? A **question**, never a refusal.

    Separate from `validate_http_url` on purpose, and the separation is the whole
    of Phase 10.4a's answer to "selection is not validation": the frozen action
    URL protocol *selects* a field, and whether the selected value is fetchable
    is a different question that belongs to the future audit.
    """
    try:
        return url_shape(value) is UrlShape.HTTP
    except BusinessMetricsError:
        return False


def validate_http_url(value: Any, *, subject: str) -> str:
    """An absolute http(s) URL, or a refusal. Parsed, never fetched.

    Applied where a URL is the *record of a response* — a final URL, a redirect
    hop — because those came from a real HTTP exchange and could not have been
    anything else. It is deliberately **not** applied to the requested URL of an
    observation: see `is_http_url` and `frozen_action_url`.
    """
    url = validate_text(value, subject=subject)
    if url_shape(url) is not UrlShape.HTTP:
        raise BusinessMetricBindingError(
            f"{subject} is not an absolute http(s) URL: {value!r}"
        )
    return url


def validate_status_code(value: Any, *, subject: str) -> int:
    """An HTTP status code, in the range the protocol defines."""
    code = validate_count(value, subject=subject, minimum=100)
    if code > 599:
        raise BusinessMetricBindingError(
            f"{subject} is not an HTTP status code: {value!r}"
        )
    return code


# --------------------------------------------------------------------------
# the closed universes
# --------------------------------------------------------------------------


class BusinessMetricUniverse(StrEnum):
    """Which closed set a business number is a statement about.

    **There is no default member**, no metric may choose its universe, and no
    result may misstate that universe's *size*: `BUSINESS_METRIC_DEFINITIONS`
    fixes the pairing and the builder holds `support.universe_size` against the
    artefact that defines the set.
    """

    #: Every record in the frozen Phase 10.1 cohort, exactly as the snapshot
    #: holds them. Its size is `FrozenCohortBinding.cohort_size`.
    FROZEN_COHORT = "FROZEN_COHORT"

    #: The cohort plus the duplicates its records absorbed — the postings the
    #: collector actually saw before deduplication merged them away. Its size is
    #: `DedupEvidence.represented_universe_size`.
    PRE_DEDUP_REPRESENTED_UNIVERSE = "PRE_DEDUP_REPRESENTED_UNIVERSE"

    #: The complete set of unique frozen action URLs derived from the cohort and
    #: represented in a sealed audit artefact. "Complete" is enforced, not
    #: hoped: the audit is held against `FrozenCohortBinding.expected_action_urls`
    #: every time a result over this universe is sealed or verified.
    URL_AUDITED_UNIVERSE = "URL_AUDITED_UNIVERSE"

    #: The declared, versioned source map — the explicit denominator of every
    #: coverage claim about sources. Its size is the number of its entries.
    DECLARED_SOURCE_UNIVERSE = "DECLARED_SOURCE_UNIVERSE"


# --------------------------------------------------------------------------
# status and reason codes
# --------------------------------------------------------------------------


class BusinessMetricStatus(StrEnum):
    """What a business metric result is: a number, or a stated refusal."""

    COMPUTED = "COMPUTED"
    N_A = "N_A"


class BusinessMetricUnavailableReason(StrEnum):
    """Why a metric is `N_A`, as a closed vocabulary.

    Closed is necessary and not sufficient. Each metric additionally declares
    which of these it may state (`BusinessMetricDefinition.permitted_reasons`)
    and the builder checks the *condition* each names, so a `DATA_AI_RATE` can
    no more be `N_A / URL_AUDIT_EVIDENCE_MISSING` than it could carry a
    free-form string.

    Every member names **coherent evidence that is absent or insufficient**. A
    fingerprint mismatch, a wrong universe size, a metric over the wrong
    universe, a scope and an evidence view naming different artefacts, an
    incomplete sealed audit, an inconsistent dedup count: none of those is here,
    because none of them means "we cannot answer this question" — they mean "the
    artefacts in front of us are not describing the same world".
    """

    #: No URL audit artefact is bound to this result at all.
    URL_AUDIT_EVIDENCE_MISSING = "URL_AUDIT_EVIDENCE_MISSING"

    #: An audit is bound and complete, and not one observation in it is
    #: conclusive. A broken-URL rate of 0.0 here would report "nothing is broken"
    #: on the strength of having established nothing.
    NO_CONCLUSIVE_URL_AUDIT = "NO_CONCLUSIVE_URL_AUDIT"

    #: The gold benchmark this recall would be measured against is absent, or
    #: says in its own manifest that it is not evaluation-ready.
    SOURCE_BENCHMARK_NOT_EVALUATION_READY = "SOURCE_BENCHMARK_NOT_EVALUATION_READY"

    #: Per-opportunity skill requirements are not frozen in the Phase 10.1 record
    #: contract, so a skill-coverage rate has no input. Absent, not empty.
    OPPORTUNITY_SKILLS_NOT_FROZEN = "OPPORTUNITY_SKILLS_NOT_FROZEN"

    #: The result carries no `ProfileTargetBindingEvidence`, so there is no
    #: target to compare a posting's country against.
    TARGET_SCOPE_BINDING_MISSING = "TARGET_SCOPE_BINDING_MISSING"

    #: A target binding exists, is coherent, and says the target country is
    #: UNKNOWN. The reason code that carries the whole `UNKNOWN != FALSE` rule.
    TARGET_COUNTRY_UNKNOWN = "TARGET_COUNTRY_UNKNOWN"

    #: No record in the universe carries a `published_at` this build can read as
    #: a date at or before the as-of date, so a *mean age* has no denominator.
    #: The freshness **distribution** is still computable, and still computed:
    #: it is hosted by `PUBLICATION_DATE_COVERAGE`, which stays COMPUTED.
    NO_VALID_PUBLICATION_DATES = "NO_VALID_PUBLICATION_DATES"

    #: No declared source universe is bound.
    DECLARED_SOURCE_UNIVERSE_MISSING = "DECLARED_SOURCE_UNIVERSE_MISSING"

    #: No deduplication evidence is bound.
    DEDUP_EVIDENCE_MISSING = "DEDUP_EVIDENCE_MISSING"

    #: No freshness binding is attached, so there is no as-of date against which
    #: an age could be computed. Deliberately not "we used today".
    FRESHNESS_BINDING_MISSING = "FRESHNESS_BINDING_MISSING"


# --------------------------------------------------------------------------
# evidence classes and the evidence members a metric may use
# --------------------------------------------------------------------------


class BusinessEvidenceClass(StrEnum):
    """What a number is *allowed to claim*, given what it was computed from.

    **A property of the metric, not of the report.** It is fixed per metric in
    `BUSINESS_METRIC_DEFINITIONS` and checked against the evidence actually
    projected for that metric, so a `DATA_AI_RATE` computed in the same run as a
    source-map coverage stays `SNAPSHOT_DESCRIPTIVE`.
    """

    #: Evidence from the frozen Phase 10.1 dataset and nothing else.
    SNAPSHOT_DESCRIPTIVE = "SNAPSHOT_DESCRIPTIVE"

    #: Evidence that includes an external, temporal observation — a frozen HTTP
    #: audit above all. Such evidence is true of one moment, so the audit says
    #: when its campaign ran and every attempted observation says when it was
    #: taken.
    EXTERNAL_AUDIT_DESCRIPTIVE = "EXTERNAL_AUDIT_DESCRIPTIVE"

    #: Evidence from a declared, versioned universe — the Phase 7C source map
    #: above all. A declaration is authored: a coverage number over it is a claim
    #: about our own stated ambition rather than about the market.
    DECLARED_UNIVERSE_DESCRIPTIVE = "DECLARED_UNIVERSE_DESCRIPTIVE"

    #: Reserved for a benchmark that is genuinely `evaluation_ready` **and**
    #: identified by a digest of its rows. Nothing in this repository is, so the
    #: gate fails closed today, by name.
    EVALUATION_READY_BENCHMARK = "EVALUATION_READY_BENCHMARK"


class BusinessEvidenceMember(StrEnum):
    """The kinds of *optional* evidence a business metric may rest on. Closed.

    The frozen cohort binding is deliberately not here: it is mandatory on every
    context, scope and evidence view, because every business metric in this
    contract is a statement about one frozen cohort.
    """

    PROFILE_TARGET_BINDING = "PROFILE_TARGET_BINDING"
    DECLARED_SOURCE_UNIVERSE = "DECLARED_SOURCE_UNIVERSE"
    URL_AUDIT_BINDING = "URL_AUDIT_BINDING"
    DEDUP_EVIDENCE = "DEDUP_EVIDENCE"
    BENCHMARK_BINDING = "BENCHMARK_BINDING"
    FRESHNESS_BINDING = "FRESHNESS_BINDING"


#: Member -> the attribute that carries it on a run context, an evidence view
#: and a scope. One mapping, so no verifier spells a field name a second time.
EVIDENCE_MEMBER_ATTRIBUTES: Mapping[BusinessEvidenceMember, str] = {
    BusinessEvidenceMember.PROFILE_TARGET_BINDING: "profile_target_binding",
    BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE: "declared_source_universe",
    BusinessEvidenceMember.URL_AUDIT_BINDING: "url_audit_binding",
    BusinessEvidenceMember.DEDUP_EVIDENCE: "dedup_evidence",
    BusinessEvidenceMember.BENCHMARK_BINDING: "benchmark_binding",
    BusinessEvidenceMember.FRESHNESS_BINDING: "freshness_binding",
}

assert set(EVIDENCE_MEMBER_ATTRIBUTES) == set(BusinessEvidenceMember)

#: Which member *defines* each universe — the artefact whose own count is that
#: universe's size. `FROZEN_COHORT` is absent because the cohort binding is
#: mandatory rather than optional; its size is checked all the same.
UNIVERSE_DEFINING_MEMBERS: Mapping[
    BusinessMetricUniverse, BusinessEvidenceMember
] = {
    BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE: (
        BusinessEvidenceMember.DEDUP_EVIDENCE
    ),
    BusinessMetricUniverse.URL_AUDITED_UNIVERSE: (
        BusinessEvidenceMember.URL_AUDIT_BINDING
    ),
    BusinessMetricUniverse.DECLARED_SOURCE_UNIVERSE: (
        BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE
    ),
}

#: The reason a metric **must** state when a required member is absent. One
#: mapping, so "the audit is missing" cannot be reported as "no conclusive
#: audit" and a reader can always tell an absence from an insufficiency.
MEMBER_ABSENCE_REASONS: Mapping[
    BusinessEvidenceMember, BusinessMetricUnavailableReason
] = {
    BusinessEvidenceMember.PROFILE_TARGET_BINDING: (
        BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING
    ),
    BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE: (
        BusinessMetricUnavailableReason.DECLARED_SOURCE_UNIVERSE_MISSING
    ),
    BusinessEvidenceMember.URL_AUDIT_BINDING: (
        BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING
    ),
    BusinessEvidenceMember.DEDUP_EVIDENCE: (
        BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING
    ),
    BusinessEvidenceMember.BENCHMARK_BINDING: (
        BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY
    ),
    BusinessEvidenceMember.FRESHNESS_BINDING: (
        BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING
    ),
}

assert set(MEMBER_ABSENCE_REASONS) == set(BusinessEvidenceMember)


def evidence_member_of(value: Any) -> BusinessEvidenceMember:
    """A member of the closed evidence vocabulary, or a refusal."""
    try:
        return BusinessEvidenceMember(value)
    except ValueError as error:
        raise BusinessMetricContractError(
            f"{value!r} is not a business evidence member; this build knows "
            f"{[str(item) for item in BusinessEvidenceMember]}"
        ) from error


# --------------------------------------------------------------------------
# the metric registry, and the closed definition of each metric
# --------------------------------------------------------------------------


class BusinessMetricName(StrEnum):
    """The closed registry of business / data-quality metrics.

    **A registry, not an implementation.** Phase 10.4a computes none of these.
    What each one is defined over, may rest on and may refuse is not here but in
    `BUSINESS_METRIC_DEFINITIONS`, which is the part a caller cannot override.
    """

    # ------------------------------------------------------------ targeting --
    #: `match_count / cohort_size`. The denominator is the **whole cohort**.
    TARGET_COUNTRY_MATCH_RATE = "TARGET_COUNTRY_MATCH_RATE"

    # ------------------------------------------------------ classification --
    DATA_AI_RATE = "DATA_AI_RATE"
    CORE_DATA_AI_RATE = "CORE_DATA_AI_RATE"
    CLASSIFICATION_COVERAGE_RATE = "CLASSIFICATION_COVERAGE_RATE"
    PFE_OR_INTERNSHIP_RATE = "PFE_OR_INTERNSHIP_RATE"
    OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE = (
        "OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE"
    )

    # ------------------------------------------------------- deduplication --
    APPLIED_DUPLICATE_RATE = "APPLIED_DUPLICATE_RATE"
    DUPLICATE_CLUSTER_RATE = "DUPLICATE_CLUSTER_RATE"

    # ----------------------------------------------------------------- URLs --
    BROKEN_URL_RATE = "BROKEN_URL_RATE"
    URL_AUDIT_COVERAGE_RATE = "URL_AUDIT_COVERAGE_RATE"
    URL_CONCLUSIVE_COVERAGE_RATE = "URL_CONCLUSIVE_COVERAGE_RATE"
    MISSING_ACTION_URL_RATE = "MISSING_ACTION_URL_RATE"

    # ------------------------------------------------------------- geography --
    UNKNOWN_LOCATION_RATE = "UNKNOWN_LOCATION_RATE"

    # ------------------------------------------------------------ freshness --
    #: The **host of the freshness distribution**, and the reason it is not
    #: merely a coverage number: classifying a `published_at` as MISSING, INVALID
    #: or one of six ages needs the as-of date, so this metric requires the
    #: frozen freshness binding like the mean does.
    PUBLICATION_DATE_COVERAGE = "PUBLICATION_DATE_COVERAGE"
    MEAN_KNOWN_FRESHNESS_SCORE = "MEAN_KNOWN_FRESHNESS_SCORE"

    # --------------------------------------------------------- completeness --
    DESCRIPTION_PRESENCE_RATE = "DESCRIPTION_PRESENCE_RATE"
    LOCATION_TEXT_PRESENCE_RATE = "LOCATION_TEXT_PRESENCE_RATE"
    ACTION_URL_PRESENCE_RATE = "ACTION_URL_PRESENCE_RATE"
    DEADLINE_DATE_COVERAGE = "DEADLINE_DATE_COVERAGE"
    SOURCE_PROVENANCE_COVERAGE_RATE = "SOURCE_PROVENANCE_COVERAGE_RATE"
    NORMAL_LISTING_RATE = "NORMAL_LISTING_RATE"

    # ----------------------------------------------------- observed sources --
    OBSERVED_SOURCE_CONTRIBUTION = "OBSERVED_SOURCE_CONTRIBUTION"
    MULTI_SOURCE_RECORD_RATE = "MULTI_SOURCE_RECORD_RATE"
    MISSING_SOURCE_PROVENANCE_RATE = "MISSING_SOURCE_PROVENANCE_RATE"

    # ----------------------------------------------------- declared sources --
    ACTIVE_DECLARED_SOURCE_RATE = "ACTIVE_DECLARED_SOURCE_RATE"
    DECLARED_SOURCE_DISCOVERY_RECALL = "DECLARED_SOURCE_DISCOVERY_RECALL"

    # -------------------------------------------------------------- skills --
    OPPORTUNITY_SKILL_COVERAGE = "OPPORTUNITY_SKILL_COVERAGE"


@dataclass(frozen=True)
class BusinessMetricDefinition:
    """What one metric is defined over, may rest on, and may refuse.

    Four facts, none of which a caller may choose:

    * `universe` — the one closed set this metric is a fraction of, whose *size*
      the builder checks against the artefact that defines it;
    * `evidence_class` — what a number from this metric may claim. Per metric,
      never per report;
    * `required_members` / `optional_members` — the evidence this metric uses. A
      `COMPUTED` result must carry every required member; an `N_A` may omit one,
      which is exactly how a missing binding becomes a stated reason code.
      Anything outside `permitted_members` may not appear in this metric's scope
      or evidence view at all;
    * `permitted_reasons` — the refusals this metric may state. An empty set
      means the metric has no legitimate `N_A` in v1: over a coherent, non-empty
      cohort a composition rate is always computable, so an unavailable one
      would be hiding a bug rather than reporting a fact.
    """

    universe: BusinessMetricUniverse
    evidence_class: BusinessEvidenceClass
    required_members: frozenset[BusinessEvidenceMember] = frozenset()
    optional_members: frozenset[BusinessEvidenceMember] = frozenset()
    permitted_reasons: frozenset[BusinessMetricUnavailableReason] = frozenset()

    @property
    def permitted_members(self) -> frozenset[BusinessEvidenceMember]:
        return self.required_members | self.optional_members


_SNAPSHOT = BusinessEvidenceClass.SNAPSHOT_DESCRIPTIVE
_AUDIT = BusinessEvidenceClass.EXTERNAL_AUDIT_DESCRIPTIVE
_DECLARED = BusinessEvidenceClass.DECLARED_UNIVERSE_DESCRIPTIVE
_BENCHMARK = BusinessEvidenceClass.EVALUATION_READY_BENCHMARK

_COHORT = BusinessMetricUniverse.FROZEN_COHORT
_PRE_DEDUP = BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE
_AUDITED = BusinessMetricUniverse.URL_AUDITED_UNIVERSE
_DECLARED_SOURCES = BusinessMetricUniverse.DECLARED_SOURCE_UNIVERSE

_TARGET = BusinessEvidenceMember.PROFILE_TARGET_BINDING
_SOURCE_MAP = BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE
_URL_AUDIT = BusinessEvidenceMember.URL_AUDIT_BINDING
_DEDUP = BusinessEvidenceMember.DEDUP_EVIDENCE
_BENCH = BusinessEvidenceMember.BENCHMARK_BINDING
_FRESHNESS = BusinessEvidenceMember.FRESHNESS_BINDING

_NO_AUDIT = BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING
_NO_CONCLUSIVE = BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT
_NO_BENCHMARK = (
    BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY
)
_NO_SKILLS = BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN
_NO_TARGET_BINDING = (
    BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING
)
_UNKNOWN_TARGET = BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN
_NO_DATES = BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES
_NO_SOURCE_MAP = (
    BusinessMetricUnavailableReason.DECLARED_SOURCE_UNIVERSE_MISSING
)
_NO_DEDUP = BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING
_NO_FRESHNESS = BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING


def _composition_metric() -> BusinessMetricDefinition:
    """A snapshot composition or completeness rate over the frozen cohort.

    No evidence beyond the cohort, and **no permitted refusal**: over a coherent,
    non-empty cohort every one of these is a count divided by the cohort size, so
    an `N_A` would be reporting a bug as a fact about the data.
    """
    return BusinessMetricDefinition(universe=_COHORT, evidence_class=_SNAPSHOT)


#: **The closed metric table.** The single most important object in this module:
#: one universe, one evidence class, one set of required artefacts and one set of
#: permitted refusals per metric.
BUSINESS_METRIC_DEFINITIONS: Mapping[
    BusinessMetricName, BusinessMetricDefinition
] = {
    # ---- FROZEN_COHORT -------------------------------------------------
    #: The frozen definition: `MATCH_count / FROZEN_COHORT_SIZE`. A posting whose
    #: country nobody could place stays in the denominator and is counted as an
    #: UNKNOWN verdict; defining the denominator as "records whose country is
    #: known" would delete the pipeline's own uncertainty from the measurement.
    #: Its two refusals are distinguished by condition, not by taste: no binding
    #: is `TARGET_SCOPE_BINDING_MISSING`, a binding with no country is
    #: `TARGET_COUNTRY_UNKNOWN`, and COMPUTED needs a country.
    BusinessMetricName.TARGET_COUNTRY_MATCH_RATE: BusinessMetricDefinition(
        universe=_COHORT,
        evidence_class=_SNAPSHOT,
        required_members=frozenset({_TARGET}),
        permitted_reasons=frozenset({_NO_TARGET_BINDING, _UNKNOWN_TARGET}),
    ),
    BusinessMetricName.DATA_AI_RATE: _composition_metric(),
    BusinessMetricName.CORE_DATA_AI_RATE: _composition_metric(),
    BusinessMetricName.CLASSIFICATION_COVERAGE_RATE: _composition_metric(),
    BusinessMetricName.PFE_OR_INTERNSHIP_RATE: _composition_metric(),
    BusinessMetricName.OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE: (
        _composition_metric()
    ),
    #: How many cohort records absorbed at least one duplicate — a different
    #: question from how many duplicates there were, and stated over the cohort.
    BusinessMetricName.DUPLICATE_CLUSTER_RATE: BusinessMetricDefinition(
        universe=_COHORT,
        evidence_class=_SNAPSHOT,
        required_members=frozenset({_DEDUP}),
        permitted_reasons=frozenset({_NO_DEDUP}),
    ),
    #: Records with no frozen action URL at all. A selection fact about the
    #: snapshot, and emphatically not an audit result.
    BusinessMetricName.MISSING_ACTION_URL_RATE: _composition_metric(),
    BusinessMetricName.UNKNOWN_LOCATION_RATE: _composition_metric(),
    #: Hosts the eight-state freshness distribution, which is why it needs the
    #: as-of date: `INVALID` includes a `published_at` later than the snapshot
    #: was taken, and that is undecidable without it. Stays COMPUTED even when
    #: not one date is valid — the distribution then says so, bucket by bucket.
    BusinessMetricName.PUBLICATION_DATE_COVERAGE: BusinessMetricDefinition(
        universe=_COHORT,
        evidence_class=_SNAPSHOT,
        required_members=frozenset({_FRESHNESS}),
        permitted_reasons=frozenset({_NO_FRESHNESS}),
    ),
    #: The secondary scalar. `N_A / NO_VALID_PUBLICATION_DATES` when no date is
    #: valid — and the distribution is still available, on the coverage result.
    BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE: BusinessMetricDefinition(
        universe=_COHORT,
        evidence_class=_SNAPSHOT,
        required_members=frozenset({_FRESHNESS}),
        permitted_reasons=frozenset({_NO_FRESHNESS, _NO_DATES}),
    ),
    BusinessMetricName.DESCRIPTION_PRESENCE_RATE: _composition_metric(),
    BusinessMetricName.LOCATION_TEXT_PRESENCE_RATE: _composition_metric(),
    BusinessMetricName.ACTION_URL_PRESENCE_RATE: _composition_metric(),
    BusinessMetricName.DEADLINE_DATE_COVERAGE: _composition_metric(),
    BusinessMetricName.SOURCE_PROVENANCE_COVERAGE_RATE: _composition_metric(),
    BusinessMetricName.NORMAL_LISTING_RATE: _composition_metric(),
    BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION: _composition_metric(),
    BusinessMetricName.MULTI_SOURCE_RECORD_RATE: _composition_metric(),
    BusinessMetricName.MISSING_SOURCE_PROVENANCE_RATE: _composition_metric(),
    #: Always `N_A / OPPORTUNITY_SKILLS_NOT_FROZEN` in v1: per-opportunity skill
    #: requirements are not in the Phase 10.1 record contract, so a COMPUTED
    #: result is refused outright — see `ALWAYS_UNAVAILABLE_METRICS`.
    BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE: BusinessMetricDefinition(
        universe=_COHORT,
        evidence_class=_SNAPSHOT,
        permitted_reasons=frozenset({_NO_SKILLS}),
    ),
    # ---- PRE_DEDUP_REPRESENTED_UNIVERSE ---------------------------------
    #: `D / (S + D)`. The merged tombstones are outside the cohort by
    #: construction, so the same numerator over the cohort would be a rate whose
    #: denominator excludes half of what it counts.
    BusinessMetricName.APPLIED_DUPLICATE_RATE: BusinessMetricDefinition(
        universe=_PRE_DEDUP,
        evidence_class=_SNAPSHOT,
        required_members=frozenset({_DEDUP}),
        permitted_reasons=frozenset({_NO_DEDUP}),
    ),
    # ---- URL_AUDITED_UNIVERSE -------------------------------------------
    #: COMPUTED requires at least one conclusive observation: a broken-URL rate
    #: over an audit that established nothing is 0.0 by construction.
    BusinessMetricName.BROKEN_URL_RATE: BusinessMetricDefinition(
        universe=_AUDITED,
        evidence_class=_AUDIT,
        required_members=frozenset({_URL_AUDIT}),
        permitted_reasons=frozenset({_NO_AUDIT, _NO_CONCLUSIVE}),
    ),
    #: Coverage rates stay computable over an audit with nothing conclusive in
    #: it — that is precisely the fact they exist to report.
    BusinessMetricName.URL_AUDIT_COVERAGE_RATE: BusinessMetricDefinition(
        universe=_AUDITED,
        evidence_class=_AUDIT,
        required_members=frozenset({_URL_AUDIT}),
        permitted_reasons=frozenset({_NO_AUDIT}),
    ),
    BusinessMetricName.URL_CONCLUSIVE_COVERAGE_RATE: BusinessMetricDefinition(
        universe=_AUDITED,
        evidence_class=_AUDIT,
        required_members=frozenset({_URL_AUDIT}),
        permitted_reasons=frozenset({_NO_AUDIT}),
    ),
    # ---- DECLARED_SOURCE_UNIVERSE ---------------------------------------
    BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE: BusinessMetricDefinition(
        universe=_DECLARED_SOURCES,
        evidence_class=_DECLARED,
        required_members=frozenset({_SOURCE_MAP}),
        permitted_reasons=frozenset({_NO_SOURCE_MAP}),
    ),
    #: Needs **both**: the declared universe it is a recall over, and a genuinely
    #: evaluation-ready benchmark to be a recall *of*, scoped to the same country.
    BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL: BusinessMetricDefinition(
        universe=_DECLARED_SOURCES,
        evidence_class=_BENCHMARK,
        required_members=frozenset({_SOURCE_MAP, _BENCH}),
        permitted_reasons=frozenset({_NO_SOURCE_MAP, _NO_BENCHMARK}),
    ),
}

#: Metrics that cannot be COMPUTED at all under the current Phase 10.1 record
#: contract, mapped to the refusal they must state. Keyed to
#: `EVALUATION_DATASET_SCHEMA_VERSION` in the message rather than to a guess:
#: freezing opportunity skills into the dataset is what would remove this entry.
ALWAYS_UNAVAILABLE_METRICS: Mapping[
    BusinessMetricName, BusinessMetricUnavailableReason
] = {BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE: _NO_SKILLS}

#: Exhaustive, and asserted at import.
assert set(BUSINESS_METRIC_DEFINITIONS) == set(BusinessMetricName)
#: A metric whose universe is defined by an artefact must require that artefact.
assert all(
    UNIVERSE_DEFINING_MEMBERS[definition.universe] in definition.required_members
    for definition in BUSINESS_METRIC_DEFINITIONS.values()
    if definition.universe in UNIVERSE_DEFINING_MEMBERS
)
#: Every required member's absence reason must be a refusal the metric may
#: state, or the metric would have a condition it could not report.
assert all(
    {
        MEMBER_ABSENCE_REASONS[member] for member in definition.required_members
    }
    <= definition.permitted_reasons
    for definition in BUSINESS_METRIC_DEFINITIONS.values()
)
#: An always-unavailable metric must permit exactly the refusal it always states.
assert all(
    reason in BUSINESS_METRIC_DEFINITIONS[metric].permitted_reasons
    for metric, reason in ALWAYS_UNAVAILABLE_METRICS.items()
)


def metric_definition(metric: Any) -> BusinessMetricDefinition:
    """The closed definition of one metric, or a refusal."""
    if not isinstance(metric, BusinessMetricName):
        raise BusinessMetricContractError(
            f"{metric!r} is not a business metric name; this build knows "
            f"{[str(item) for item in BusinessMetricName]}"
        )
    return BUSINESS_METRIC_DEFINITIONS[metric]


def require_metric_universe(metric: Any, universe: Any) -> BusinessMetricUniverse:
    """Refuse a metric stated over a universe it is not defined over.

    A **contract failure**, never an `N_A`. `N_A` says "this evidence cannot
    answer the question"; a metric over the wrong universe is a different
    question, already answered, filed under the name of this one.
    """
    definition = metric_definition(metric)
    if not isinstance(universe, BusinessMetricUniverse):
        raise BusinessMetricContractError(
            f"{universe!r} is not a business metric universe; a result states "
            "which closed set it is about, and this contract has no default "
            "universe"
        )
    if universe is not definition.universe:
        raise BusinessMetricContractError(
            f"{metric} is defined over {definition.universe} and this result "
            f"states {universe}; the pairing is fixed by the contract, and a "
            f"{metric} over {universe} would be a different number reported "
            f"under this one's name"
        )
    return universe


def require_permitted_reason(
    metric: Any, reason: Any
) -> BusinessMetricUnavailableReason:
    """Refuse a refusal this metric is not entitled to state.

    The half a closed enum cannot do on its own. `DATA_AI_RATE / N_A /
    URL_AUDIT_EVIDENCE_MISSING` names a real reason code and is nonsense: the
    metric reads no audit, so the audit's absence cannot be why it has no
    number. A reason borrowed from another metric would send a reader looking
    for evidence that was never part of the question.
    """
    definition = metric_definition(metric)
    if not isinstance(reason, BusinessMetricUnavailableReason):
        raise BusinessMetricContractError(
            f"{reason!r} is not a business metric unavailable reason; this "
            f"build knows {[str(item) for item in BusinessMetricUnavailableReason]}"
        )
    if reason not in definition.permitted_reasons:
        permitted = sorted(str(item) for item in definition.permitted_reasons)
        entitled = (
            str(permitted)
            if permitted
            else (
                "none — over a coherent, non-empty cohort this metric is "
                "always computable"
            )
        )
        raise BusinessMetricContractError(
            f"{metric} may not state {reason}; the refusals it is entitled to "
            f"are {entitled}"
        )
    return reason


# --------------------------------------------------------------------------
# metric keys and dimensions
# --------------------------------------------------------------------------


class BusinessMetricDimension(StrEnum):
    """The kinds of dimension a metric key may carry. Closed, and short.

    A per-source contribution needs one number *per source*, and the sources are
    operational data: they arrive, they are renamed, they are retired. Putting
    them in `BusinessMetricName` would make the registry a mirror of a database
    table and every new source a contract change.
    """

    #: The metric is scalar: one number for the whole universe.
    NONE = "NONE"

    #: The metric is stated per observed `source_id`. In v1 this is permitted for
    #: `OBSERVED_SOURCE_CONTRIBUTION` and for nothing else.
    SOURCE_ID = "SOURCE_ID"


#: The metrics that are dimensional in v1 — which is to say, the metrics that
#: *require* a dimension.
DIMENSIONAL_BUSINESS_METRICS: frozenset[BusinessMetricName] = frozenset(
    {BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION}
)


@dataclass(frozen=True)
class BusinessMetricKey:
    """What exactly a result is a result about: a metric, optionally dimensioned.

    The invariants are enforced in `__post_init__` rather than by a builder,
    because a key is small, is constructed everywhere, and has no state a builder
    could add: an invalid key should simply not exist.
    """

    metric: BusinessMetricName
    dimension_kind: BusinessMetricDimension = BusinessMetricDimension.NONE
    dimension_value: str | None = None

    def __post_init__(self) -> None:
        metric_definition(self.metric)
        if not isinstance(self.dimension_kind, BusinessMetricDimension):
            raise BusinessMetricContractError(
                f"{self.dimension_kind!r} is not a business metric dimension; "
                f"this build knows "
                f"{[str(item) for item in BusinessMetricDimension]}"
            )
        if self.dimension_kind is BusinessMetricDimension.NONE:
            if self.dimension_value is not None:
                raise BusinessMetricContractError(
                    f"{self.metric} states dimension value "
                    f"{self.dimension_value!r} with dimension kind "
                    f"{BusinessMetricDimension.NONE}; a scalar metric has no "
                    "dimension to take a value in"
                )
            if self.metric in DIMENSIONAL_BUSINESS_METRICS:
                raise BusinessMetricContractError(
                    f"{self.metric} is dimensional in this contract and needs a "
                    f"{BusinessMetricDimension.SOURCE_ID} dimension; a "
                    "contribution with no source named is a number about nothing"
                )
            return
        if self.metric not in DIMENSIONAL_BUSINESS_METRICS:
            raise BusinessMetricContractError(
                f"{self.metric} is not dimensional in this contract, so it may "
                f"not carry a {self.dimension_kind} dimension; the metrics that "
                f"may are "
                f"{sorted(str(item) for item in DIMENSIONAL_BUSINESS_METRICS)}"
            )
        validate_text(
            self.dimension_value,
            subject=f"the {self.dimension_kind} dimension value of {self.metric}",
        )

    @property
    def definition(self) -> BusinessMetricDefinition:
        """The closed definition this key's metric is bound by."""
        return metric_definition(self.metric)


def business_metric_key_payload(key: BusinessMetricKey) -> dict[str, Any]:
    """The one canonical shape of a metric key."""
    return {
        "metric": str(key.metric),
        "dimension_kind": str(key.dimension_kind),
        "dimension_value": key.dimension_value,
    }


def business_metric_key_sort_key(key: BusinessMetricKey) -> tuple[str, str, str]:
    """The canonical order: metric, then dimension kind, then dimension value."""
    return (
        str(key.metric),
        str(key.dimension_kind),
        "" if key.dimension_value is None else key.dimension_value,
    )


def canonical_business_metric_keys(
    keys: Sequence[BusinessMetricKey],
) -> tuple[BusinessMetricKey, ...]:
    """Keys in the contract's canonical order. A set held in one order."""
    for key in keys:
        if not isinstance(key, BusinessMetricKey):
            raise BusinessMetricContractError(
                f"{key!r} is not a business metric key"
            )
    return tuple(sorted(keys, key=business_metric_key_sort_key))


# --------------------------------------------------------------------------
# the freshness partition, and the structured support blocks
# --------------------------------------------------------------------------


class FreshnessBucket(StrEnum):
    """The frozen eight-state freshness partition.

    Exhaustive and mutually exclusive over every record in the cohort, and the
    first two states are the reason it has eight rather than six:

    * `MISSING` — the record states no `published_at` at all;
    * `INVALID` — it states one this build cannot use: malformed, **or later
      than the as-of date**. A posting that claims to have been published after
      the snapshot was taken has a date that cannot be an age, and reading it as
      "0 days old" would make the freshest bucket a collecting point for clock
      errors and bad parsing.

    Neither is folded into an age bucket, and neither is dropped: "we do not know
    when this was published" is a measurable state of the pipeline, not an
    absence to be skipped.
    """

    MISSING = "MISSING"
    INVALID = "INVALID"
    AGE_0_2 = "AGE_0_2"
    AGE_3_7 = "AGE_3_7"
    AGE_8_14 = "AGE_8_14"
    AGE_15_30 = "AGE_15_30"
    AGE_31_60 = "AGE_31_60"
    AGE_GT_60 = "AGE_GT_60"


#: The canonical order of the partition, stated once so that every distribution
#: is held in it. The two non-age states come first: a reader looking at a
#: freshness report should see what is unknown before what is old.
FRESHNESS_BUCKET_ORDER: tuple[FreshnessBucket, ...] = (
    FreshnessBucket.MISSING,
    FreshnessBucket.INVALID,
    FreshnessBucket.AGE_0_2,
    FreshnessBucket.AGE_3_7,
    FreshnessBucket.AGE_8_14,
    FreshnessBucket.AGE_15_30,
    FreshnessBucket.AGE_31_60,
    FreshnessBucket.AGE_GT_60,
)

#: The inclusive day bounds of the age buckets, with `None` for "no upper
#: bound". `MISSING` and `INVALID` are deliberately absent: they are not ages,
#: and a bound of `(None, None)` for them would invite arithmetic.
FRESHNESS_BUCKET_DAY_BOUNDS: Mapping[FreshnessBucket, tuple[int, int | None]] = {
    FreshnessBucket.AGE_0_2: (0, 2),
    FreshnessBucket.AGE_3_7: (3, 7),
    FreshnessBucket.AGE_8_14: (8, 14),
    FreshnessBucket.AGE_15_30: (15, 30),
    FreshnessBucket.AGE_31_60: (31, 60),
    FreshnessBucket.AGE_GT_60: (61, None),
}

#: The age buckets in order, for a formula that will walk them.
FRESHNESS_AGE_BUCKETS: tuple[FreshnessBucket, ...] = tuple(
    bucket
    for bucket in FRESHNESS_BUCKET_ORDER
    if bucket in FRESHNESS_BUCKET_DAY_BOUNDS
)

#: Stated as import-time assertions because a silent gap or overlap in the
#: partition would misfile records without a single test failing for the right
#: reason.
assert set(FRESHNESS_BUCKET_ORDER) == set(FreshnessBucket)
assert len(FRESHNESS_BUCKET_ORDER) == len(FreshnessBucket) == 8
assert set(FRESHNESS_BUCKET_DAY_BOUNDS) == set(FreshnessBucket) - {
    FreshnessBucket.MISSING,
    FreshnessBucket.INVALID,
}
assert [
    FRESHNESS_BUCKET_DAY_BOUNDS[bucket][0] for bucket in FRESHNESS_AGE_BUCKETS
] == [0, 3, 8, 15, 31, 61]
assert all(
    FRESHNESS_BUCKET_DAY_BOUNDS[bucket][1] is None
    or FRESHNESS_BUCKET_DAY_BOUNDS[bucket][1]
    == FRESHNESS_BUCKET_DAY_BOUNDS[
        FRESHNESS_AGE_BUCKETS[FRESHNESS_AGE_BUCKETS.index(bucket) + 1]
    ][0]
    - 1
    for bucket in FRESHNESS_AGE_BUCKETS
)

#: The **one** metric a freshness distribution may be attached to. It is
#: `PUBLICATION_DATE_COVERAGE` rather than the mean, and the choice matters: the
#: distribution is the canonical freshness answer and must survive the case
#: where no date is valid at all. The mean cannot — it is `N_A /
#: NO_VALID_PUBLICATION_DATES` then — so hosting the partition on the mean would
#: delete the whole eight-state picture in exactly the situation that picture
#: was most worth having.
FRESHNESS_DISTRIBUTION_HOST_METRIC = BusinessMetricName.PUBLICATION_DATE_COVERAGE

#: Likewise for the target verdict breakdown, whose three counts must sum to the
#: cohort — the frozen definition of `TARGET_COUNTRY_MATCH_RATE`.
TARGET_VERDICT_HOST_METRIC = BusinessMetricName.TARGET_COUNTRY_MATCH_RATE


@dataclass(frozen=True)
class FreshnessBucketCount:
    """How many records of a universe fall in one bucket."""

    bucket: FreshnessBucket
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.bucket, FreshnessBucket):
            raise BusinessMetricContractError(
                f"{self.bucket!r} is not a freshness bucket"
            )
        validate_count(self.count, subject=f"the {self.bucket} bucket count")


@dataclass(frozen=True)
class FreshnessBucketDistribution:
    """The whole eight-state freshness partition of the cohort, as counts.

    **The canonical freshness answer**, not a free-floating diagnostic. Its
    contractual identity is fourfold and each part is enforced:

    1. it is exhaustive — every bucket appears exactly once, in
       `FRESHNESS_BUCKET_ORDER`. There is no "other" and no omission;
    2. its counts sum to `universe_size`, checked rather than believed;
    3. it may be carried by `FRESHNESS_DISTRIBUTION_HOST_METRIC` and by no other
       metric, only on a `COMPUTED` result, and only when its `universe_size`
       equals the result's — all three enforced in `BusinessMetricResult`;
    4. that host's universe is `FROZEN_COHORT`, and the builder additionally
       requires the result's universe size to equal the frozen cohort's, so the
       partition always covers exactly the cohort.

    It is therefore inside `result_fingerprint` like every other part of the
    answer, and it remains computable when `known_date_count` is zero — which is
    the case the mean cannot report.
    """

    entries: tuple[FreshnessBucketCount, ...]
    universe_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise BusinessMetricContractError(
                "a freshness distribution's entries are not an immutable sequence"
            )
        for entry in self.entries:
            if not isinstance(entry, FreshnessBucketCount):
                raise BusinessMetricContractError(
                    f"{entry!r} is not a freshness bucket count"
                )
        buckets = tuple(entry.bucket for entry in self.entries)
        if buckets != FRESHNESS_BUCKET_ORDER:
            raise BusinessMetricContractError(
                "a freshness distribution states every bucket exactly once, in "
                f"the canonical order "
                f"{[str(item) for item in FRESHNESS_BUCKET_ORDER]}, not "
                f"{[str(item) for item in buckets]}"
            )
        validate_count(
            self.universe_size, subject="the freshness distribution universe size"
        )
        total = sum(entry.count for entry in self.entries)
        if total != self.universe_size:
            raise BusinessMetricBindingError(
                f"the freshness distribution counts {total} records and declares "
                f"a universe of {self.universe_size}; a partition that does not "
                "sum to its universe has lost or double-counted a record"
            )

    def count(self, bucket: FreshnessBucket) -> int:
        for entry in self.entries:
            if entry.bucket is bucket:
                return entry.count
        raise BusinessMetricContractError(f"{bucket!r} is not a freshness bucket")

    @property
    def known_date_count(self) -> int:
        """Records with a usable `published_at` — neither MISSING nor INVALID.

        Zero is a perfectly good answer, and the one that licenses
        `MEAN_KNOWN_FRESHNESS_SCORE -> N_A / NO_VALID_PUBLICATION_DATES` while
        this distribution stays COMPUTED on its own host.
        """
        return (
            self.universe_size
            - self.count(FreshnessBucket.MISSING)
            - self.count(FreshnessBucket.INVALID)
        )


def freshness_bucket_distribution_payload(
    distribution: FreshnessBucketDistribution,
) -> dict[str, Any]:
    """The one canonical shape of a freshness partition."""
    return {
        "entries": [
            {"bucket": str(entry.bucket), "count": entry.count}
            for entry in distribution.entries
        ],
        "universe_size": distribution.universe_size,
    }


@dataclass(frozen=True)
class TargetVerdictBreakdown:
    """The three verdicts of `TARGET_COUNTRY_MATCH_RATE`, summing to the cohort.

    The shape that makes the frozen definition checkable:

        target_country_match_rate = match_count / FROZEN_COHORT_SIZE

    `unknown_target_verdict_count` is the whole point. A posting the geographic
    resolver could not place is neither a match nor out of target, and it stays
    in the denominator: dropping it would delete the pipeline's own uncertainty
    from the measurement and inflate the rate by exactly the amount we are least
    sure about.

    **Mandatory** on a COMPUTED `TARGET_COUNTRY_MATCH_RATE`: a match rate whose
    three verdicts are not stated cannot be checked against its own definition.
    """

    match_count: int
    out_of_target_count: int
    unknown_target_verdict_count: int

    def __post_init__(self) -> None:
        validate_count(self.match_count, subject="the target match count")
        validate_count(self.out_of_target_count, subject="the out-of-target count")
        validate_count(
            self.unknown_target_verdict_count,
            subject="the unknown target verdict count",
        )

    @property
    def total(self) -> int:
        return (
            self.match_count
            + self.out_of_target_count
            + self.unknown_target_verdict_count
        )


def target_verdict_breakdown_payload(
    breakdown: TargetVerdictBreakdown,
) -> dict[str, Any]:
    """The one canonical shape of a target verdict breakdown."""
    return {
        "match_count": breakdown.match_count,
        "out_of_target_count": breakdown.out_of_target_count,
        "unknown_target_verdict_count": breakdown.unknown_target_verdict_count,
    }


@dataclass(frozen=True)
class DataAiQualificationBreakdown:
    """How the Data/AI classifier disposed of every record in the cohort.

    Five states, exhaustive over the cohort, and the last two are why the block
    exists rather than a single rate:

    * `uncertain_count` — the classifier read the posting and hedged;
    * `unclassified_count` — it never read it at all.

    Those are different facts and neither is "not Data/AI". A `DATA_AI_RATE`
    reported without them looks like a measurement of the market when part of it
    is a measurement of how much of the pipeline has run.

    **Mandatory** on a COMPUTED `DATA_AI_RATE`, where
    `numerator == core_target_count + adjacent_target_count`. Reused — not
    redefined — by `CORE_DATA_AI_RATE` (`numerator == core_target_count`) and
    `CLASSIFICATION_COVERAGE_RATE`
    (`numerator == universe_size - unclassified_count`), so the three rates can
    never be published from three different pictures of the same cohort.
    """

    core_target_count: int
    adjacent_target_count: int
    out_of_scope_count: int
    uncertain_count: int
    unclassified_count: int

    def __post_init__(self) -> None:
        for name in (
            "core_target_count",
            "adjacent_target_count",
            "out_of_scope_count",
            "uncertain_count",
            "unclassified_count",
        ):
            validate_count(getattr(self, name), subject=f"the {name}")

    @property
    def total(self) -> int:
        return (
            self.core_target_count
            + self.adjacent_target_count
            + self.out_of_scope_count
            + self.uncertain_count
            + self.unclassified_count
        )

    @property
    def data_ai_count(self) -> int:
        """Core plus adjacent: the numerator `DATA_AI_RATE` is defined as."""
        return self.core_target_count + self.adjacent_target_count

    @property
    def classified_count(self) -> int:
        """Everything the classifier actually reached."""
        return self.total - self.unclassified_count


def data_ai_qualification_breakdown_payload(
    breakdown: DataAiQualificationBreakdown,
) -> dict[str, Any]:
    """The one canonical shape of a Data/AI qualification breakdown."""
    return {
        "core_target_count": breakdown.core_target_count,
        "adjacent_target_count": breakdown.adjacent_target_count,
        "out_of_scope_count": breakdown.out_of_scope_count,
        "uncertain_count": breakdown.uncertain_count,
        "unclassified_count": breakdown.unclassified_count,
    }


@dataclass(frozen=True)
class OpportunityTypeBreakdown:
    """How every record in the cohort was typed, in seven exhaustive states.

    `unknown_type_count` and `unclassified_type_count` are separate for the same
    reason as above: a posting whose type the classifier could not determine is
    not a posting it never read.

    **Mandatory** on a COMPUTED `PFE_OR_INTERNSHIP_RATE`, where

        numerator == pfe_count + internship_count

    and **apprenticeship is deliberately not in it**. An apprenticeship is a
    different contract, a different duration and a different application season;
    folding it in would inflate the one number this project exists to move, and
    the count is kept beside it precisely so that anybody who wants the wider
    figure can add it themselves and say so.
    """

    pfe_count: int
    internship_count: int
    apprenticeship_count: int
    graduate_count: int
    job_count: int
    unknown_type_count: int
    unclassified_type_count: int

    def __post_init__(self) -> None:
        for name in (
            "pfe_count",
            "internship_count",
            "apprenticeship_count",
            "graduate_count",
            "job_count",
            "unknown_type_count",
            "unclassified_type_count",
        ):
            validate_count(getattr(self, name), subject=f"the {name}")

    @property
    def total(self) -> int:
        return (
            self.pfe_count
            + self.internship_count
            + self.apprenticeship_count
            + self.graduate_count
            + self.job_count
            + self.unknown_type_count
            + self.unclassified_type_count
        )

    @property
    def pfe_or_internship_count(self) -> int:
        """The numerator `PFE_OR_INTERNSHIP_RATE` is defined as. No apprenticeships."""
        return self.pfe_count + self.internship_count

    @property
    def typed_count(self) -> int:
        return self.total - self.unclassified_type_count


def opportunity_type_breakdown_payload(
    breakdown: OpportunityTypeBreakdown,
) -> dict[str, Any]:
    """The one canonical shape of an opportunity type breakdown."""
    return {
        "pfe_count": breakdown.pfe_count,
        "internship_count": breakdown.internship_count,
        "apprenticeship_count": breakdown.apprenticeship_count,
        "graduate_count": breakdown.graduate_count,
        "job_count": breakdown.job_count,
        "unknown_type_count": breakdown.unknown_type_count,
        "unclassified_type_count": breakdown.unclassified_type_count,
    }


@dataclass(frozen=True)
class ListingQualityBreakdown:
    """What kind of page each record in the cohort turned out to be.

    **Mandatory** on a COMPUTED `NORMAL_LISTING_RATE`, where
    `numerator == normal_listing_count`. The other three states are what the rate
    is *not*, and keeping them separate is what stops "this page is probably not
    a job posting" and "we have not classified this page" reading as one number.
    """

    normal_listing_count: int
    possible_non_job_page_count: int
    insufficient_content_count: int
    unclassified_count: int

    def __post_init__(self) -> None:
        for name in (
            "normal_listing_count",
            "possible_non_job_page_count",
            "insufficient_content_count",
            "unclassified_count",
        ):
            validate_count(getattr(self, name), subject=f"the {name}")

    @property
    def total(self) -> int:
        return (
            self.normal_listing_count
            + self.possible_non_job_page_count
            + self.insufficient_content_count
            + self.unclassified_count
        )


def listing_quality_breakdown_payload(
    breakdown: ListingQualityBreakdown,
) -> dict[str, Any]:
    """The one canonical shape of a listing quality breakdown."""
    return {
        "normal_listing_count": breakdown.normal_listing_count,
        "possible_non_job_page_count": breakdown.possible_non_job_page_count,
        "insufficient_content_count": breakdown.insufficient_content_count,
        "unclassified_count": breakdown.unclassified_count,
    }


@dataclass(frozen=True)
class UnknownLocationDiagnostics:
    """Why the cohort's locations are unknown, in three counts.

    **Not a partition**, and the difference matters. `UNKNOWN_LOCATION_RATE`
    counts a record with *no* geography segment or with *at least one* UNKNOWN
    segment — two conditions that cannot both hold of one record, so

        numerator == no_geography_segments_count + unknown_segment_record_count

    is an identity the contract enforces.

    `ambiguous_location_count` is a diagnostic beside it and is **not** in that
    numerator: an AMBIGUOUS segment means the resolver found several places the
    text could mean, which is a different failure from finding none, and a rate
    that merged them would hide which of the two the pipeline should fix. It may
    overlap the other two freely.
    """

    ambiguous_location_count: int
    no_geography_segments_count: int
    unknown_segment_record_count: int

    def __post_init__(self) -> None:
        for name in (
            "ambiguous_location_count",
            "no_geography_segments_count",
            "unknown_segment_record_count",
        ):
            validate_count(getattr(self, name), subject=f"the {name}")

    @property
    def unknown_location_count(self) -> int:
        """The numerator `UNKNOWN_LOCATION_RATE` is defined as."""
        return self.no_geography_segments_count + self.unknown_segment_record_count


def unknown_location_diagnostics_payload(
    diagnostics: UnknownLocationDiagnostics,
) -> dict[str, Any]:
    """The one canonical shape of the unknown-location diagnostics."""
    return {
        "ambiguous_location_count": diagnostics.ambiguous_location_count,
        "no_geography_segments_count": diagnostics.no_geography_segments_count,
        "unknown_segment_record_count": diagnostics.unknown_segment_record_count,
    }


@dataclass(frozen=True)
class FreshnessAvailabilityWitness:
    """The evidence behind `N_A / NO_VALID_PUBLICATION_DATES`, as a number.

    The one refusal in this contract that no binding could verify: whether any
    record carries a usable `published_at` is a fact about the records, and Phase
    10.4a holds none. An earlier version documented that and granted the reason
    unconditionally, which made it the single reason code a caller could state
    for free — exactly the shape "N_A as a shrug" takes.

    So the reason now carries a witness. It is not a fraction and cannot be
    mistaken for one: a single count, required to be zero, stating the thing the
    refusal asserts. Phase 10.4b derives it from the records; this slice makes it
    impossible to claim the refusal without stating it, and it is inside
    `result_fingerprint`, so a run that later turns out to have had dated records
    is a run whose witness was wrong rather than a run that said nothing.
    """

    known_date_count: int

    def __post_init__(self) -> None:
        validate_count(
            self.known_date_count, subject="the witnessed known publication date count"
        )


def freshness_availability_witness_payload(
    witness: FreshnessAvailabilityWitness,
) -> dict[str, Any]:
    """The one canonical shape of the freshness availability witness."""
    return {"known_date_count": witness.known_date_count}


# --------------------------------------------------------------------------
# support and result
# --------------------------------------------------------------------------

#: The structured block each metric **must** carry on a COMPUTED result, as the
#: attribute of `BusinessMetricSupport` that holds it. Phase 10.4a is the slice
#: that freezes the contract, so these are required here rather than left for
#: 10.4b to remember: a rate published without the partition it is a fraction of
#: cannot be checked against its own definition by anybody, ever.
REQUIRED_SUPPORT_BLOCKS: Mapping[BusinessMetricName, str] = {
    BusinessMetricName.TARGET_COUNTRY_MATCH_RATE: "target_verdict_breakdown",
    BusinessMetricName.DATA_AI_RATE: "data_ai_breakdown",
    BusinessMetricName.PFE_OR_INTERNSHIP_RATE: "opportunity_type_breakdown",
    BusinessMetricName.NORMAL_LISTING_RATE: "listing_quality_breakdown",
    BusinessMetricName.UNKNOWN_LOCATION_RATE: "unknown_location_diagnostics",
    BusinessMetricName.PUBLICATION_DATE_COVERAGE: "freshness_distribution",
}

#: Which metrics may carry each block at all. A block on any other metric is
#: refused: a partition describes one question, and attaching it to another is
#: how a number comes to be read as evidence for something nobody computed.
SUPPORT_BLOCK_HOSTS: Mapping[str, frozenset[BusinessMetricName]] = {
    "target_verdict_breakdown": frozenset(
        {BusinessMetricName.TARGET_COUNTRY_MATCH_RATE}
    ),
    "data_ai_breakdown": frozenset(
        {
            BusinessMetricName.DATA_AI_RATE,
            BusinessMetricName.CORE_DATA_AI_RATE,
            BusinessMetricName.CLASSIFICATION_COVERAGE_RATE,
        }
    ),
    "opportunity_type_breakdown": frozenset(
        {
            BusinessMetricName.PFE_OR_INTERNSHIP_RATE,
            BusinessMetricName.OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE,
        }
    ),
    "listing_quality_breakdown": frozenset({BusinessMetricName.NORMAL_LISTING_RATE}),
    "unknown_location_diagnostics": frozenset(
        {BusinessMetricName.UNKNOWN_LOCATION_RATE}
    ),
    "freshness_distribution": frozenset(
        {BusinessMetricName.PUBLICATION_DATE_COVERAGE}
    ),
    "freshness_availability_witness": frozenset(
        {BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE}
    ),
}

assert set(REQUIRED_SUPPORT_BLOCKS.values()) <= set(SUPPORT_BLOCK_HOSTS)
assert all(
    metric in SUPPORT_BLOCK_HOSTS[attribute]
    for metric, attribute in REQUIRED_SUPPORT_BLOCKS.items()
)

#: The blocks that are *computations* and therefore forbidden on an `N_A`
#: result. The witness is deliberately absent: it exists only on an `N_A`.
COMPUTED_ONLY_SUPPORT_BLOCKS: tuple[str, ...] = (
    "target_verdict_breakdown",
    "data_ai_breakdown",
    "opportunity_type_breakdown",
    "listing_quality_breakdown",
    "unknown_location_diagnostics",
    "freshness_distribution",
)


@dataclass(frozen=True)
class BusinessMetricSupport:
    """The numbers behind a business metric, so a reader can check the claim.

    `universe_size` is mandatory for every `COMPUTED` result, mandatory for an
    `N_A` whose universe is determinable, and *checked against the artefact that
    defines that universe* rather than merely required to exist.

    `numerator` and `denominator` are `None` for an `N_A`: a fabricated
    denominator is worse than an absent one.

    The structured blocks are typed objects rather than a free `dict[str, Any]`,
    each has a closed set of host metrics (`SUPPORT_BLOCK_HOSTS`), six of them
    are **required** on their host's COMPUTED result (`REQUIRED_SUPPORT_BLOCKS`),
    and every one of them is inside `result_fingerprint`.
    """

    numerator: float | None = None
    denominator: float | None = None
    universe_size: int | None = None
    freshness_distribution: FreshnessBucketDistribution | None = None
    target_verdict_breakdown: TargetVerdictBreakdown | None = None
    data_ai_breakdown: DataAiQualificationBreakdown | None = None
    opportunity_type_breakdown: OpportunityTypeBreakdown | None = None
    listing_quality_breakdown: ListingQualityBreakdown | None = None
    unknown_location_diagnostics: UnknownLocationDiagnostics | None = None
    freshness_availability_witness: FreshnessAvailabilityWitness | None = None

    def __post_init__(self) -> None:
        if self.numerator is not None:
            validate_finite_number(self.numerator, subject="the numerator")
        if self.denominator is not None:
            denominator = validate_finite_number(
                self.denominator, subject="the denominator"
            )
            if denominator < 0:
                raise BusinessMetricArgumentError(
                    f"the denominator must not be negative, not "
                    f"{self.denominator!r}"
                )
        if self.universe_size is not None:
            validate_count(self.universe_size, subject="the universe size")
        for attribute, expected in (
            ("freshness_distribution", FreshnessBucketDistribution),
            ("target_verdict_breakdown", TargetVerdictBreakdown),
            ("data_ai_breakdown", DataAiQualificationBreakdown),
            ("opportunity_type_breakdown", OpportunityTypeBreakdown),
            ("listing_quality_breakdown", ListingQualityBreakdown),
            ("unknown_location_diagnostics", UnknownLocationDiagnostics),
            ("freshness_availability_witness", FreshnessAvailabilityWitness),
        ):
            value = getattr(self, attribute)
            if value is not None and not isinstance(value, expected):
                raise BusinessMetricContractError(
                    f"{value!r} is not a {expected.__name__}"
                )


def business_metric_support_payload(
    support: BusinessMetricSupport,
) -> dict[str, Any]:
    """The one canonical shape of the support block."""
    return {
        "numerator": support.numerator,
        "denominator": support.denominator,
        "universe_size": support.universe_size,
        "freshness_distribution": (
            None
            if support.freshness_distribution is None
            else freshness_bucket_distribution_payload(support.freshness_distribution)
        ),
        "target_verdict_breakdown": (
            None
            if support.target_verdict_breakdown is None
            else target_verdict_breakdown_payload(support.target_verdict_breakdown)
        ),
        "data_ai_breakdown": (
            None
            if support.data_ai_breakdown is None
            else data_ai_qualification_breakdown_payload(support.data_ai_breakdown)
        ),
        "opportunity_type_breakdown": (
            None
            if support.opportunity_type_breakdown is None
            else opportunity_type_breakdown_payload(support.opportunity_type_breakdown)
        ),
        "listing_quality_breakdown": (
            None
            if support.listing_quality_breakdown is None
            else listing_quality_breakdown_payload(support.listing_quality_breakdown)
        ),
        "unknown_location_diagnostics": (
            None
            if support.unknown_location_diagnostics is None
            else unknown_location_diagnostics_payload(
                support.unknown_location_diagnostics
            )
        ),
        "freshness_availability_witness": (
            None
            if support.freshness_availability_witness is None
            else freshness_availability_witness_payload(
                support.freshness_availability_witness
            )
        ),
    }


@dataclass(frozen=True)
class BusinessMetricResult:
    """One business / data-quality answer, and everything that identifies it.

    The three fingerprints are three different questions, never interchangeable,
    and all three are **per metric**: `scope_fingerprint` (which question, with
    only the bindings this metric uses), `evidence_fingerprint` (which input
    artefacts, and no output of any computation), `result_fingerprint` (the
    answer, referencing the other two).

    The invariants enforced here:

    * `COMPUTED` carries a value, a numerator, a positive denominator **and a
      universe size**, and no reason;
    * `N_A` carries a reason **this metric is entitled to state** and no value,
      no fraction, and no structured block;
    * a freshness distribution appears only on
      `FRESHNESS_DISTRIBUTION_HOST_METRIC`, with a universe equal to the
      result's;
    * a target verdict breakdown appears only on `TARGET_VERDICT_HOST_METRIC`,
      sums to the universe, and agrees with the numerator;
    * `TARGET_COUNTRY_MATCH_RATE` has `denominator == universe_size`;
    * the key's metric and the stated universe are the pair the contract fixes,
      and the evidence class is the metric's own.

    What is *not* here, because it needs the evidence: whether the universe size
    is the real size of that universe, whether the reason's condition actually
    holds, and whether a URL audit is complete. Those are `bindings.py`'s, and
    they run on every build and every verification.
    """

    key: BusinessMetricKey
    status: BusinessMetricStatus
    universe_kind: BusinessMetricUniverse
    evidence_class: BusinessEvidenceClass
    support: BusinessMetricSupport
    scope_fingerprint: str
    evidence_fingerprint: str
    result_fingerprint: str
    contract_version: str
    result_schema_version: str = BUSINESS_METRIC_RESULT_SCHEMA_VERSION
    value: float | None = None
    reason: BusinessMetricUnavailableReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, BusinessMetricKey):
            raise BusinessMetricContractError(
                f"{self.key!r} is not a business metric key"
            )
        if not isinstance(self.status, BusinessMetricStatus):
            raise BusinessMetricContractError(
                f"{self.status!r} is not a business metric status"
            )
        require_metric_universe(self.key.metric, self.universe_kind)
        definition = metric_definition(self.key.metric)
        if self.evidence_class is not definition.evidence_class:
            raise BusinessMetricContractError(
                f"{self.key.metric} claims evidence class {self.evidence_class!r} "
                f"and the contract defines it as {definition.evidence_class}; an "
                "evidence class is a property of the metric, not of the report "
                "it appears in"
            )
        if not isinstance(self.support, BusinessMetricSupport):
            raise BusinessMetricContractError(
                f"{self.support!r} is not a business metric support block"
            )
        self._check_support_block_hosts()
        self._check_status_invariants()
        self._check_structured_support()
        validate_fingerprint(
            self.scope_fingerprint, subject="the result's scope fingerprint"
        )
        validate_fingerprint(
            self.evidence_fingerprint, subject="the result's evidence fingerprint"
        )

    # -- helpers, called only from __post_init__ ---------------------------

    def _check_status_invariants(self) -> None:
        if self.status is BusinessMetricStatus.COMPUTED:
            if self.key.metric in ALWAYS_UNAVAILABLE_METRICS:
                raise BusinessMetricContractError(
                    f"{self.key.metric} cannot be COMPUTED under "
                    f"{EVALUATION_DATASET_SCHEMA_VERSION}: the input it would "
                    f"count is not frozen in the dataset, so it is "
                    f"{BusinessMetricStatus.N_A} / "
                    f"{ALWAYS_UNAVAILABLE_METRICS[self.key.metric]} until it is"
                )
            if self.value is None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED but states no value"
                )
            validate_finite_number(
                self.value, subject=f"the value of {self.key.metric}"
            )
            if self.reason is not None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED and states an N_A reason "
                    f"({self.reason})"
                )
            if self.support.numerator is None or self.support.denominator is None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED without the fraction behind "
                    "it; a number a reader cannot check is not evidence"
                )
            if self.support.denominator == 0:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED over an empty denominator; a "
                    "rate with nothing under the line is N_A with a reason, not "
                    "a number"
                )
            if self.support.universe_size is None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED without a universe size; a "
                    "rate whose universe is unstated cannot be read, compared or "
                    "re-derived"
                )
            required = REQUIRED_SUPPORT_BLOCKS.get(self.key.metric)
            if required is not None and getattr(self.support, required) is None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED without its {required}; this "
                    "contract requires the partition a rate is a fraction of, so "
                    "that the number can be checked against its own definition "
                    "rather than believed"
                )
            if self.support.freshness_availability_witness is not None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is COMPUTED and carries a freshness "
                    "availability witness; that witness exists only to justify a "
                    "refusal"
                )
            return
        if self.value is not None:
            raise BusinessMetricContractError(
                f"{self.key.metric} is N_A and states value {self.value!r}; an "
                "unavailable metric has no number, not even a zero"
            )
        if self.reason is None:
            raise BusinessMetricContractError(
                f"{self.key.metric} is N_A without a stated reason code"
            )
        require_permitted_reason(self.key.metric, self.reason)
        if self.support.numerator is not None or (
            self.support.denominator is not None
        ):
            raise BusinessMetricContractError(
                f"{self.key.metric} is N_A and states a fraction "
                f"({self.support.numerator!r}/{self.support.denominator!r}); an "
                "unavailable metric has no fraction behind it"
            )
        for attribute in COMPUTED_ONLY_SUPPORT_BLOCKS:
            if getattr(self.support, attribute) is not None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is N_A and carries a {attribute}; that "
                    "block is a computation over the universe, and an unavailable "
                    "metric was not allowed to make one"
                )
        self._check_freshness_witness()

    def _check_freshness_witness(self) -> None:
        """`NO_VALID_PUBLICATION_DATES` is the one refusal that carries evidence."""
        witness = self.support.freshness_availability_witness
        wants_witness = (
            self.reason is BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES
        )
        if wants_witness:
            if witness is None:
                raise BusinessMetricContractError(
                    f"{self.key.metric} states "
                    f"{BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES} "
                    "and offers no witness; this is the one refusal no binding can "
                    "verify, so the contract requires the count it asserts"
                )
            if witness.known_date_count != 0:
                raise BusinessMetricBindingError(
                    f"{self.key.metric} states "
                    f"{BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES} "
                    f"and witnesses {witness.known_date_count} usable publication "
                    "date(s); the refusal asserts that there are none"
                )
            return
        if witness is not None:
            raise BusinessMetricContractError(
                f"{self.key.metric} states {self.reason} and carries a freshness "
                "availability witness, which justifies a different refusal"
            )

    def _check_support_block_hosts(self) -> None:
        """A partition sits on the one metric it answers, whatever the status.

        This runs before every other support rule. A block on a metric that is
        not its host is a structural error, not a consequence of the status the
        result happens to carry, and saying so first keeps the diagnosis of a
        mis-assembled support block from depending on which check fired.
        """
        for attribute, hosts in SUPPORT_BLOCK_HOSTS.items():
            block = getattr(self.support, attribute)
            if block is not None and self.key.metric not in hosts:
                raise BusinessMetricContractError(
                    f"a {attribute} belongs to "
                    f"{sorted(str(item) for item in hosts)} and this result is "
                    f"{self.key.metric}; a partition is the canonical answer to "
                    "one question, not a diagnostic that rides along on whatever "
                    "number was computed nearby"
                )

    def _check_structured_support(self) -> None:
        """Each host's own arithmetic relation to the number beside it.

        These relations are not computations: they are equalities between values
        the caller has already stated, and checking them is how a mis-assembled
        support block is caught before it becomes a published rate.
        """
        if self.status is not BusinessMetricStatus.COMPUTED:
            return
        universe = self.support.universe_size
        numerator = self.support.numerator

        distribution = self.support.freshness_distribution
        if distribution is not None:
            self._require_total(
                distribution.universe_size, universe, "freshness distribution"
            )
            self._require_numerator(
                distribution.known_date_count, numerator, "known publication dates"
            )
            if self.support.denominator != universe:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is a coverage over the whole cohort, so "
                    f"its denominator is {universe!r}, not "
                    f"{self.support.denominator!r}"
                )

        breakdown = self.support.target_verdict_breakdown
        if breakdown is not None:
            self._require_total(breakdown.total, universe, "target verdicts")
            self._require_numerator(breakdown.match_count, numerator, "target matches")
            if self.support.denominator != universe:
                raise BusinessMetricContractError(
                    f"{self.key.metric} is defined as match_count / cohort_size, "
                    f"so its denominator is the whole universe ({universe!r}), not "
                    f"{self.support.denominator!r}; a denominator of 'records "
                    "whose country is known' would delete the pipeline's own "
                    "uncertainty from the measurement"
                )

        data_ai = self.support.data_ai_breakdown
        if data_ai is not None:
            self._require_total(data_ai.total, universe, "Data/AI qualifications")
            if self.key.metric is BusinessMetricName.DATA_AI_RATE:
                self._require_numerator(
                    data_ai.data_ai_count, numerator, "core plus adjacent Data/AI"
                )
            elif self.key.metric is BusinessMetricName.CORE_DATA_AI_RATE:
                self._require_numerator(
                    data_ai.core_target_count, numerator, "core Data/AI"
                )
            elif self.key.metric is BusinessMetricName.CLASSIFICATION_COVERAGE_RATE:
                self._require_numerator(
                    data_ai.classified_count, numerator, "classified records"
                )

        types = self.support.opportunity_type_breakdown
        if types is not None:
            self._require_total(types.total, universe, "opportunity types")
            if self.key.metric is BusinessMetricName.PFE_OR_INTERNSHIP_RATE:
                self._require_numerator(
                    types.pfe_or_internship_count,
                    numerator,
                    "PFE plus internship postings (apprenticeships excluded)",
                )
            else:
                self._require_numerator(
                    types.typed_count, numerator, "typed records"
                )

        quality = self.support.listing_quality_breakdown
        if quality is not None:
            self._require_total(quality.total, universe, "listing qualities")
            self._require_numerator(
                quality.normal_listing_count, numerator, "normal listings"
            )

        locations = self.support.unknown_location_diagnostics
        if locations is not None:
            for name, count in (
                ("ambiguous_location_count", locations.ambiguous_location_count),
                (
                    "no_geography_segments_count",
                    locations.no_geography_segments_count,
                ),
                (
                    "unknown_segment_record_count",
                    locations.unknown_segment_record_count,
                ),
            ):
                if universe is not None and count > universe:
                    raise BusinessMetricBindingError(
                        f"the {name} is {count} in a cohort of {universe}"
                    )
            self._require_numerator(
                locations.unknown_location_count,
                numerator,
                "records with no segment or an UNKNOWN one",
            )

    def _require_total(self, total: int, universe: int | None, subject: str) -> None:
        if total != universe:
            raise BusinessMetricBindingError(
                f"the {subject} count {total} record(s) and {self.key.metric} "
                f"states a universe of {universe!r}; a partition that does not "
                "cover its universe has lost or double-counted a record"
            )

    def _require_numerator(
        self, expected: int, numerator: float | None, subject: str
    ) -> None:
        if numerator != expected:
            raise BusinessMetricBindingError(
                f"{self.key.metric} states numerator {numerator!r} and its support "
                f"counts {expected} {subject}; the two are the same number or the "
                "support is not the support of this rate"
            )


def business_metric_result_payload(
    result: BusinessMetricResult, *, include_result_fingerprint: bool = True
) -> dict[str, Any]:
    """The result as a structure: the semantic block, and optionally its digest."""
    payload: dict[str, Any] = {
        "result_schema_version": result.result_schema_version,
        "contract_version": result.contract_version,
        "key": business_metric_key_payload(result.key),
        "status": str(result.status),
        "value": result.value,
        "support": business_metric_support_payload(result.support),
        "universe_kind": str(result.universe_kind),
        "reason": None if result.reason is None else str(result.reason),
        "evidence_class": str(result.evidence_class),
        "scope_fingerprint": result.scope_fingerprint,
        "evidence_fingerprint": result.evidence_fingerprint,
    }
    if include_result_fingerprint:
        payload["result_fingerprint"] = result.result_fingerprint
    return payload


def validate_business_metric_result_structure(
    result: Any,
) -> BusinessMetricResult:
    """Re-establish every invariant a result claims, or refuse it."""
    if not isinstance(result, BusinessMetricResult):
        raise BusinessMetricContractError(
            f"{result!r} is not a business metric result"
        )
    if result.result_schema_version != BUSINESS_METRIC_RESULT_SCHEMA_VERSION:
        raise BusinessMetricContractError(
            f"unsupported business metric result schema version: "
            f"{result.result_schema_version!r} (this build reads "
            f"{BUSINESS_METRIC_RESULT_SCHEMA_VERSION!r})"
        )
    require_supported_business_metric_contract_version(result.contract_version)
    validate_fingerprint(
        result.result_fingerprint, subject="the business metric result fingerprint"
    )
    return result


# --------------------------------------------------------------------------
# the frozen cohort binding — the facts every later check needs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenCohortBinding:
    """The Phase 10.1 snapshot, as the *facts* a business metric is checked against.

    A dataset id and a content fingerprint say *which* snapshot a number is about
    and nothing about what is in it. This binding closes that by deriving
    everything later checks need, once, from records and a manifest that
    `bindings.build_frozen_cohort_binding` has **re-verified against Phase 10.1's
    own digest domain** — not from a `content_fingerprint` the manifest declares
    about itself:

    * `cohort_size` — the number of frozen records;
    * `profile_id` and `profile_fingerprint` — from the manifest's
      `profile_context`, so the identity of the person a snapshot was built for
      is a property of the snapshot and not an argument a caller supplies;
    * `dataset_generated_at` and `freshness_as_of_date` — the manifest's own
      timestamp and its calendar date, the only as-of date this contract accepts;
    * `expected_action_urls` — the complete, deduplicated universe of frozen
      action URLs the cohort implies, each held at its **first occurrence** in
      the snapshot's canonical record order. That order is a fact about this
      cohort; a lexical order over the same strings would be identical for a
      shuffled snapshot and so identifies nothing;
    * `absorbed_duplicate_total`, `records_with_absorbed_duplicates` and
      `declared_merged_duplicate_count` — what the records say about
      deduplication and what the manifest says, already held against each other.

    **The two temporal values are outside `binding_fingerprint`**, and that
    exclusion is the point of separating them from the rest. `generated_at` is
    administrative for every metric that is not about age: re-freezing the same
    database an hour later produces the same cohort, the same records and the
    same classification rates, and a `DATA_AI_RATE` whose identity moved with the
    clock would be unreproducible for no reason. The as-of date is derived from
    it, so it leaves with it. Freshness does not lose the binding — quite the
    opposite: `FreshnessBinding` states `as_of_date` in *its* digest, so the two
    freshness metrics move when it moves and nothing else does.
    """

    binding_version: str
    dataset_id: str
    dataset_fingerprint: str
    cohort_size: int
    profile_id: int
    profile_fingerprint: str
    dataset_generated_at: str
    freshness_as_of_date: str
    #: Unique, in the snapshot's own order, and not sorted: see the class
    #: docstring. Uniqueness is re-established by the structure validator, which
    #: cannot re-establish the order without the records — that derivation lives
    #: in `expected_frozen_action_urls` and is checked there.
    expected_action_urls: tuple[str, ...]
    absorbed_duplicate_total: int
    records_with_absorbed_duplicates: int
    declared_merged_duplicate_count: int
    binding_fingerprint: str

    @property
    def expected_audited_universe_size(self) -> int:
        """How many observations a complete URL audit of this cohort holds."""
        return len(self.expected_action_urls)

    @property
    def represented_universe_size(self) -> int:
        """S + D: the postings the collector saw before merging them."""
        return self.cohort_size + self.absorbed_duplicate_total


def frozen_cohort_binding_payload(
    binding: FrozenCohortBinding,
) -> dict[str, Any]:
    """The one canonical shape of a frozen cohort binding.

    The URL set is digested in full rather than by count: two cohorts of the same
    size implying different URLs are different cohorts, and an audit is verified
    against the set rather than against its length.

    **`dataset_generated_at` and `freshness_as_of_date` are deliberately absent.**
    They are carried on the object, used to derive and validate the freshness
    binding, and kept out of this digest so that re-freezing an unchanged
    database does not move the identity of every metric in the run. Only
    `FreshnessBinding` digests the as-of date, so only the freshness metrics
    depend on it.
    """
    return {
        "binding_version": binding.binding_version,
        "dataset_id": binding.dataset_id,
        "dataset_fingerprint": binding.dataset_fingerprint,
        "cohort_size": binding.cohort_size,
        "profile_id": binding.profile_id,
        "profile_fingerprint": binding.profile_fingerprint,
        "expected_action_urls": list(binding.expected_action_urls),
        "absorbed_duplicate_total": binding.absorbed_duplicate_total,
        "records_with_absorbed_duplicates": binding.records_with_absorbed_duplicates,
        "declared_merged_duplicate_count": binding.declared_merged_duplicate_count,
    }


def validate_frozen_cohort_binding_structure(binding: Any) -> FrozenCohortBinding:
    """Re-establish every invariant a frozen cohort binding claims.

    Including the derivations, which are re-checked rather than trusted: the
    as-of date must still be the calendar date of the stated `generated_at`, and
    the URL set must still be unique and canonically ordered. An artefact
    somebody rebuilt with a hand-chosen as-of date is refused here.
    """
    if not isinstance(binding, FrozenCohortBinding):
        raise BusinessMetricBindingError(
            f"{binding!r} is not a frozen cohort binding"
        )
    if binding.binding_version != FROZEN_COHORT_BINDING_VERSION:
        raise BusinessMetricContractError(
            f"unsupported frozen cohort binding version: "
            f"{binding.binding_version!r} (this build reads "
            f"{FROZEN_COHORT_BINDING_VERSION!r})"
        )
    validate_text(binding.dataset_id, subject="the cohort binding dataset id")
    validate_fingerprint(
        binding.dataset_fingerprint,
        subject="the cohort binding dataset fingerprint",
    )
    validate_count(binding.cohort_size, subject="the frozen cohort size", minimum=1)
    validate_profile_id(binding.profile_id, subject="the cohort binding profile id")
    validate_fingerprint(
        binding.profile_fingerprint,
        subject="the cohort binding profile context fingerprint",
    )
    derived = calendar_date_of(
        binding.dataset_generated_at, subject="the dataset generated_at"
    )
    validate_iso_date(
        binding.freshness_as_of_date, subject="the freshness as-of date"
    )
    if binding.freshness_as_of_date != derived:
        raise BusinessMetricBindingError(
            f"the cohort binding states as-of date {binding.freshness_as_of_date} "
            f"and its snapshot was generated on {derived}; the as-of date is the "
            "date of the snapshot and is never chosen"
        )
    if not isinstance(binding.expected_action_urls, tuple):
        raise BusinessMetricBindingError(
            "the cohort binding's expected action URLs are not an immutable "
            "sequence"
        )
    seen: set[str] = set()
    for url in binding.expected_action_urls:
        validate_text(url, subject="an expected frozen action URL")
        if url in seen:
            raise BusinessMetricBindingError(
                f"the cohort binding lists {url!r} twice; the audited universe is "
                "a set of unique URLs"
            )
        seen.add(url)
    validate_count(
        binding.absorbed_duplicate_total, subject="the absorbed duplicate total"
    )
    validate_count(
        binding.records_with_absorbed_duplicates,
        subject="the number of records with absorbed duplicates",
    )
    if binding.records_with_absorbed_duplicates > binding.cohort_size:
        raise BusinessMetricBindingError(
            f"{binding.records_with_absorbed_duplicates} records absorbed a "
            f"duplicate in a cohort of {binding.cohort_size}"
        )
    if binding.records_with_absorbed_duplicates > binding.absorbed_duplicate_total:
        raise BusinessMetricBindingError(
            f"{binding.records_with_absorbed_duplicates} records absorbed at "
            f"least one duplicate, which cannot exceed the "
            f"{binding.absorbed_duplicate_total} duplicates absorbed in total"
        )
    if len(binding.expected_action_urls) > binding.cohort_size:
        raise BusinessMetricBindingError(
            f"the cohort binding implies {len(binding.expected_action_urls)} "
            f"unique action URLs from {binding.cohort_size} records; each record "
            "contributes at most one"
        )
    validate_count(
        binding.declared_merged_duplicate_count,
        subject=f"the manifest's {MERGED_DUPLICATE_EXCLUSION_KEY!r} exclusion count",
    )
    if binding.absorbed_duplicate_total != binding.declared_merged_duplicate_count:
        raise BusinessMetricBindingError(
            f"the frozen records absorbed {binding.absorbed_duplicate_total} "
            f"duplicates and the manifest declares "
            f"{binding.declared_merged_duplicate_count} "
            f"{MERGED_DUPLICATE_EXCLUSION_KEY!r} exclusions; refusing a cohort "
            "whose two independent statements about deduplication disagree — this "
            "is an integrity failure and not an unavailable metric"
        )
    validate_fingerprint(
        binding.binding_fingerprint, subject="the frozen cohort binding fingerprint"
    )
    return binding


# --------------------------------------------------------------------------
# the profile target binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileTargetBindingEvidence:
    """Which country this person restricted themselves to, bound to a snapshot.

    Phase 10.1's manifest records the *fingerprint* of the profile context and
    deliberately does not republish the payload — no skills, no experiences, and
    no `geographic_target`. So a dataset knows which profile state it was built
    for and cannot say what that state's target country was, which is precisely
    the value `TARGET_COUNTRY_MATCH_RATE` needs.

    This evidence closes that gap with the smallest possible statement: whose
    profile, which profile state (by digest), which country the production
    resolver derived, and under which of its rules. No skills, no mobility list,
    no name, no contact detail.

    `country_code = None` is **coherent evidence of an UNKNOWN target**, not a
    missing binding, and it is what licenses `N_A / TARGET_COUNTRY_UNKNOWN`
    while making `COMPUTED` impossible.
    """

    binding_version: str
    profile_id: int
    profile_fingerprint: str
    country_code: str | None
    rule_id: str
    binding_fingerprint: str


def profile_target_binding_payload(
    binding: ProfileTargetBindingEvidence,
) -> dict[str, Any]:
    """The one canonical shape of a profile target binding."""
    return {
        "binding_version": binding.binding_version,
        "profile_id": binding.profile_id,
        "profile_fingerprint": binding.profile_fingerprint,
        "country_code": binding.country_code,
        "rule_id": binding.rule_id,
    }


def validate_profile_target_binding_structure(
    binding: Any,
) -> ProfileTargetBindingEvidence:
    """Everything about a target binding that needs no production vocabulary.

    Whether `rule_id` is one of the production resolver's rules, and whether the
    country/rule pair agrees with that resolver's contract, need
    `services.geography`; this module imports no production package, so
    `bindings.py` applies them in one function its builder and verifier share.
    """
    if not isinstance(binding, ProfileTargetBindingEvidence):
        raise BusinessMetricBindingError(
            f"{binding!r} is not a profile target binding"
        )
    if binding.binding_version != PROFILE_TARGET_BINDING_VERSION:
        raise BusinessMetricContractError(
            f"unsupported profile target binding version: "
            f"{binding.binding_version!r} (this build reads "
            f"{PROFILE_TARGET_BINDING_VERSION!r})"
        )
    validate_profile_id(binding.profile_id, subject="the bound profile id")
    validate_fingerprint(
        binding.profile_fingerprint, subject="the bound profile context fingerprint"
    )
    if binding.country_code is not None:
        validate_country_code(
            binding.country_code, subject="the bound target country code"
        )
    validate_text(binding.rule_id, subject="the target resolution rule id")
    validate_fingerprint(
        binding.binding_fingerprint, subject="the profile target binding fingerprint"
    )
    return binding


# --------------------------------------------------------------------------
# the declared source universe
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredSourceEntry:
    """One entry of a declared source universe, reduced to what a metric needs.

    A **projection for the calculation**, not the map's identity. The Phase 7C
    file carries names, homepages, statuses about verification, live-canary flags
    and reviewer notes; a coverage rate reads none of those, so they are not
    here. They are still *bound*, through `source_map_fingerprint` — see
    `DeclaredSourceUniverseEvidence`.

    The vocabularies — which integration statuses exist, which coverage roles,
    which priorities — are not redeclared here. They belong to the map's own
    validator, which owns that file's schema.
    """

    source_id: str
    integration_status: str
    coverage_role: str
    priority: str
    country: str
    production_source_id: str | None = None


def declared_source_entry_payload(entry: DeclaredSourceEntry) -> dict[str, Any]:
    """The one canonical shape of a declared source entry."""
    return {
        "source_id": entry.source_id,
        "integration_status": entry.integration_status,
        "coverage_role": entry.coverage_role,
        "priority": entry.priority,
        "country": entry.country,
        "production_source_id": entry.production_source_id,
    }


@dataclass(frozen=True)
class DeclaredSourceUniverseEvidence:
    """A declared, versioned source map, frozen by its **exact** identity.

    Two digests, and the difference between them is the finding this version
    exists for:

    * the typed `entries` above are the projection a coverage rate computes over;
    * `source_map_fingerprint` is the digest of the **whole validated map** —
      every field of every entry, plus the map's own header. The Phase 7C file
      says `version: v1` and has been amended since it was written, by the phases
      that actually visited some of its entries. A binding that digested only the
      six projected fields would call an edit to a `notes`, a `homepage_url` or a
      `homepage_url_status` the *same universe*, and a recall number computed
      before and after such an edit would be filed under one identity.

    Both are inside `content_fingerprint`, together with `git_commit`. The commit
    is in the digest deliberately, and it is the one place this package departs
    from Phase 10.1's "provenance is not content" rule: a frozen dataset is
    immutable and identified by what it contains, whereas this map is an authored
    file that people edit in place, so the revision it was read at is part of
    what it *is*. `provenance_path` stays outside: a path is where a file was
    read, not which file it was.
    """

    binding_version: str
    map_name: str
    map_version: str
    scope_country: str
    entries: tuple[DeclaredSourceEntry, ...]
    #: SHA-256 of the canonical payload of the whole validated map. Computed by
    #: `bindings.declared_source_universe_from_source_map` from the map object
    #: itself; there is no public way to state one.
    source_map_fingerprint: str
    #: The revision the map was read at. **Required** in v1, and inside the
    #: content digest.
    git_commit: str
    content_fingerprint: str
    provenance_path: str | None = None


def declared_source_universe_payload(
    evidence: DeclaredSourceUniverseEvidence,
) -> dict[str, Any]:
    """The one canonical shape of a declared source universe: its digest domain."""
    return {
        "binding_version": evidence.binding_version,
        "map_name": evidence.map_name,
        "map_version": evidence.map_version,
        "scope_country": evidence.scope_country,
        "entries": [
            declared_source_entry_payload(entry) for entry in evidence.entries
        ],
        "source_map_fingerprint": evidence.source_map_fingerprint,
        "git_commit": evidence.git_commit,
    }


def validate_declared_source_universe_structure(
    evidence: Any,
) -> DeclaredSourceUniverseEvidence:
    """Re-establish every invariant a declared source universe claims."""
    if not isinstance(evidence, DeclaredSourceUniverseEvidence):
        raise BusinessMetricBindingError(
            f"{evidence!r} is not a declared source universe"
        )
    if evidence.binding_version != DECLARED_SOURCE_UNIVERSE_VERSION:
        raise BusinessMetricContractError(
            f"unsupported declared source universe version: "
            f"{evidence.binding_version!r} (this build reads "
            f"{DECLARED_SOURCE_UNIVERSE_VERSION!r})"
        )
    validate_text(evidence.map_name, subject="the declared source map name")
    validate_text(evidence.map_version, subject="the declared source map version")
    validate_country_code(
        evidence.scope_country, subject="the declared source map scope country"
    )
    if not isinstance(evidence.entries, tuple):
        raise BusinessMetricBindingError(
            "the declared source universe's entries are not an immutable sequence"
        )
    if not evidence.entries:
        raise BusinessMetricBindingError(
            "a declared source universe holds at least one source; an empty "
            "declaration cannot be the denominator of a coverage rate"
        )
    seen: set[str] = set()
    repeated: set[str] = set()
    for entry in evidence.entries:
        if not isinstance(entry, DeclaredSourceEntry):
            raise BusinessMetricBindingError(
                f"{entry!r} is not a declared source entry"
            )
        source_id = validate_text(entry.source_id, subject="a declared source id")
        validate_text(
            entry.integration_status,
            subject=f"the integration status of {source_id}",
        )
        validate_text(entry.coverage_role, subject=f"the coverage role of {source_id}")
        validate_text(entry.priority, subject=f"the priority of {source_id}")
        validate_country_code(entry.country, subject=f"the country of {source_id}")
        validate_optional_text(
            entry.production_source_id,
            subject=f"the production source id of {source_id}",
        )
        if source_id in seen:
            repeated.add(source_id)
        seen.add(source_id)
    if repeated:
        raise BusinessMetricBindingError(
            f"the declared source universe repeats source ids: {sorted(repeated)}; "
            "a duplicated entry would be counted twice in every denominator"
        )
    ordered = [entry.source_id for entry in evidence.entries]
    if ordered != sorted(ordered):
        raise BusinessMetricBindingError(
            "the declared source universe's entries are not in canonical "
            "source_id order; a universe is a set, and it is held in the one "
            "order two equal universes are guaranteed to agree on"
        )
    validate_fingerprint(
        evidence.source_map_fingerprint,
        subject="the exact source map fingerprint",
    )
    validate_fingerprint(
        evidence.content_fingerprint,
        subject="the declared source universe content fingerprint",
    )
    validate_git_commit(
        evidence.git_commit, subject="the declared source map git commit"
    )
    validate_optional_text(
        evidence.provenance_path, subject="the declared source map path"
    )
    return evidence


# --------------------------------------------------------------------------
# the frozen action URL: selection, and never validation
# --------------------------------------------------------------------------


def frozen_action_url(record: Mapping[str, Any]) -> str | None:
    """Which URL of a frozen record a future audit is about, or `None`.

    The frozen protocol of `FROZEN_ACTION_URL_PROTOCOL_VERSION`:

        application_url, else source_url, else canonical_url

    **Selection is not validation, and this function does only the first.**

    * a field that is *absent* — `None`, or a string that is empty or entirely
      whitespace — states nothing, so the protocol falls through to the next
      field. A presence rule, not a judgement about the URL;
    * a field that is *present* is selected, **whatever it contains**. A
      `mailto:`, a relative path, a private address: each is a real value a real
      posting carried.

    A selected value that is not fetchable is not an integrity failure, is not a
    `BROKEN` verdict and is not silently dropped. It is represented by the future
    audit as an observation that was never attempted, with the reason its *shape*
    demands — `UNSUPPORTED_SCHEME` for a `mailto:`, `INVALID_URL` for a
    `/careers/3` — and `UrlAuditObservation` enforces that pairing, so the honest
    representation is the only representable one.

    Surrounding whitespace is stripped: a URL does not contain the spaces around
    it, and keeping them would split one address into two members of a set. A
    value that is not a string at all is refused — that is a record of another
    contract, not an odd URL.
    """
    if not isinstance(record, Mapping):
        raise BusinessMetricBindingError(
            f"{record!r} is not a frozen evaluation record"
        )
    for field_name in FROZEN_ACTION_URL_PRECEDENCE:
        if field_name not in record:
            raise BusinessMetricBindingError(
                f"a frozen evaluation record states no {field_name!r}; refusing "
                "to derive an action URL from a record of another contract"
            )
        value = record[field_name]
        if value is None:
            continue
        if not isinstance(value, str):
            raise BusinessMetricBindingError(
                f"a frozen evaluation record states {field_name}={value!r} "
                f"({type(value).__name__}), which is not a URL field of the "
                "Phase 10.1 record contract"
            )
        selected = value.strip()
        if not selected:
            # Blank is absence, by the presence rule above: fall through.
            continue
        return selected
    return None


# --------------------------------------------------------------------------
# the URL audit — contract only, and no client anywhere
# --------------------------------------------------------------------------


class UrlAuditOutcome(StrEnum):
    """What one audited URL turned out to be. Three members, and the third
    matters most.

    `INCONCLUSIVE` is not a soft `BROKEN`. A timeout, a robots refusal, a 403, a
    URL the preflight would not let us request and a status nobody can interpret
    are all states in which **we do not know** whether the posting is reachable,
    and counting them as broken would let a guarded site look like a dead one.
    """

    VALID = "VALID"
    BROKEN = "BROKEN"
    INCONCLUSIVE = "INCONCLUSIVE"


class UrlAuditInconclusiveReason(StrEnum):
    """Why an observation is inconclusive, as a closed and *specific* vocabulary.

    There is deliberately **no generic "not attempted"** member. A sealed audit
    that could say "we did not try, and we are not saying why" would be an audit
    with a hole in it that reads as completeness: every URL would have an
    observation, and the artefact would still not account for what happened. Each
    member below names a decision or an event a reader can act on, and
    `UrlAuditObservation` additionally requires the reason to fit the URL's own
    shape — a `mailto:` cannot be `INVALID_URL`, and an ordinary https URL can be
    neither.
    """

    # -- decided before any request was made -----------------------------
    #: The selected action URL is not an absolute URL at all — a relative path, a
    #: fragment, a bare word. Nothing could have been requested.
    INVALID_URL = "INVALID_URL"

    #: An absolute URL in a scheme the audit does not speak — `mailto:`, `ftp:`.
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"

    #: The security preflight refused the destination: loopback, a private range,
    #: link-local metadata. A refusal to look, never evidence about the posting.
    #: It may also be reached at a redirect hop.
    UNSAFE_DESTINATION = "UNSAFE_DESTINATION"

    #: The site's own robots.txt withholds this path from us.
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"

    #: robots.txt itself could not be retrieved or parsed, so permission is
    #: unknown — distinct from being refused, and fails closed.
    ROBOTS_UNRESOLVED = "ROBOTS_UNRESOLVED"

    #: A crawl barrier stood between us and the page — a consent wall, a bot
    #: challenge, an interstitial. The page may be perfectly alive behind it.
    ROBOTS_BARRIER = "ROBOTS_BARRIER"

    # -- attempted, and no final status was reached -----------------------
    TIMEOUT = "TIMEOUT"
    DNS_ERROR = "DNS_ERROR"
    TLS_ERROR = "TLS_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"

    #: The redirect chain exceeded `URL_AUDIT_MAX_REDIRECTS`.
    REDIRECT_OVERFLOW = "REDIRECT_OVERFLOW"

    #: A redirect pointed somewhere unusable — a missing or malformed
    #: `Location`, a loop, a scheme the audit does not follow.
    REDIRECT_INVALID = "REDIRECT_INVALID"

    # -- attempted, and a final status arrived that decides nothing --------
    #: 401, 403, 407 — we were refused, and refusal is not absence.
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"

    #: 429 — we asked too often, which says nothing about the posting.
    RATE_LIMITED = "RATE_LIMITED"

    #: Any other 4xx that is not 404 or 410.
    CLIENT_ERROR = "CLIENT_ERROR"

    #: 5xx — the server failed, which is a fact about the server today.
    SERVER_ERROR = "SERVER_ERROR"

    #: The exchange ended on a 3xx, so the chain was never resolved.
    REDIRECT_NOT_RESOLVED = "REDIRECT_NOT_RESOLVED"

    #: A 1xx as a final status: nothing about the resource was established.
    AMBIGUOUS_STATUS = "AMBIGUOUS_STATUS"


#: Reasons that describe a decision taken **before** requesting. An observation
#: carrying one of these has `attempted=False` and no response fields.
UNATTEMPTED_REASONS: frozenset[UrlAuditInconclusiveReason] = frozenset(
    {
        UrlAuditInconclusiveReason.INVALID_URL,
        UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME,
        UrlAuditInconclusiveReason.UNSAFE_DESTINATION,
        UrlAuditInconclusiveReason.ROBOTS_DISALLOWED,
        UrlAuditInconclusiveReason.ROBOTS_UNRESOLVED,
        UrlAuditInconclusiveReason.ROBOTS_BARRIER,
    }
)

#: Reasons available to an attempted observation that reached no final status.
#: `UNSAFE_DESTINATION` and `ROBOTS_BARRIER` appear here **and** above: an unsafe
#: destination can be recognised from the URL before requesting or discovered at
#: a redirect hop, and a barrier can be anticipated or met.
NO_RESPONSE_REASONS: frozenset[UrlAuditInconclusiveReason] = frozenset(
    {
        UrlAuditInconclusiveReason.TIMEOUT,
        UrlAuditInconclusiveReason.DNS_ERROR,
        UrlAuditInconclusiveReason.TLS_ERROR,
        UrlAuditInconclusiveReason.NETWORK_ERROR,
        UrlAuditInconclusiveReason.REDIRECT_OVERFLOW,
        UrlAuditInconclusiveReason.REDIRECT_INVALID,
        UrlAuditInconclusiveReason.UNSAFE_DESTINATION,
        UrlAuditInconclusiveReason.ROBOTS_BARRIER,
    }
)

#: The only reason an absolute URL in an unsupported scheme may carry.
NON_HTTP_SCHEME_URL_REASONS: frozenset[UrlAuditInconclusiveReason] = frozenset(
    {UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME}
)

#: The only reason a string that is not an absolute URL may carry.
NO_SCHEME_URL_REASONS: frozenset[UrlAuditInconclusiveReason] = frozenset(
    {UrlAuditInconclusiveReason.INVALID_URL}
)

#: The reasons an ordinary http(s) URL may **not** carry: it parsed, and it has
#: a scheme this audit speaks, so neither of these can be true of it.
HTTP_URL_FORBIDDEN_REASONS: frozenset[UrlAuditInconclusiveReason] = frozenset(
    {
        UrlAuditInconclusiveReason.INVALID_URL,
        UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME,
    }
)

#: The frozen v1 verdict table, as a set rather than as prose. Only 404 and 410
#: are `BROKEN`, and that narrowness is the point: those two are the codes that
#: *mean* the resource is gone. A 403 means we were refused, a 500 means the
#: server broke today, a 429 means we asked too often — reading any of them as
#: "this posting is dead" would report our own access problems as the pipeline's
#: data quality.
BROKEN_STATUS_CODES: frozenset[int] = frozenset({404, 410})


def outcome_for_status(status_code: Any) -> UrlAuditOutcome:
    """The outcome a final status implies under `URL_AUDIT_POLICY_VERSION`.

    A pure function of one integer, and what makes the verdict table enforceable
    instead of aspirational: `UrlAuditObservation` compares an observation's own
    outcome against this, so a `VALID` on a 404 cannot be constructed at all.
    """
    code = validate_status_code(status_code, subject="an audited status code")
    if 200 <= code <= 299:
        return UrlAuditOutcome.VALID
    if code in BROKEN_STATUS_CODES:
        return UrlAuditOutcome.BROKEN
    return UrlAuditOutcome.INCONCLUSIVE


def _required_status_reason(code: int) -> UrlAuditInconclusiveReason:
    if code in (401, 403, 407):
        return UrlAuditInconclusiveReason.ACCESS_RESTRICTED
    if code == 429:
        return UrlAuditInconclusiveReason.RATE_LIMITED
    if 400 <= code <= 499:
        return UrlAuditInconclusiveReason.CLIENT_ERROR
    if 500 <= code <= 599:
        return UrlAuditInconclusiveReason.SERVER_ERROR
    if 300 <= code <= 399:
        return UrlAuditInconclusiveReason.REDIRECT_NOT_RESOLVED
    return UrlAuditInconclusiveReason.AMBIGUOUS_STATUS


#: The reason an inconclusive observation **must** carry for a given final
#: status, as a mapping a test can read and assert on.
INCONCLUSIVE_STATUS_REASONS: Mapping[int, UrlAuditInconclusiveReason] = {
    code: _required_status_reason(code)
    for code in range(100, 600)
    if outcome_for_status(code) is UrlAuditOutcome.INCONCLUSIVE
}


@dataclass(frozen=True)
class UrlAuditObservation:
    """What a future audit recorded about one URL. **No client lives here.**

    Phase 10.4a defines this shape and performs no request of any kind.

    The invariants encode `URL_AUDIT_POLICY_VERSION` so that a semantically false
    artefact cannot be constructed:

    * **the verdict follows the status.** If a final status is stated, the
      outcome must be the one `outcome_for_status` derives from it, and an
      inconclusive verdict must carry the reason `INCONCLUSIVE_STATUS_REASONS`
      fixes. `VALID + 404` and `BROKEN + 200` are unrepresentable;
    * **a conclusion needs a response.** `VALID` and `BROKEN` require an attempt
      and a status code;
    * **an attempt needs a timestamp.** Every attempted observation carries an
      RFC 3339 `audited_at` with an explicit offset;
    * **an unattempted URL is still an observation**, and it says *specifically*
      why — a reason from `UNATTEMPTED_REASONS`, never a generic "not attempted";
    * **the reason fits the URL's shape.** An ordinary https URL cannot be
      `INVALID_URL` or `UNSUPPORTED_SCHEME`; a `mailto:` must be
      `UNSUPPORTED_SCHEME`; a `/careers/3` must be `INVALID_URL`. Without this
      the two most convenient reasons would absorb everything nobody wanted to
      explain.
    """

    requested_url: str
    attempted: bool
    outcome: UrlAuditOutcome
    reason: UrlAuditInconclusiveReason | None = None
    final_url: str | None = None
    status_code: int | None = None
    redirect_chain: tuple[str, ...] = ()
    audited_at: str | None = None

    def __post_init__(self) -> None:
        validate_text(self.requested_url, subject="an audited URL")
        if not isinstance(self.attempted, bool):
            raise BusinessMetricBindingError(
                f"the `attempted` flag of {self.requested_url} is not a boolean: "
                f"{self.attempted!r}"
            )
        if not isinstance(self.outcome, UrlAuditOutcome):
            raise BusinessMetricContractError(
                f"{self.outcome!r} is not a URL audit outcome"
            )
        if self.reason is not None and not isinstance(
            self.reason, UrlAuditInconclusiveReason
        ):
            raise BusinessMetricContractError(
                f"{self.reason!r} is not a URL audit inconclusive reason"
            )
        self._validate_response_fields()
        self._validate_reason_presence()
        if not self.attempted:
            self._validate_unattempted()
        else:
            self._validate_attempted()
        self._validate_reason_against_url_shape()

    # -- helpers, called only from __post_init__ ---------------------------

    def _validate_response_fields(self) -> None:
        if not isinstance(self.redirect_chain, tuple):
            raise BusinessMetricBindingError(
                f"the redirect chain of {self.requested_url} is not an immutable "
                "sequence"
            )
        if len(self.redirect_chain) > URL_AUDIT_MAX_REDIRECTS:
            raise BusinessMetricContractError(
                f"{self.requested_url} records {len(self.redirect_chain)} "
                f"redirect hops and {URL_AUDIT_POLICY_VERSION} follows at most "
                f"{URL_AUDIT_MAX_REDIRECTS}; a longer chain is "
                f"{UrlAuditInconclusiveReason.REDIRECT_OVERFLOW}"
            )
        for hop in self.redirect_chain:
            validate_http_url(hop, subject=f"a redirect hop of {self.requested_url}")
        if self.final_url is not None:
            validate_http_url(
                self.final_url, subject=f"the final URL of {self.requested_url}"
            )
        if self.status_code is not None:
            validate_status_code(
                self.status_code, subject=f"the status code of {self.requested_url}"
            )
        if self.audited_at is not None:
            validate_timestamp(
                self.audited_at,
                subject=f"the audit timestamp of {self.requested_url}",
            )

    def _validate_reason_presence(self) -> None:
        if self.outcome is UrlAuditOutcome.INCONCLUSIVE:
            if self.reason is None:
                raise BusinessMetricContractError(
                    f"{self.requested_url} is INCONCLUSIVE without a stated "
                    "reason code"
                )
        elif self.reason is not None:
            raise BusinessMetricContractError(
                f"{self.requested_url} is {self.outcome} and states the "
                f"inconclusive reason {self.reason}"
            )

    def _validate_unattempted(self) -> None:
        if self.outcome is not UrlAuditOutcome.INCONCLUSIVE:
            raise BusinessMetricContractError(
                f"{self.requested_url} was never requested and claims outcome "
                f"{self.outcome}; an unattempted URL is "
                f"{UrlAuditOutcome.INCONCLUSIVE} and nothing else"
            )
        if (
            self.status_code is not None
            or self.final_url is not None
            or self.redirect_chain
        ):
            raise BusinessMetricContractError(
                f"{self.requested_url} was never requested and states response "
                "fields; there was no response"
            )
        if self.reason not in UNATTEMPTED_REASONS:
            raise BusinessMetricContractError(
                f"{self.requested_url} was never requested and states reason "
                f"{self.reason}, which describes something that can only be "
                f"learned by requesting; an unattempted URL states one of "
                f"{sorted(str(item) for item in UNATTEMPTED_REASONS)}"
            )

    def _validate_attempted(self) -> None:
        if self.audited_at is None:
            raise BusinessMetricBindingError(
                f"{self.requested_url} was requested and states no audited_at; an "
                "external observation that does not say when it was taken is not "
                "evidence, and two audits of one URL could not be told apart"
            )
        if self.status_code is None:
            if self.outcome is not UrlAuditOutcome.INCONCLUSIVE:
                raise BusinessMetricContractError(
                    f"{self.requested_url} is {self.outcome} without a status "
                    "code; a conclusion needs the response it was drawn from"
                )
            if self.reason not in NO_RESPONSE_REASONS:
                raise BusinessMetricContractError(
                    f"{self.requested_url} reached no final status and states "
                    f"reason {self.reason}, which describes a response or a "
                    "decision taken before requesting"
                )
            return
        expected_outcome = outcome_for_status(self.status_code)
        if self.outcome is not expected_outcome:
            raise BusinessMetricContractError(
                f"{self.requested_url} answered {self.status_code} and claims "
                f"{self.outcome}; {URL_AUDIT_POLICY_VERSION} reads that status as "
                f"{expected_outcome}, and only {sorted(BROKEN_STATUS_CODES)} are "
                "BROKEN — a refusal, a server failure or a rate limit is "
                "INCONCLUSIVE, never evidence that a posting is gone"
            )
        if expected_outcome is UrlAuditOutcome.INCONCLUSIVE:
            required = INCONCLUSIVE_STATUS_REASONS[self.status_code]
            if self.reason is not required:
                raise BusinessMetricContractError(
                    f"{self.requested_url} answered {self.status_code} and states "
                    f"reason {self.reason}; {URL_AUDIT_POLICY_VERSION} records "
                    f"that status as {required}"
                )

    def _validate_reason_against_url_shape(self) -> None:
        shape = url_shape(self.requested_url)
        if shape is UrlShape.HTTP:
            if self.reason in HTTP_URL_FORBIDDEN_REASONS:
                raise BusinessMetricContractError(
                    f"{self.requested_url!r} is an ordinary http(s) URL and states "
                    f"{self.reason}; that reason describes a URL this build could "
                    "not have requested, and this one could be"
                )
            return
        if self.attempted:
            raise BusinessMetricContractError(
                f"{self.requested_url!r} is not an http(s) URL and the observation "
                "claims it was requested; a URL this build cannot request is "
                "recorded as attempted=False / INCONCLUSIVE"
            )
        permitted = (
            NON_HTTP_SCHEME_URL_REASONS
            if shape is UrlShape.NON_HTTP_SCHEME
            else NO_SCHEME_URL_REASONS
        )
        if self.reason not in permitted:
            raise BusinessMetricContractError(
                f"{self.requested_url!r} is {shape} and states reason "
                f"{self.reason}; such a URL is "
                f"{sorted(str(item) for item in permitted)} — it was selected by "
                "the frozen action URL protocol and cannot be fetched, which is "
                "not the same as being broken"
            )

    @property
    def conclusive(self) -> bool:
        """Did this observation decide the question? `INCONCLUSIVE` did not."""
        return self.outcome is not UrlAuditOutcome.INCONCLUSIVE


@dataclass(frozen=True)
class UrlAuditBinding:
    """A sealed URL audit campaign, bound to the snapshot it was derived from.

    `audit_started_at` is **mandatory and inside the digest**, and that is the
    point of calling this a campaign rather than a list of observations: an audit
    in which every URL was withheld by robots or refused by the preflight has no
    per-observation timestamp anywhere, and it is still an external, temporal
    claim — "as of this moment, nothing here could be checked". An artefact that
    could not say when that was would be indistinguishable from the same refusals
    a year later.

    `audit_policy_version` must be exactly `URL_AUDIT_POLICY_VERSION`. A free
    string was accepted before, which meant an artefact could name a policy
    nobody had written down; a broken-URL rate is only comparable across two
    audits if both followed the same redirect bound, timeout and robots
    discipline.

    There is deliberately no `sealed: bool`. Completeness is not something an
    artefact can assert about itself: it is established against
    `FrozenCohortBinding.expected_action_urls`, every time a result over
    `URL_AUDITED_UNIVERSE` is built or verified.
    """

    binding_version: str
    protocol_version: str
    audit_policy_version: str
    dataset_id: str
    dataset_fingerprint: str
    audit_started_at: str
    observations: tuple[UrlAuditObservation, ...]
    declared_universe_size: int
    binding_fingerprint: str

    @property
    def requested_urls(self) -> tuple[str, ...]:
        return tuple(item.requested_url for item in self.observations)

    @property
    def conclusive_count(self) -> int:
        """How many observations decided anything. Zero is a real answer."""
        return sum(1 for item in self.observations if item.conclusive)

    @property
    def attempted_count(self) -> int:
        return sum(1 for item in self.observations if item.attempted)

    def observation(self, requested_url: str) -> UrlAuditObservation:
        for item in self.observations:
            if item.requested_url == requested_url:
                return item
        raise BusinessMetricBindingError(
            f"{requested_url!r} has no observation in this URL audit; a sealed "
            "audit holds one observation per eligible URL"
        )


def url_audit_observation_payload(
    observation: UrlAuditObservation,
) -> dict[str, Any]:
    """The one canonical shape of a single URL observation."""
    return {
        "requested_url": observation.requested_url,
        "attempted": observation.attempted,
        "outcome": str(observation.outcome),
        "reason": None if observation.reason is None else str(observation.reason),
        "final_url": observation.final_url,
        "status_code": observation.status_code,
        "redirect_chain": list(observation.redirect_chain),
        "audited_at": observation.audited_at,
    }


def url_audit_binding_payload(binding: UrlAuditBinding) -> dict[str, Any]:
    """The one canonical shape of a URL audit campaign.

    `audit_started_at` and every `audited_at` are **in** the digest, unlike every
    other timestamp in Phase 10. That is deliberate and is the difference between
    snapshot evidence and external evidence: when a URL was looked at is part of
    what was observed about it, and two audits of the same cohort a month apart
    are two facts rather than one fact recorded twice.
    """
    return {
        "binding_version": binding.binding_version,
        "protocol_version": binding.protocol_version,
        "audit_policy_version": binding.audit_policy_version,
        "dataset_id": binding.dataset_id,
        "dataset_fingerprint": binding.dataset_fingerprint,
        "audit_started_at": binding.audit_started_at,
        "observations": [
            url_audit_observation_payload(item) for item in binding.observations
        ],
        "declared_universe_size": binding.declared_universe_size,
    }


def validate_url_audit_binding_structure(binding: Any) -> UrlAuditBinding:
    """Re-establish every invariant a URL audit claims, or refuse it.

    What is establishable without the cohort: the versions, the policy, the
    campaign timestamp, one observation per URL, canonical URL order, and a
    declared size that equals the observations. Whether those URLs are *the* URLs
    the cohort implies needs the frozen cohort binding, and is
    `bindings.assert_url_audit_covers` — which every URL result now goes through.
    """
    if not isinstance(binding, UrlAuditBinding):
        raise BusinessMetricBindingError(f"{binding!r} is not a URL audit binding")
    if binding.binding_version != URL_AUDIT_BINDING_VERSION:
        raise BusinessMetricContractError(
            f"unsupported URL audit binding version: {binding.binding_version!r} "
            f"(this build reads {URL_AUDIT_BINDING_VERSION!r})"
        )
    if binding.protocol_version != FROZEN_ACTION_URL_PROTOCOL_VERSION:
        raise BusinessMetricContractError(
            f"unsupported frozen action URL protocol: {binding.protocol_version!r} "
            f"(this build reads {FROZEN_ACTION_URL_PROTOCOL_VERSION!r}); an audit "
            "that chose its URLs by another rule is auditing another universe"
        )
    if binding.audit_policy_version != URL_AUDIT_POLICY_VERSION:
        raise BusinessMetricContractError(
            f"unsupported URL audit policy: {binding.audit_policy_version!r} "
            f"(this build reads {URL_AUDIT_POLICY_VERSION!r}); an audit made "
            "under another timeout, redirect bound or robots discipline produces "
            "numbers that cannot be compared with these"
        )
    validate_text(binding.dataset_id, subject="the audited dataset id")
    validate_fingerprint(
        binding.dataset_fingerprint, subject="the audited dataset fingerprint"
    )
    validate_timestamp(
        binding.audit_started_at, subject="the URL audit campaign timestamp"
    )
    if not isinstance(binding.observations, tuple):
        raise BusinessMetricBindingError(
            "the URL audit's observations are not an immutable sequence"
        )
    if not binding.observations:
        raise BusinessMetricBindingError(
            "a URL audit holds at least one observation; an empty audit is not "
            "evidence of anything"
        )
    seen: set[str] = set()
    repeated: set[str] = set()
    for observation in binding.observations:
        if not isinstance(observation, UrlAuditObservation):
            raise BusinessMetricBindingError(
                f"{observation!r} is not a URL audit observation"
            )
        if observation.requested_url in seen:
            repeated.add(observation.requested_url)
        seen.add(observation.requested_url)
    if repeated:
        raise BusinessMetricBindingError(
            f"the URL audit holds two observations of {sorted(repeated)}; one URL "
            "is audited once, and two verdicts would be counted twice"
        )
    ordered = list(binding.requested_urls)
    if ordered != sorted(ordered):
        raise BusinessMetricBindingError(
            "the URL audit's observations are not in canonical URL order; the "
            "audited universe is a set, and it is held in the one order two equal "
            "audits are guaranteed to agree on"
        )
    validate_count(
        binding.declared_universe_size,
        subject="the declared audited universe size",
        minimum=1,
    )
    if binding.declared_universe_size != len(binding.observations):
        raise BusinessMetricBindingError(
            f"the URL audit declares a universe of {binding.declared_universe_size} "
            f"and holds {len(binding.observations)} observations"
        )
    validate_fingerprint(
        binding.binding_fingerprint, subject="the URL audit binding fingerprint"
    )
    return binding


# --------------------------------------------------------------------------
# deduplication evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupEvidence:
    """What deduplication did, entirely derived from the frozen cohort binding.

    The future `APPLIED_DUPLICATE_RATE` is

        S = cohort size
        D = sum(len(record.absorbed_duplicate_ids))
        rate = D / (S + D)

    over `PRE_DEDUP_REPRESENTED_UNIVERSE`, and the invariant that keeps it honest
    is `D == manifest.cohort.excluded_counts["merged_duplicate"]`.

    **That invariant is no longer this artefact's to establish**, and that is the
    change v4 made: it is established in `build_frozen_cohort_binding`, against
    the manifest the cohort was derived from, before any of this exists. An
    earlier version took an `excluded_counts` mapping as an argument, which meant
    a caller could produce perfectly coherent dedup evidence by passing
    `{"merged_duplicate": D}` — the two "independent" statements were then one
    statement, made twice.

    Every field here is copied from the verified binding. The artefact remains
    separate because its *presence* is what a run decides: a context without it
    licenses `DEDUP_EVIDENCE_MISSING`, and the two dedup metrics are then `N_A`
    rather than silently computed from the cohort binding everything else uses.
    """

    evidence_version: str
    dataset_id: str
    dataset_fingerprint: str
    #: S — the number of records in the frozen cohort.
    cohort_size: int
    #: D — the total number of duplicates those records absorbed.
    absorbed_duplicate_total: int
    #: What the manifest's cohort exclusion counts said, carried from the cohort
    #: binding that already held the two against each other.
    declared_merged_duplicate_count: int
    #: How many cohort records absorbed at least one duplicate — the numerator of
    #: the future `DUPLICATE_CLUSTER_RATE`, a different question from how many
    #: duplicates there were.
    records_with_absorbed_duplicates: int
    evidence_fingerprint: str

    @property
    def represented_universe_size(self) -> int:
        """S + D: the postings the collector saw before merging them."""
        return self.cohort_size + self.absorbed_duplicate_total


def dedup_evidence_payload(evidence: DedupEvidence) -> dict[str, Any]:
    """The one canonical shape of deduplication evidence."""
    return {
        "evidence_version": evidence.evidence_version,
        "dataset_id": evidence.dataset_id,
        "dataset_fingerprint": evidence.dataset_fingerprint,
        "cohort_size": evidence.cohort_size,
        "absorbed_duplicate_total": evidence.absorbed_duplicate_total,
        "declared_merged_duplicate_count": evidence.declared_merged_duplicate_count,
        "records_with_absorbed_duplicates": (
            evidence.records_with_absorbed_duplicates
        ),
    }


def validate_dedup_evidence_structure(evidence: Any) -> DedupEvidence:
    """Re-establish every dedup invariant, the manifest agreement included."""
    if not isinstance(evidence, DedupEvidence):
        raise BusinessMetricBindingError(f"{evidence!r} is not deduplication evidence")
    if evidence.evidence_version != DEDUP_EVIDENCE_VERSION:
        raise BusinessMetricContractError(
            f"unsupported dedup evidence version: {evidence.evidence_version!r} "
            f"(this build reads {DEDUP_EVIDENCE_VERSION!r})"
        )
    validate_text(evidence.dataset_id, subject="the dedup evidence dataset id")
    validate_fingerprint(
        evidence.dataset_fingerprint, subject="the dedup evidence dataset fingerprint"
    )
    validate_count(evidence.cohort_size, subject="the cohort size", minimum=1)
    validate_count(
        evidence.absorbed_duplicate_total, subject="the absorbed duplicate total"
    )
    validate_count(
        evidence.declared_merged_duplicate_count,
        subject=f"the declared {MERGED_DUPLICATE_EXCLUSION_KEY!r} exclusion count",
    )
    validate_count(
        evidence.records_with_absorbed_duplicates,
        subject="the number of records with absorbed duplicates",
    )
    if evidence.absorbed_duplicate_total != evidence.declared_merged_duplicate_count:
        raise BusinessMetricBindingError(
            f"the frozen records absorbed {evidence.absorbed_duplicate_total} "
            f"duplicates and the manifest declares "
            f"{evidence.declared_merged_duplicate_count} "
            f"{MERGED_DUPLICATE_EXCLUSION_KEY!r} exclusions; refusing evidence "
            "whose two independent statements about deduplication disagree — this "
            "is an integrity failure and not an unavailable metric"
        )
    if evidence.records_with_absorbed_duplicates > evidence.cohort_size:
        raise BusinessMetricBindingError(
            f"{evidence.records_with_absorbed_duplicates} records absorbed a "
            f"duplicate in a cohort of {evidence.cohort_size}"
        )
    if evidence.records_with_absorbed_duplicates > evidence.absorbed_duplicate_total:
        raise BusinessMetricBindingError(
            f"{evidence.records_with_absorbed_duplicates} records absorbed at "
            f"least one duplicate, which cannot exceed the "
            f"{evidence.absorbed_duplicate_total} duplicates absorbed in total"
        )
    if evidence.absorbed_duplicate_total == 0 and (
        evidence.records_with_absorbed_duplicates != 0
    ):
        raise BusinessMetricBindingError(
            "no duplicate was absorbed and "
            f"{evidence.records_with_absorbed_duplicates} records claim to have "
            "absorbed one"
        )
    validate_fingerprint(
        evidence.evidence_fingerprint, subject="the dedup evidence fingerprint"
    )
    return evidence


# --------------------------------------------------------------------------
# the freshness binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FreshnessBinding:
    """The two parameters every age statement needs, and **no choice at all**.

    `as_of_date` is not a parameter a caller supplies: it is
    `FrozenCohortBinding.freshness_as_of_date`, the calendar date of the
    snapshot's own `generated_at`, and `bindings.build_freshness_binding` takes
    the cohort binding rather than a date. Two reasons, and the second is the one
    that bites:

    * no clock. A date read from `date.today()` would make every freshness number
      unreproducible the following morning;
    * no *later* date either. An age measured from a moment after the snapshot
      was taken would count days in which the pipeline was not running, and
      `published_at > as_of_date` — the `INVALID` case — would stop being
      detectable at all.

    `policy_version` names the production policy the buckets and the score come
    from, so a number computed under one policy can never be compared with a
    number computed under another simply because both were called "freshness".

    Both `PUBLICATION_DATE_COVERAGE` and `MEAN_KNOWN_FRESHNESS_SCORE` require it,
    and they require the *same* one: classifying a date as `INVALID` needs the
    as-of date just as much as measuring an age does.
    """

    binding_version: str
    as_of_date: str
    policy_version: str
    #: The cohort binding this date was derived from. Carried so that a stored
    #: freshness binding can be held against its own origin rather than believed.
    cohort_binding_fingerprint: str
    binding_fingerprint: str


def freshness_binding_payload(binding: FreshnessBinding) -> dict[str, Any]:
    """The one canonical shape of a freshness binding."""
    return {
        "binding_version": binding.binding_version,
        "as_of_date": binding.as_of_date,
        "policy_version": binding.policy_version,
        "cohort_binding_fingerprint": binding.cohort_binding_fingerprint,
    }


def validate_freshness_binding_structure(binding: Any) -> FreshnessBinding:
    """Re-establish every invariant a freshness binding claims."""
    if not isinstance(binding, FreshnessBinding):
        raise BusinessMetricBindingError(f"{binding!r} is not a freshness binding")
    if binding.binding_version != FRESHNESS_BINDING_VERSION:
        raise BusinessMetricContractError(
            f"unsupported freshness binding version: {binding.binding_version!r} "
            f"(this build reads {FRESHNESS_BINDING_VERSION!r})"
        )
    validate_iso_date(binding.as_of_date, subject="the freshness as-of date")
    if binding.policy_version != EXPECTED_FRESHNESS_POLICY_VERSION:
        raise BusinessMetricContractError(
            f"unsupported freshness policy version: {binding.policy_version!r} "
            f"(this build reads {EXPECTED_FRESHNESS_POLICY_VERSION!r})"
        )
    validate_fingerprint(
        binding.cohort_binding_fingerprint,
        subject="the freshness binding's cohort binding fingerprint",
    )
    validate_fingerprint(
        binding.binding_fingerprint, subject="the freshness binding fingerprint"
    )
    return binding


# --------------------------------------------------------------------------
# the benchmark binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkBinding:
    """A gold benchmark, frozen as evidence, with its readiness *checkable*.

    `evaluation_ready` is the benchmark manifest's own field and is not taken on
    trust. Two things are refused:

    * readiness claimed with fewer rows than the manifest's own declared target —
      a manifest that contradicts itself is an integrity failure rather than a
      benchmark;
    * readiness claimed **without a `records_fingerprint`**. A recall is a
      fraction of a specific set of rows, so an evaluation-ready benchmark must
      be identified by a digest of those rows and not merely by a name and a
      count.

    Not being ready is an ordinary state of the world, and it is what licenses
    `DECLARED_SOURCE_DISCOVERY_RECALL -> N_A /
    SOURCE_BENCHMARK_NOT_EVALUATION_READY`. `morocco_pfe_gold_v1` records two
    rows against a target of sixty and `evaluation_ready: false`.
    """

    binding_version: str
    benchmark_name: str
    benchmark_version: str
    scope_country: str
    status: str
    record_count: int
    target_minimum_rows: int
    evaluation_ready: bool
    content_fingerprint: str
    records_fingerprint: str | None = None
    provenance_path: str | None = None


def benchmark_binding_payload(binding: BenchmarkBinding) -> dict[str, Any]:
    """The one canonical shape of a benchmark binding. Not the path."""
    return {
        "binding_version": binding.binding_version,
        "benchmark_name": binding.benchmark_name,
        "benchmark_version": binding.benchmark_version,
        "scope_country": binding.scope_country,
        "status": binding.status,
        "record_count": binding.record_count,
        "target_minimum_rows": binding.target_minimum_rows,
        "evaluation_ready": binding.evaluation_ready,
        "records_fingerprint": binding.records_fingerprint,
    }


def validate_benchmark_binding_structure(binding: Any) -> BenchmarkBinding:
    """Re-establish every invariant a benchmark binding claims."""
    if not isinstance(binding, BenchmarkBinding):
        raise BusinessMetricBindingError(f"{binding!r} is not a benchmark binding")
    if binding.binding_version != BENCHMARK_BINDING_VERSION:
        raise BusinessMetricContractError(
            f"unsupported benchmark binding version: {binding.binding_version!r} "
            f"(this build reads {BENCHMARK_BINDING_VERSION!r})"
        )
    validate_text(binding.benchmark_name, subject="the benchmark name")
    validate_text(binding.benchmark_version, subject="the benchmark version")
    validate_country_code(binding.scope_country, subject="the benchmark scope country")
    validate_text(binding.status, subject="the benchmark status")
    validate_count(binding.record_count, subject="the benchmark record count")
    validate_count(
        binding.target_minimum_rows,
        subject="the benchmark target minimum rows",
        minimum=1,
    )
    if not isinstance(binding.evaluation_ready, bool):
        raise BusinessMetricBindingError(
            f"the benchmark's evaluation_ready is not a boolean: "
            f"{binding.evaluation_ready!r}"
        )
    if binding.evaluation_ready:
        if binding.record_count < binding.target_minimum_rows:
            raise BusinessMetricBindingError(
                f"benchmark {binding.benchmark_name} claims to be evaluation-ready "
                f"with {binding.record_count} rows against its own declared target "
                f"of {binding.target_minimum_rows}; refusing a manifest that "
                "contradicts itself"
            )
        if binding.records_fingerprint is None:
            raise BusinessMetricBindingError(
                f"benchmark {binding.benchmark_name} claims to be evaluation-ready "
                "and states no records_fingerprint; a recall is a fraction of a "
                "specific set of rows, so a ready benchmark is identified by a "
                "digest of those rows and not by a name and a count"
            )
    validate_fingerprint(
        binding.content_fingerprint, subject="the benchmark content fingerprint"
    )
    if binding.records_fingerprint is not None:
        validate_fingerprint(
            binding.records_fingerprint, subject="the benchmark records fingerprint"
        )
    validate_optional_text(
        binding.provenance_path, subject="the benchmark manifest path"
    )
    return binding


# --------------------------------------------------------------------------
# the member-carrying artefacts, and their digests
# --------------------------------------------------------------------------

#: Member -> the callable that returns that artefact's own fingerprint field.
#: Stated once so that a scope, an evidence view and every cross-check read a
#: member's identity the same way.
_MEMBER_FINGERPRINT_ATTRIBUTES: Mapping[BusinessEvidenceMember, str] = {
    BusinessEvidenceMember.PROFILE_TARGET_BINDING: "binding_fingerprint",
    BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE: "content_fingerprint",
    BusinessEvidenceMember.URL_AUDIT_BINDING: "binding_fingerprint",
    BusinessEvidenceMember.DEDUP_EVIDENCE: "evidence_fingerprint",
    BusinessEvidenceMember.BENCHMARK_BINDING: "content_fingerprint",
    BusinessEvidenceMember.FRESHNESS_BINDING: "binding_fingerprint",
}

assert set(_MEMBER_FINGERPRINT_ATTRIBUTES) == set(BusinessEvidenceMember)

_MEMBER_VALIDATORS: Mapping[BusinessEvidenceMember, Any] = {
    BusinessEvidenceMember.PROFILE_TARGET_BINDING: (
        validate_profile_target_binding_structure
    ),
    BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE: (
        validate_declared_source_universe_structure
    ),
    BusinessEvidenceMember.URL_AUDIT_BINDING: validate_url_audit_binding_structure,
    BusinessEvidenceMember.DEDUP_EVIDENCE: validate_dedup_evidence_structure,
    BusinessEvidenceMember.BENCHMARK_BINDING: validate_benchmark_binding_structure,
    BusinessEvidenceMember.FRESHNESS_BINDING: validate_freshness_binding_structure,
}

assert set(_MEMBER_VALIDATORS) == set(BusinessEvidenceMember)


def member_value(holder: Any, member: BusinessEvidenceMember) -> Any:
    """The artefact a run context, scope or evidence view carries for `member`."""
    return getattr(holder, EVIDENCE_MEMBER_ATTRIBUTES[member], None)


def member_fingerprint(artefact: Any, member: BusinessEvidenceMember) -> str | None:
    """That artefact's own digest, or `None` when the member is absent."""
    if artefact is None:
        return None
    attribute = _MEMBER_FINGERPRINT_ATTRIBUTES[member]
    value = getattr(artefact, attribute, None)
    if value is None:
        raise BusinessMetricBindingError(f"the {member} artefact states no {attribute}")
    return validate_fingerprint(value, subject=f"the {member} fingerprint")


def _validate_members(holder: Any, *, subject: str) -> None:
    """Re-validate every member an artefact carries, and bind it to the cohort.

    The dataset agreement is checked against the holder's **frozen cohort
    binding**, which is mandatory on all three holders, so an artefact built over
    another snapshot cannot enter a context, a scope or an evidence view.
    """
    cohort = holder.frozen_cohort_binding
    for member in BusinessEvidenceMember:
        artefact = member_value(holder, member)
        if artefact is None:
            continue
        _MEMBER_VALIDATORS[member](artefact)
        dataset_id = getattr(artefact, "dataset_id", None)
        if dataset_id is not None and dataset_id != cohort.dataset_id:
            raise BusinessMetricBindingError(
                f"{subject}: the {member} artefact was built over dataset "
                f"{dataset_id}, not {cohort.dataset_id}"
            )
        dataset_fingerprint = getattr(artefact, "dataset_fingerprint", None)
        if (
            dataset_fingerprint is not None
            and dataset_fingerprint != cohort.dataset_fingerprint
        ):
            raise BusinessMetricBindingError(
                f"{subject}: the {member} artefact was built over content "
                f"fingerprint {dataset_fingerprint}, not "
                f"{cohort.dataset_fingerprint}"
            )
        if member is BusinessEvidenceMember.PROFILE_TARGET_BINDING:
            if artefact.profile_fingerprint != cohort.profile_fingerprint:
                raise BusinessMetricBindingError(
                    f"{subject}: the target binding is attached to profile context "
                    f"{artefact.profile_fingerprint} and this cohort was built for "
                    f"{cohort.profile_fingerprint}; refusing a target derived from "
                    "a profile state this snapshot is not about"
                )
            if artefact.profile_id != cohort.profile_id:
                raise BusinessMetricBindingError(
                    f"{subject}: the target binding is attached to profile "
                    f"{artefact.profile_id} and this cohort to {cohort.profile_id}"
                )
        if member is BusinessEvidenceMember.DEDUP_EVIDENCE:
            if artefact.cohort_size != cohort.cohort_size:
                raise BusinessMetricBindingError(
                    f"{subject}: the dedup evidence counts a cohort of "
                    f"{artefact.cohort_size} and this run's cohort holds "
                    f"{cohort.cohort_size} records"
                )
            if artefact.absorbed_duplicate_total != cohort.absorbed_duplicate_total:
                raise BusinessMetricBindingError(
                    f"{subject}: the dedup evidence counts "
                    f"{artefact.absorbed_duplicate_total} absorbed duplicates and "
                    f"this run's records hold {cohort.absorbed_duplicate_total}"
                )
        if member is BusinessEvidenceMember.FRESHNESS_BINDING:
            if artefact.as_of_date != cohort.freshness_as_of_date:
                raise BusinessMetricBindingError(
                    f"{subject}: the freshness binding states as-of date "
                    f"{artefact.as_of_date} and this run's snapshot was taken on "
                    f"{cohort.freshness_as_of_date}; the as-of date is the date of "
                    "the snapshot and is never chosen"
                )
            if artefact.cohort_binding_fingerprint != cohort.binding_fingerprint:
                raise BusinessMetricBindingError(
                    f"{subject}: the freshness binding was derived from cohort "
                    f"{artefact.cohort_binding_fingerprint} and this run is about "
                    f"{cohort.binding_fingerprint}"
                )
        if member is BusinessEvidenceMember.URL_AUDIT_BINDING:
            if artefact.declared_universe_size != (
                cohort.expected_audited_universe_size
            ):
                raise BusinessMetricBindingError(
                    f"{subject}: the URL audit declares a universe of "
                    f"{artefact.declared_universe_size} and this cohort implies "
                    f"{cohort.expected_audited_universe_size} unique action URLs"
                )


def _member_payload(holder: Any) -> dict[str, Any]:
    """Every member as its own digest, or `None`. Absence is stated, not omitted.

    `None` is explicit rather than an omitted key: "no URL audit was used" is a
    fact about this metric, and a payload that simply lacked the key would digest
    identically to one written under a contract that had never heard of URL
    audits.
    """
    payload = {
        f"{EVIDENCE_MEMBER_ATTRIBUTES[member]}_fingerprint": member_fingerprint(
            member_value(holder, member), member
        )
        for member in BusinessEvidenceMember
    }
    payload["frozen_cohort_binding_fingerprint"] = validate_fingerprint(
        holder.frozen_cohort_binding.binding_fingerprint,
        subject="the frozen cohort binding fingerprint",
    )
    return payload


# --------------------------------------------------------------------------
# the run context — every artefact a run has, with an identity of its own
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessMetricRunContext:
    """Everything one run has available, and **not** any metric's scope.

    It holds one frozen cohort binding — mandatory, because every business metric
    in this contract is a statement about one cohort and every universe check
    needs its facts — one profile binding, and whichever optional evidence the
    operator managed to assemble.

    Its identity is `run_context_fingerprint`, and the name matters: an earlier
    version of this contract digested exactly this object and called the result
    the *scope fingerprint* of every metric in the run, which made a
    `DATA_AI_RATE`'s identity move when an unrelated URL audit changed. A run
    context answers "what did this run have to work with"; it does not answer
    "which question did this number ask".
    """

    run_context_schema_version: str
    contract_version: str
    frozen_cohort_binding: FrozenCohortBinding
    run_context_fingerprint: str
    profile_target_binding: ProfileTargetBindingEvidence | None = None
    declared_source_universe: DeclaredSourceUniverseEvidence | None = None
    url_audit_binding: UrlAuditBinding | None = None
    dedup_evidence: DedupEvidence | None = None
    benchmark_binding: BenchmarkBinding | None = None
    freshness_binding: FreshnessBinding | None = None

    @property
    def dataset_id(self) -> str:
        return self.frozen_cohort_binding.dataset_id

    @property
    def dataset_fingerprint(self) -> str:
        return self.frozen_cohort_binding.dataset_fingerprint

    @property
    def profile_id(self) -> int:
        """Whose snapshot this is. **Derived**, never supplied.

        A run cannot be assembled for a profile other than the one the dataset
        was frozen for: there is no field to set and no argument to pass.
        """
        return self.frozen_cohort_binding.profile_id

    @property
    def profile_fingerprint(self) -> str:
        return self.frozen_cohort_binding.profile_fingerprint

    def has(self, member: BusinessEvidenceMember) -> bool:
        return member_value(self, member) is not None


def business_metric_run_context_payload(
    context: BusinessMetricRunContext,
) -> dict[str, Any]:
    """The one canonical shape of a run context: its digest domain.

    Deliberately **not** a scope. No metric key, no universe: a run context is
    not about any one metric, and its digest exists so that a report can say
    "these numbers came from one assembly of evidence" without that fact leaking
    into any single number's identity.
    """
    payload: dict[str, Any] = {
        "run_context_schema_version": context.run_context_schema_version,
        "contract_version": context.contract_version,
    }
    # The profile identity is not repeated here: it is part of the cohort
    # binding, whose digest is in `_member_payload`. Stating it twice would make
    # two fields that could disagree.
    payload.update(_member_payload(context))
    return payload


def validate_business_metric_run_context_structure(
    context: Any,
) -> BusinessMetricRunContext:
    """Re-establish a run context from itself: versions, identities, members."""
    if not isinstance(context, BusinessMetricRunContext):
        raise BusinessMetricBindingError(
            f"{context!r} is not a business metric run context"
        )
    if context.run_context_schema_version != (
        BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION
    ):
        raise BusinessMetricContractError(
            f"unsupported business metric run context schema version: "
            f"{context.run_context_schema_version!r} (this build reads "
            f"{BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION!r})"
        )
    require_supported_business_metric_contract_version(context.contract_version)
    validate_frozen_cohort_binding_structure(context.frozen_cohort_binding)
    validate_fingerprint(
        context.run_context_fingerprint,
        subject="the business metric run context fingerprint",
    )
    _validate_members(context, subject="the run context")
    return context


# --------------------------------------------------------------------------
# the per-metric scope — which exact question
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessMetricScope:
    """Which exact question one metric asked. **Per metric, never per run.**

    A rate means nothing on its own. It is a statement about *this* metric, over
    *this* universe, on *this* cohort, for *this* profile state, under *this*
    contract, with *these* bindings in force — and with nothing else. A scope
    that carried every binding a run happened to have made `DATA_AI_RATE` change
    identity when a URL audit it never reads was re-run; the question had not
    changed, so the scope fingerprint must not have.

    The bindings here are exactly the ones `key.definition.permitted_members`
    allows, and `validate_business_metric_scope_structure` refuses any other.

    **Never Morocco.** There is no country in this dataclass and no default
    anywhere in this module. **No human label**, and no field one could hide in.
    """

    scope_schema_version: str
    contract_version: str
    key: BusinessMetricKey
    universe_kind: BusinessMetricUniverse
    frozen_cohort_binding: FrozenCohortBinding
    scope_fingerprint: str
    profile_target_binding: ProfileTargetBindingEvidence | None = None
    declared_source_universe: DeclaredSourceUniverseEvidence | None = None
    url_audit_binding: UrlAuditBinding | None = None
    dedup_evidence: DedupEvidence | None = None
    benchmark_binding: BenchmarkBinding | None = None
    freshness_binding: FreshnessBinding | None = None

    @property
    def dataset_id(self) -> str:
        return self.frozen_cohort_binding.dataset_id

    @property
    def dataset_fingerprint(self) -> str:
        return self.frozen_cohort_binding.dataset_fingerprint

    @property
    def profile_id(self) -> int:
        return self.frozen_cohort_binding.profile_id

    @property
    def profile_fingerprint(self) -> str:
        return self.frozen_cohort_binding.profile_fingerprint

    @property
    def target_country_code(self) -> str | None:
        """The bound target country, or `None` — for *either* reason.

        Deliberately ambiguous, and documented as such: `None` here means "no
        binding" or "a binding whose target is UNKNOWN", and the two license
        **different** reason codes. A caller that needs to tell them apart looks
        at `profile_target_binding is None`.
        """
        if self.profile_target_binding is None:
            return None
        return self.profile_target_binding.country_code


def business_metric_scope_payload(scope: BusinessMetricScope) -> dict[str, Any]:
    """The one canonical shape of a per-metric scope: its digest domain.

    What is **outside** it: no clock, no directory, no operator, no presentation,
    and **no binding this metric does not use**. `scope_fingerprint` itself is
    outside, because it is derived from this payload.
    """
    payload: dict[str, Any] = {
        "scope_schema_version": scope.scope_schema_version,
        "contract_version": scope.contract_version,
        "key": business_metric_key_payload(scope.key),
        "universe_kind": str(scope.universe_kind),
    }
    payload.update(_member_payload(scope))
    return payload


def validate_business_metric_scope_structure(scope: Any) -> BusinessMetricScope:
    """Re-establish a scope from itself, the permitted-member rule included."""
    if not isinstance(scope, BusinessMetricScope):
        raise BusinessMetricBindingError(f"{scope!r} is not a business metric scope")
    if scope.scope_schema_version != BUSINESS_METRIC_SCOPE_SCHEMA_VERSION:
        raise BusinessMetricContractError(
            f"unsupported business metric scope schema version: "
            f"{scope.scope_schema_version!r} (this build reads "
            f"{BUSINESS_METRIC_SCOPE_SCHEMA_VERSION!r})"
        )
    require_supported_business_metric_contract_version(scope.contract_version)
    if not isinstance(scope.key, BusinessMetricKey):
        raise BusinessMetricContractError(f"{scope.key!r} is not a business metric key")
    require_metric_universe(scope.key.metric, scope.universe_kind)
    validate_frozen_cohort_binding_structure(scope.frozen_cohort_binding)
    validate_fingerprint(
        scope.scope_fingerprint, subject="the business metric scope fingerprint"
    )
    _validate_members(scope, subject=f"the scope of {scope.key.metric}")
    permitted = scope.key.definition.permitted_members
    for member in BusinessEvidenceMember:
        if member_value(scope, member) is not None and member not in permitted:
            raise BusinessMetricContractError(
                f"the scope of {scope.key.metric} carries {member}, which that "
                f"metric does not use (it uses "
                f"{sorted(str(item) for item in permitted) or 'no binding'}); a "
                "question parameterised by something it never reads would change "
                "identity for no reason"
            )
    return scope


# --------------------------------------------------------------------------
# the per-metric evidence view — on which exact evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessMetricEvidenceView:
    """Exactly the artefacts **one metric** rests on, and nothing it produced.

    This is the object `evidence_fingerprint` covers, and the list of what it may
    **not** contain is the most load-bearing sentence in this module: no value,
    no status, no numerator, no denominator, no support block, and no metric key.
    Those are outputs. An evidence digest that moved when a number moved would
    answer "which answer is this" — which is `result_fingerprint`'s job.

    Deliberately **no key either**: two metrics that genuinely rest on the same
    artefacts share one evidence identity, and they should. What differs between
    them is the question, and that is the scope's job.

    The frozen cohort binding is always here, because every metric rests on the
    cohort; the optional members are those the metric's definition permits.
    """

    evidence_view_schema_version: str
    evidence_class: BusinessEvidenceClass
    frozen_cohort_binding: FrozenCohortBinding
    evidence_fingerprint: str
    profile_target_binding: ProfileTargetBindingEvidence | None = None
    declared_source_universe: DeclaredSourceUniverseEvidence | None = None
    url_audit_binding: UrlAuditBinding | None = None
    dedup_evidence: DedupEvidence | None = None
    benchmark_binding: BenchmarkBinding | None = None
    freshness_binding: FreshnessBinding | None = None

    @property
    def dataset_id(self) -> str:
        return self.frozen_cohort_binding.dataset_id

    @property
    def dataset_fingerprint(self) -> str:
        return self.frozen_cohort_binding.dataset_fingerprint

    @property
    def profile_id(self) -> int:
        return self.frozen_cohort_binding.profile_id

    @property
    def profile_fingerprint(self) -> str:
        return self.frozen_cohort_binding.profile_fingerprint

    def has(self, member: BusinessEvidenceMember) -> bool:
        return member_value(self, member) is not None


def business_metric_evidence_payload(
    evidence: BusinessMetricEvidenceView,
) -> dict[str, Any]:
    """The one canonical shape of an evidence view: inputs only."""
    payload: dict[str, Any] = {
        "evidence_view_schema_version": evidence.evidence_view_schema_version,
        "evidence_class": str(evidence.evidence_class),
    }
    payload.update(_member_payload(evidence))
    return payload


def validate_business_metric_evidence_structure(
    evidence: Any,
) -> BusinessMetricEvidenceView:
    """Re-establish an evidence view from itself: versions, shapes, members."""
    if not isinstance(evidence, BusinessMetricEvidenceView):
        raise BusinessMetricBindingError(
            f"{evidence!r} is not a business metric evidence view"
        )
    if evidence.evidence_view_schema_version != BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION:
        raise BusinessMetricContractError(
            f"unsupported business evidence view schema version: "
            f"{evidence.evidence_view_schema_version!r} (this build reads "
            f"{BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION!r})"
        )
    if not isinstance(evidence.evidence_class, BusinessEvidenceClass):
        raise BusinessMetricContractError(
            f"{evidence.evidence_class!r} is not a business evidence class"
        )
    validate_frozen_cohort_binding_structure(evidence.frozen_cohort_binding)
    validate_fingerprint(
        evidence.evidence_fingerprint, subject="the business evidence fingerprint"
    )
    _validate_members(evidence, subject="the evidence view")
    return evidence


# --------------------------------------------------------------------------
# the whole run — Phase 10.4b
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessMetricRunProvenance:
    """Where and when a run was assembled. **Outside `run_fingerprint`.**

    Phase 10.1's rule, applied to this artefact: a digest that moved with the
    clock or with a directory would describe the act of running rather than what
    was run. Two operators computing the same metrics from the same frozen
    snapshot, a week and a filesystem apart, produce one run identity — which is
    exactly what makes the content-addressed storage in `storage.py` able to
    recognise a re-run as the run it already holds.

    The paths are **informative**. They are validated as text and nothing here
    asks the filesystem whether they exist: a path says where somebody read a
    file, and a run that was moved to another machine did not thereby become a
    different measurement.
    """

    generated_at: str | None = None
    dataset_directory: str | None = None
    benchmark_records_path: str | None = None

    def __post_init__(self) -> None:
        if self.generated_at is not None:
            validate_timestamp(
                self.generated_at, subject="the run's generated_at"
            )
        validate_optional_text(
            self.dataset_directory, subject="the run's dataset directory"
        )
        validate_optional_text(
            self.benchmark_records_path,
            subject="the run's benchmark records path",
        )


@dataclass(frozen=True)
class BusinessMetricRun:
    """One complete business / data-quality run: every metric, once, sealed.

    "Complete" is the load-bearing word and it is not a promise this dataclass
    can keep on its own — `run.build_business_metric_run` derives the exact key
    set from the verified cohort and refuses to return anything less. What is
    enforced here is the shape: one result per key, no key twice, canonical
    order, and one contract version across the run, its context and every result
    in it.

    **Nothing is duplicated.** There is no `metric_count`, no `computed_count`,
    no `na_count`, no `values_by_metric`, no second copy of the dataset id and no
    second copy of the run context's digest. Every one of those is derivable from
    `results` and `context`, and a stored copy is a field that can come to
    disagree with the thing it summarises — which is how a report ends up saying
    twenty-seven metrics over a list of twenty-six.

    `provenance` is the one block outside `run_fingerprint`.
    """

    run_schema_version: str
    contract_version: str
    context: BusinessMetricRunContext
    results: tuple[BusinessMetricResult, ...]
    run_fingerprint: str
    provenance: BusinessMetricRunProvenance = BusinessMetricRunProvenance()

    @property
    def keys(self) -> tuple[BusinessMetricKey, ...]:
        """The keys this run answered, in the order the results are held."""
        return tuple(result.key for result in self.results)

    def result_for(self, key: BusinessMetricKey) -> BusinessMetricResult:
        """This run's answer for one key, or a refusal.

        Derived rather than stored: an index built beside `results` would be a
        second statement about the same set.
        """
        if not isinstance(key, BusinessMetricKey):
            raise BusinessMetricContractError(
                f"{key!r} is not a business metric key"
            )
        wanted = business_metric_key_sort_key(key)
        for result in self.results:
            if business_metric_key_sort_key(result.key) == wanted:
                return result
        raise BusinessMetricBindingError(
            f"this run holds no result for {key.metric}"
            + (
                ""
                if key.dimension_value is None
                else f" / {key.dimension_kind}={key.dimension_value}"
            )
        )


def business_metric_run_fingerprint_payload(
    run: BusinessMetricRun,
) -> dict[str, Any]:
    """The run's **identity domain**, and deliberately not its document.

    Exactly four things, and the third and fourth are why a run has an identity
    at all:

        run_schema_version    the layout
        contract_version      the rules the numbers were produced under
        run_context_fingerprint   which assembly of evidence this run had
        results               every key, with that key's own result digest

    The results enter **by digest**, in canonical key order, the same way nested
    artefacts enter every other domain in this package — each one is recomputed
    before a run is sealed, so the digest is the whole of what it says, and a
    changed value moves the result's digest and therefore this one.

    A key is represented by `business_metric_key_payload`, never by `repr()`: a
    digest over a Python representation would move with a dataclass's `__repr__`
    and name nothing stable.

    **Outside**: the provenance and everything in it, the bytes of `run.json`,
    the directory it lands in, the machine, the OS and the Python version. This
    is not the SHA-256 of a file; it is the identity of a measurement, and the
    same measurement written twice is one run.
    """
    return {
        "run_schema_version": run.run_schema_version,
        "contract_version": run.contract_version,
        "run_context_fingerprint": run.context.run_context_fingerprint,
        "results": [
            {
                "key": business_metric_key_payload(result.key),
                "result_fingerprint": result.result_fingerprint,
            }
            for result in run.results
        ],
    }


def business_metric_run_payload(run: BusinessMetricRun) -> dict[str, Any]:
    """The whole run as a structure — the authoritative document of `run.json`.

    Distinct from the fingerprint domain above, and the difference is not a
    block that was left out: the identity holds each result by digest, while
    this holds each result **in full**, because a document a reader can only
    verify by already having the run is not a document. Everything needed to
    rebuild the run is here — the context with every binding it assembled, every
    result with its support, and the provenance.

    `storage.py` renders this and parses it back strictly, field by field.
    """
    return {
        "run_schema_version": run.run_schema_version,
        "contract_version": run.contract_version,
        "context": _full_run_context_payload(run.context),
        "results": [business_metric_result_payload(item) for item in run.results],
        "run_fingerprint": run.run_fingerprint,
        "provenance": {
            "generated_at": run.provenance.generated_at,
            "dataset_directory": run.provenance.dataset_directory,
            "benchmark_records_path": run.provenance.benchmark_records_path,
        },
    }


# -- the full shapes of the nested artefacts ------------------------------
#
# Each is its digest domain **plus the fields that domain deliberately leaves
# out** — a self-declared fingerprint, a provenance path, the two temporal
# values the cohort binding keeps outside its own identity. Written as one
# expression over the existing payload function rather than as a second field
# list, so a field added to a digest domain appears in the document too.


def _full_frozen_cohort_binding_payload(
    binding: FrozenCohortBinding,
) -> dict[str, Any]:
    return {
        **frozen_cohort_binding_payload(binding),
        "dataset_generated_at": binding.dataset_generated_at,
        "freshness_as_of_date": binding.freshness_as_of_date,
        "binding_fingerprint": binding.binding_fingerprint,
    }


def _full_profile_target_binding_payload(
    binding: ProfileTargetBindingEvidence,
) -> dict[str, Any]:
    return {
        **profile_target_binding_payload(binding),
        "binding_fingerprint": binding.binding_fingerprint,
    }


def _full_declared_source_universe_payload(
    evidence: DeclaredSourceUniverseEvidence,
) -> dict[str, Any]:
    return {
        **declared_source_universe_payload(evidence),
        "content_fingerprint": evidence.content_fingerprint,
        "provenance_path": evidence.provenance_path,
    }


def _full_url_audit_binding_payload(binding: UrlAuditBinding) -> dict[str, Any]:
    return {
        **url_audit_binding_payload(binding),
        "binding_fingerprint": binding.binding_fingerprint,
    }


def _full_dedup_evidence_payload(evidence: DedupEvidence) -> dict[str, Any]:
    return {
        **dedup_evidence_payload(evidence),
        "evidence_fingerprint": evidence.evidence_fingerprint,
    }


def _full_benchmark_binding_payload(binding: BenchmarkBinding) -> dict[str, Any]:
    return {
        **benchmark_binding_payload(binding),
        "content_fingerprint": binding.content_fingerprint,
        "provenance_path": binding.provenance_path,
    }


def _full_freshness_binding_payload(binding: FreshnessBinding) -> dict[str, Any]:
    return {
        **freshness_binding_payload(binding),
        "binding_fingerprint": binding.binding_fingerprint,
    }


#: Member -> the function that renders that artefact in full. One mapping, so
#: the document and the parser agree about which shape each member takes.
_FULL_MEMBER_PAYLOADS: Mapping[BusinessEvidenceMember, Any] = {
    BusinessEvidenceMember.PROFILE_TARGET_BINDING: (
        _full_profile_target_binding_payload
    ),
    BusinessEvidenceMember.DECLARED_SOURCE_UNIVERSE: (
        _full_declared_source_universe_payload
    ),
    BusinessEvidenceMember.URL_AUDIT_BINDING: _full_url_audit_binding_payload,
    BusinessEvidenceMember.DEDUP_EVIDENCE: _full_dedup_evidence_payload,
    BusinessEvidenceMember.BENCHMARK_BINDING: _full_benchmark_binding_payload,
    BusinessEvidenceMember.FRESHNESS_BINDING: _full_freshness_binding_payload,
}

assert set(_FULL_MEMBER_PAYLOADS) == set(BusinessEvidenceMember)


def _full_run_context_payload(
    context: BusinessMetricRunContext,
) -> dict[str, Any]:
    """The run context with every artefact in full, not by digest.

    The context's own *digest domain* holds each member by fingerprint, which is
    right for an identity and useless for a document: a stored run must be
    re-readable into the same objects, and a fingerprint reconstructs nothing.
    An absent member is written as an explicit `null` rather than omitted, for
    the same reason it is stated in the digest domain — "this run assembled no
    URL audit" is a fact about the run.
    """
    payload: dict[str, Any] = {
        "run_context_schema_version": context.run_context_schema_version,
        "contract_version": context.contract_version,
        "frozen_cohort_binding": _full_frozen_cohort_binding_payload(
            context.frozen_cohort_binding
        ),
        "run_context_fingerprint": context.run_context_fingerprint,
    }
    for member in BusinessEvidenceMember:
        artefact = member_value(context, member)
        payload[EVIDENCE_MEMBER_ATTRIBUTES[member]] = (
            None if artefact is None else _FULL_MEMBER_PAYLOADS[member](artefact)
        )
    return payload


def validate_business_metric_run_structure(run: Any) -> BusinessMetricRun:
    """Re-establish every invariant a run claims about its own shape.

    Structure only, and the boundary is deliberate: this checks the versions,
    the nested structures, one result per key in canonical order, and the single
    contract version across the run, its context and its results. It recomputes
    **no digest** and re-derives **no key set** — `run.verify_business_metric_run_structure`
    does the first, and only the full verifier can do the second, because the
    dynamic per-source keys are a fact about records this function does not hold.
    """
    if not isinstance(run, BusinessMetricRun):
        raise BusinessMetricContractError(
            f"{run!r} is not a business metric run"
        )
    require_supported_business_metric_run_schema_version(run.run_schema_version)
    require_supported_business_metric_contract_version(run.contract_version)
    validate_business_metric_run_context_structure(run.context)
    if run.context.contract_version != run.contract_version:
        raise BusinessMetricContractError(
            f"the run states contract version {run.contract_version!r} and its "
            f"context was assembled under {run.context.contract_version!r}; a "
            "run and the evidence it drew on answer to one set of rules"
        )
    if not isinstance(run.results, tuple):
        raise BusinessMetricContractError(
            "a business metric run's results are not an immutable sequence"
        )
    if not run.results:
        raise BusinessMetricContractError(
            "a business metric run holds at least one result; an empty run "
            "reports nothing and would still claim to be complete"
        )
    seen: set[tuple[str, str, str]] = set()
    repeated: set[tuple[str, str, str]] = set()
    for result in run.results:
        validate_business_metric_result_structure(result)
        if result.contract_version != run.contract_version:
            raise BusinessMetricContractError(
                f"{result.key.metric} was produced under contract version "
                f"{result.contract_version!r} and this run states "
                f"{run.contract_version!r}"
            )
        identity = business_metric_key_sort_key(result.key)
        if identity in seen:
            repeated.add(identity)
        seen.add(identity)
    if repeated:
        raise BusinessMetricBindingError(
            f"the run holds more than one result for {sorted(repeated)}; one "
            "key carries one result, and two answers under one name leave "
            "nothing downstream able to tell which is the measurement"
        )
    ordered = [business_metric_key_sort_key(result.key) for result in run.results]
    if ordered != sorted(ordered):
        raise BusinessMetricBindingError(
            "the run's results are not in the contract's canonical key order; a "
            "run is a set of answers, and it is held in the one order two equal "
            "runs are guaranteed to agree on"
        )
    validate_fingerprint(
        run.run_fingerprint, subject="the business metric run fingerprint"
    )
    if not isinstance(run.provenance, BusinessMetricRunProvenance):
        raise BusinessMetricContractError(
            f"{run.provenance!r} is not a business metric run provenance block"
        )
    return run
