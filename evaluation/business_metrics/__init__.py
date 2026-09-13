"""Phase 10.4: business and data-quality metrics for a frozen evaluation dataset.

This package answers a different question from `evaluation/metrics`, which is why
it is a **sibling** of it and not a module inside it. Phase 10.3 measures the
*ranking* — Precision@K, Recall@K, NDCG@K against human judgements — and states
in its own docstring that it holds no business KPI. Phase 10.4 measures the
*data*: how much of it is classified, how much is a duplicate, how much still has
a URL that resolves, how much we can date. It shares no abstraction with the
ranking contract on purpose.

Phase 10.4a — this slice — is **the contract and the bindings, and nothing
else**:

    schema.py       the contract — versions, the closed metric table (one
                    universe, one evidence class, one set of required artefacts
                    and one set of permitted refusals per metric), the frozen
                    cohort binding, the eight-state freshness partition, the URL
                    audit policy, the support and result shapes, the run context
                    and the two per-metric projections. It opens nothing and
                    computes nothing.
    fingerprint.py  the identities — the cohort binding's, the run context's, and
                    three per result (scope, evidence, result) — each with one
                    canonical payload and a verifier that recomputes the
                    structure and the digest before believing either.
    bindings.py     the builders, the projections and the verifiers: where every
                    invariant is established and where a contradiction is
                    refused.

## What this slice deliberately does not contain

No `formulas.py` and **no metric is computed anywhere** — not one of the
twenty-seven names in `BusinessMetricName`, and not over the 394 opportunities of
the current snapshot. No `storage.py`: nothing is written. No HTTP client, no
`httpx`, no socket, no CLI. No migration. No change to any production module.

## The seven ideas worth remembering

**The cohort is a bound artefact, not two strings.** `FrozenCohortBinding` is
derived from the verified Phase 10.1 records and manifest and carries what every
later check needs: `cohort_size`, `dataset_generated_at`, the
`freshness_as_of_date` that follows from it, the complete set of
`expected_action_urls` the frozen protocol implies, and the records' own
deduplication counts. No caller states a cohort size and no caller chooses an
as-of date.

**A valid digest over an invalid structure is not a valid dataset.**
`build_frozen_cohort_binding` recomputes Phase 10.1's content fingerprint, and
that is the *second* half of what it does. Before the digest it establishes the
structure, against Phase 10.1's own constants and dataclass: the cohort's exact
`criteria` in their contractual order, its canonical `ordering`, and records
that carry exactly the fields of `EvaluationOpportunityRecord`, with integer —
never boolean — ids that neither repeat nor descend. A snapshot whose records
were reshaped or reordered digests perfectly against a manifest that was
reshaped with them.

**Nothing is sealed from a number somebody stated.** A benchmark binding is
derived from a manifest **and its real rows**: the row count comes from the
rows and the manifest's own claim is checked against it, and the rows' digest is
computed here rather than accepted. And Phase 10.4a exports no way to seal a
`value` at all — the result assembly is private, because a public sealer taking
a number, a numerator and a denominator would be an API for publishing an
arbitrary float as a measured rate before any formula exists to produce one.
Phase 10.4b owns that surface; `verify_business_metric_result` is what stays
public here.

**Every universe's size is checked, not merely stated.** A `FROZEN_COHORT` result
states the cohort's real size, a `PRE_DEDUP_REPRESENTED_UNIVERSE` result states
`S + D`, a `URL_AUDITED_UNIVERSE` result states the audit's size, a
`DECLARED_SOURCE_UNIVERSE` result states the number of declared entries. A wrong
size is a binding failure, never `N_A`. And the metric/universe pairing itself is
fixed by `BUSINESS_METRIC_DEFINITIONS`: `BROKEN_URL_RATE` over `FROZEN_COHORT` is
a contract failure.

**URL audit completeness cannot be skipped.** `assert_url_audit_covers` takes the
cohort binding rather than a caller-supplied list, and it runs when the audit
enters a context, when a result over the audited universe is sealed, and again
when one is verified. A complete audit holds exactly one observation per unique
frozen action URL, `attempted=False` ones included.

**Freshness is derived, and its distribution survives the worst case.**
`as_of_date` is the calendar date of the snapshot's `generated_at` and nothing
else; `INVALID` covers a malformed date *and* one later than that date. The
eight-state distribution is hosted by `PUBLICATION_DATE_COVERAGE`, which stays
COMPUTED even when not one date is valid — `MEAN_KNOWN_FRESHNESS_SCORE` is then
`N_A / NO_VALID_PUBLICATION_DATES`, and the picture is still there.

**Each metric may only refuse in ways it is entitled to, when the condition
holds.** A closed reason enum is necessary and not sufficient:
`DATA_AI_RATE / URL_AUDIT_EVIDENCE_MISSING` names a real code and is nonsense.
Each definition declares its permitted reasons, and the builder checks the
condition each asserts — an absent member reported as its own absence, a target
with no country as `TARGET_COUNTRY_UNKNOWN`, an audit that concluded nothing as
`NO_CONCLUSIVE_URL_AUDIT`.

**Identity is per metric, never per report.** A result's `scope_fingerprint`
covers only what *this* metric asked; its `evidence_fingerprint` covers only the
artefacts *this* metric rests on, and no output of any computation. Re-running a
URL audit moves neither for a `DATA_AI_RATE`.

**No human label, and nothing production can reach.** This package does not
import `evaluation.labeling` — not even for a type — and no scope, context or
evidence field can hold a labelset fingerprint, a grade or a judged count.
Nothing in `services/` imports this package. One function,
`build_profile_target_binding`, accepts a database connection, because Phase
10.1's manifest records the *fingerprint* of the profile context without
republishing its payload; it reads through production's own read-only loaders and
writes nothing.

    operational SQLite / production outputs
        -> frozen evaluation dataset (Phase 10.1)
            -> business / data-quality evidence (here)
                -> business metric results

and never the reverse.
"""


from .bindings import (
    assert_unique_business_metric_keys,
    assert_url_audit_covers,
    benchmark_records_fingerprint,
    build_benchmark_binding,
    build_business_metric_run_context,
    build_dedup_evidence,
    build_freshness_binding,
    build_frozen_cohort_binding,
    build_profile_target_binding,
    build_url_audit_binding,
    declared_source_universe_from_source_map,
    expected_frozen_action_urls,
    project_metric_evidence,
    project_metric_scope,
    require_business_evidence_class,
    source_map_payload,
    validate_target_rule_binding,
    verify_business_metric_result,
    verify_business_metric_results,
    verify_business_metric_run_context,
    verify_metric_scope_and_evidence_agree,
)

from .fingerprint import (
    PLACEHOLDER_FINGERPRINT,
    benchmark_binding_fingerprint,
    business_metric_evidence_fingerprint,
    business_metric_result_fingerprint,
    business_metric_run_context_fingerprint,
    business_metric_scope_fingerprint,
    canonical_benchmark_binding_payload,
    canonical_business_metric_evidence_payload,
    canonical_business_metric_result_payload,
    canonical_business_metric_run_context_payload,
    canonical_business_metric_scope_payload,
    canonical_declared_source_universe_payload,
    canonical_dedup_evidence_payload,
    canonical_freshness_binding_payload,
    canonical_frozen_cohort_binding_payload,
    canonical_profile_target_binding_payload,
    canonical_url_audit_binding_payload,
    declared_source_universe_fingerprint,
    dedup_evidence_fingerprint,
    freshness_binding_fingerprint,
    frozen_cohort_binding_fingerprint,
    profile_target_binding_fingerprint,
    url_audit_binding_fingerprint,
    verify_benchmark_binding_fingerprint,
    verify_business_metric_evidence_fingerprint,
    verify_business_metric_result_fingerprint,
    verify_business_metric_run_context_fingerprint,
    verify_business_metric_scope_fingerprint,
    verify_declared_source_universe_fingerprint,
    verify_dedup_evidence_fingerprint,
    verify_freshness_binding_fingerprint,
    verify_frozen_cohort_binding_fingerprint,
    verify_profile_target_binding_fingerprint,
    verify_url_audit_binding_fingerprint,
)

from .schema import (
    ALWAYS_UNAVAILABLE_METRICS,
    BENCHMARK_BINDING_VERSION,
    BROKEN_STATUS_CODES,
    BUSINESS_EVIDENCE_VIEW_SCHEMA_VERSION,
    BUSINESS_METRIC_CONTRACT_VERSION,
    BUSINESS_METRIC_DEFINITIONS,
    BUSINESS_METRIC_RESULT_SCHEMA_VERSION,
    BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION,
    BUSINESS_METRIC_SCOPE_SCHEMA_VERSION,
    BenchmarkBinding,
    BusinessEvidenceClass,
    BusinessEvidenceClassError,
    BusinessEvidenceMember,
    BusinessMetricArgumentError,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDefinition,
    BusinessMetricDimension,
    BusinessMetricEvidenceView,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricResult,
    BusinessMetricRunContext,
    BusinessMetricScope,
    BusinessMetricStatus,
    BusinessMetricSupport,
    BusinessMetricUnavailableReason,
    BusinessMetricUniverse,
    BusinessMetricsError,
    COMPUTED_ONLY_SUPPORT_BLOCKS,
    DECLARED_SOURCE_UNIVERSE_VERSION,
    DEDUP_EVIDENCE_VERSION,
    DIMENSIONAL_BUSINESS_METRICS,
    DataAiQualificationBreakdown,
    DeclaredSourceEntry,
    DeclaredSourceUniverseEvidence,
    DedupEvidence,
    EVIDENCE_MEMBER_ATTRIBUTES,
    EXPECTED_FRESHNESS_POLICY_VERSION,
    FRESHNESS_AGE_BUCKETS,
    FRESHNESS_BINDING_VERSION,
    FRESHNESS_BUCKET_DAY_BOUNDS,
    FRESHNESS_BUCKET_ORDER,
    FRESHNESS_DISTRIBUTION_HOST_METRIC,
    FROZEN_ACTION_URL_PRECEDENCE,
    FROZEN_ACTION_URL_PROTOCOL_VERSION,
    FROZEN_COHORT_BINDING_VERSION,
    FreshnessAvailabilityWitness,
    FreshnessBinding,
    FreshnessBucket,
    FreshnessBucketCount,
    FreshnessBucketDistribution,
    FrozenCohortBinding,
    HTTP_URL_FORBIDDEN_REASONS,
    INCONCLUSIVE_STATUS_REASONS,
    ListingQualityBreakdown,
    MEMBER_ABSENCE_REASONS,
    MERGED_DUPLICATE_EXCLUSION_KEY,
    NON_HTTP_SCHEME_URL_REASONS,
    NO_RESPONSE_REASONS,
    NO_SCHEME_URL_REASONS,
    OpportunityTypeBreakdown,
    PROFILE_TARGET_BINDING_VERSION,
    ProfileTargetBindingEvidence,
    REQUIRED_SUPPORT_BLOCKS,
    SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS,
    SUPPORT_BLOCK_HOSTS,
    TARGET_VERDICT_HOST_METRIC,
    TargetVerdictBreakdown,
    UNATTEMPTED_REASONS,
    UNIVERSE_DEFINING_MEMBERS,
    URL_AUDIT_BINDING_VERSION,
    URL_AUDIT_MAX_REDIRECTS,
    URL_AUDIT_METHOD,
    URL_AUDIT_POLICY_VERSION,
    URL_AUDIT_RETRIES,
    URL_AUDIT_TIMEOUT_SECONDS,
    URL_AUDIT_USER_AGENT,
    UnknownLocationDiagnostics,
    UrlAuditBinding,
    UrlAuditInconclusiveReason,
    UrlAuditObservation,
    UrlAuditOutcome,
    UrlShape,
    benchmark_binding_payload,
    business_metric_evidence_payload,
    business_metric_key_payload,
    business_metric_key_sort_key,
    business_metric_result_payload,
    business_metric_run_context_payload,
    business_metric_scope_payload,
    business_metric_support_payload,
    calendar_date_of,
    canonical_business_metric_keys,
    data_ai_qualification_breakdown_payload,
    declared_source_entry_payload,
    declared_source_universe_payload,
    dedup_evidence_payload,
    evidence_member_of,
    freshness_availability_witness_payload,
    freshness_binding_payload,
    freshness_bucket_distribution_payload,
    frozen_action_url,
    frozen_cohort_binding_payload,
    is_http_url,
    listing_quality_breakdown_payload,
    member_fingerprint,
    member_value,
    metric_definition,
    opportunity_type_breakdown_payload,
    outcome_for_status,
    profile_target_binding_payload,
    require_metric_universe,
    require_permitted_reason,
    require_supported_business_metric_contract_version,
    target_verdict_breakdown_payload,
    unknown_location_diagnostics_payload,
    url_audit_binding_payload,
    url_audit_observation_payload,
    url_shape,
    validate_benchmark_binding_structure,
    validate_business_metric_evidence_structure,
    validate_business_metric_result_structure,
    validate_business_metric_run_context_structure,
    validate_business_metric_scope_structure,
    validate_count,
    validate_country_code,
    validate_declared_source_universe_structure,
    validate_dedup_evidence_structure,
    validate_fingerprint,
    validate_finite_number,
    validate_freshness_binding_structure,
    validate_frozen_cohort_binding_structure,
    validate_git_commit,
    validate_http_url,
    validate_iso_date,
    validate_optional_text,
    validate_profile_id,
    validate_profile_target_binding_structure,
    validate_status_code,
    validate_text,
    validate_timestamp,
    validate_url_audit_binding_structure,
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
    "BusinessMetricRunContext",
    "BusinessMetricScope",
    "BusinessMetricStatus",
    "BusinessMetricSupport",
    "BusinessMetricUnavailableReason",
    "BusinessMetricUniverse",
    "BusinessMetricsError",
    "COMPUTED_ONLY_SUPPORT_BLOCKS",
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
    "PLACEHOLDER_FINGERPRINT",
    "PROFILE_TARGET_BINDING_VERSION",
    "ProfileTargetBindingEvidence",
    "REQUIRED_SUPPORT_BLOCKS",
    "SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS",
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
    "assert_unique_business_metric_keys",
    "assert_url_audit_covers",
    "benchmark_binding_fingerprint",
    "benchmark_binding_payload",
    "benchmark_records_fingerprint",
    "build_benchmark_binding",
    "build_business_metric_run_context",
    "build_dedup_evidence",
    "build_freshness_binding",
    "build_frozen_cohort_binding",
    "build_profile_target_binding",
    "build_url_audit_binding",
    "business_metric_evidence_fingerprint",
    "business_metric_evidence_payload",
    "business_metric_key_payload",
    "business_metric_key_sort_key",
    "business_metric_result_fingerprint",
    "business_metric_result_payload",
    "business_metric_run_context_fingerprint",
    "business_metric_run_context_payload",
    "business_metric_scope_fingerprint",
    "business_metric_scope_payload",
    "business_metric_support_payload",
    "calendar_date_of",
    "canonical_benchmark_binding_payload",
    "canonical_business_metric_evidence_payload",
    "canonical_business_metric_keys",
    "canonical_business_metric_result_payload",
    "canonical_business_metric_run_context_payload",
    "canonical_business_metric_scope_payload",
    "canonical_declared_source_universe_payload",
    "canonical_dedup_evidence_payload",
    "canonical_freshness_binding_payload",
    "canonical_frozen_cohort_binding_payload",
    "canonical_profile_target_binding_payload",
    "canonical_url_audit_binding_payload",
    "data_ai_qualification_breakdown_payload",
    "declared_source_entry_payload",
    "declared_source_universe_fingerprint",
    "declared_source_universe_from_source_map",
    "declared_source_universe_payload",
    "dedup_evidence_fingerprint",
    "dedup_evidence_payload",
    "evidence_member_of",
    "expected_frozen_action_urls",
    "freshness_availability_witness_payload",
    "freshness_binding_fingerprint",
    "freshness_binding_payload",
    "freshness_bucket_distribution_payload",
    "frozen_action_url",
    "frozen_cohort_binding_fingerprint",
    "frozen_cohort_binding_payload",
    "is_http_url",
    "listing_quality_breakdown_payload",
    "member_fingerprint",
    "member_value",
    "metric_definition",
    "opportunity_type_breakdown_payload",
    "outcome_for_status",
    "profile_target_binding_fingerprint",
    "profile_target_binding_payload",
    "project_metric_evidence",
    "project_metric_scope",
    "require_business_evidence_class",
    "require_metric_universe",
    "require_permitted_reason",
    "require_supported_business_metric_contract_version",
    "source_map_payload",
    "target_verdict_breakdown_payload",
    "unknown_location_diagnostics_payload",
    "url_audit_binding_fingerprint",
    "url_audit_binding_payload",
    "url_audit_observation_payload",
    "url_shape",
    "validate_benchmark_binding_structure",
    "validate_business_metric_evidence_structure",
    "validate_business_metric_result_structure",
    "validate_business_metric_run_context_structure",
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
    "validate_target_rule_binding",
    "validate_text",
    "validate_timestamp",
    "validate_url_audit_binding_structure",
    "verify_benchmark_binding_fingerprint",
    "verify_business_metric_evidence_fingerprint",
    "verify_business_metric_result",
    "verify_business_metric_result_fingerprint",
    "verify_business_metric_results",
    "verify_business_metric_run_context",
    "verify_business_metric_run_context_fingerprint",
    "verify_business_metric_scope_fingerprint",
    "verify_declared_source_universe_fingerprint",
    "verify_dedup_evidence_fingerprint",
    "verify_freshness_binding_fingerprint",
    "verify_frozen_cohort_binding_fingerprint",
    "verify_metric_scope_and_evidence_agree",
    "verify_profile_target_binding_fingerprint",
    "verify_url_audit_binding_fingerprint",
]
