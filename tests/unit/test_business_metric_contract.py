"""The Phase 10.4a contract: what binds a business number, and what forbids one.

This file tests the contract, the identities and the bindings — everything a
business / data-quality claim must establish before a number may exist at all.
**No metric is computed in this slice**, so nothing here asserts a value: what is
asserted about values is the layering, namely that `COMPUTED` is unrepresentable
without the fraction, the *real* universe and the partition it is a fraction of,
and that `N_A` is unrepresentable without a refusal this metric is entitled to
state and whose condition holds or is witnessed.

Nothing here opens the operational database, reads a human label, makes an HTTP
request or touches the 394 real opportunities. Every snapshot is invented, but
invented **in Phase 10.1's own shape**: `manifest_payload` builds a manifest with
the sections that package's fingerprint domain covers and digests it through that
same domain, so `build_frozen_cohort_binding` verifies these fixtures exactly as
it would verify a real snapshot. The one binding that reads a profile is
exercised against a fake connection object that would raise if anything tried to
execute SQL through it.

The cases the independent reviews required are grouped last, one block per
finding.
"""

import ast
import hashlib
import inspect
import textwrap
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path

import pytest

import evaluation.business_metrics as business_package
from evaluation.business_metrics import (
    ALWAYS_UNAVAILABLE_METRICS,
    BROKEN_STATUS_CODES,
    BUSINESS_METRIC_CONTRACT_VERSION,
    BUSINESS_METRIC_DEFINITIONS,
    BUSINESS_METRIC_RESULT_SCHEMA_VERSION,
    BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION,
    BUSINESS_METRIC_SCOPE_SCHEMA_VERSION,
    DIMENSIONAL_BUSINESS_METRICS,
    EXPECTED_FRESHNESS_POLICY_VERSION,
    FRESHNESS_BUCKET_ORDER,
    FRESHNESS_DISTRIBUTION_HOST_METRIC,
    FROZEN_ACTION_URL_PRECEDENCE,
    FROZEN_ACTION_URL_PROTOCOL_VERSION,
    FROZEN_COHORT_BINDING_VERSION,
    INCONCLUSIVE_STATUS_REASONS,
    MEMBER_ABSENCE_REASONS,
    MERGED_DUPLICATE_EXCLUSION_KEY,
    PLACEHOLDER_FINGERPRINT,
    REQUIRED_SUPPORT_BLOCKS,
    SUPPORT_BLOCK_HOSTS,
    SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS,
    TARGET_VERDICT_HOST_METRIC,
    UNATTEMPTED_REASONS,
    UNIVERSE_DEFINING_MEMBERS,
    URL_AUDIT_MAX_REDIRECTS,
    URL_AUDIT_METHOD,
    URL_AUDIT_POLICY_VERSION,
    URL_AUDIT_RETRIES,
    URL_AUDIT_TIMEOUT_SECONDS,
    URL_AUDIT_USER_AGENT,
    BusinessEvidenceClass,
    BusinessEvidenceClassError,
    BusinessEvidenceMember,
    BusinessMetricArgumentError,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDimension,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricResult,
    BusinessMetricStatus,
    BusinessMetricSupport,
    BusinessMetricUnavailableReason,
    BusinessMetricUniverse,
    BusinessMetricsError,
    DataAiQualificationBreakdown,
    DeclaredSourceEntry,
    FreshnessAvailabilityWitness,
    FreshnessBucket,
    FreshnessBucketCount,
    FreshnessBucketDistribution,
    ListingQualityBreakdown,
    OpportunityTypeBreakdown,
    ProfileTargetBindingEvidence,
    TargetVerdictBreakdown,
    UnknownLocationDiagnostics,
    UrlAuditInconclusiveReason,
    UrlAuditObservation,
    UrlAuditOutcome,
    UrlShape,
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
    business_metric_evidence_fingerprint,
    business_metric_key_sort_key,
    business_metric_result_fingerprint,
    business_metric_run_context_fingerprint,
    business_metric_scope_fingerprint,
    calendar_date_of,
    canonical_business_metric_evidence_payload,
    canonical_business_metric_keys,
    canonical_business_metric_result_payload,
    canonical_business_metric_run_context_payload,
    canonical_business_metric_scope_payload,
    canonical_declared_source_universe_payload,
    canonical_frozen_cohort_binding_payload,
    declared_source_universe_from_source_map,
    expected_frozen_action_urls,
    frozen_action_url,
    frozen_cohort_binding_fingerprint,
    is_http_url,
    metric_definition,
    outcome_for_status,
    project_metric_evidence,
    project_metric_scope,
    require_business_evidence_class,
    require_metric_universe,
    require_permitted_reason,
    require_supported_business_metric_contract_version,
    source_map_payload,
    url_shape,
    validate_benchmark_binding_structure,
    validate_business_metric_scope_structure,
    verify_business_metric_result,
    verify_benchmark_binding_fingerprint,
    verify_business_metric_result_fingerprint,
    verify_business_metric_results,
    verify_business_metric_run_context,
    verify_business_metric_scope_fingerprint,
    verify_declared_source_universe_fingerprint,
    verify_dedup_evidence_fingerprint,
    verify_frozen_cohort_binding_fingerprint,
    verify_url_audit_binding_fingerprint,
)
from evaluation.business_metrics.bindings import _build_business_metric_result
from evaluation.dataset import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationDatasetError,
    EvaluationOpportunityRecord,
)
from evaluation.dataset.fingerprint import manifest_fingerprint_domain
from services.collector.matching.fingerprint import canonical_json

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

PROFILE_FINGERPRINT = "9" * 64
OTHER_PROFILE_FINGERPRINT = "8" * 64
PROFILE_ID = 1
OTHER_PROFILE_ID = 2
USER_ID = 7
RESTRICTED_RULE = "mobility-restricted-country-v1"
GENERATED_AT = "2026-03-01T09:15:00.123456+00:00"
LATER_GENERATED_AT = "2026-05-04T18:00:00+00:00"
AS_OF_DATE = "2026-03-01"
LATER_AS_OF_DATE = "2026-05-04"
CAMPAIGN_AT = "2026-03-02T08:00:00Z"
GIT_COMMIT = "c" * 40

ALPHA = "https://alpha.example/jobs/1"
BETA = "https://beta.example/jobs/2"


# --------------------------------------------------------------------------
# invented artefacts, in Phase 10.1's own shape
# --------------------------------------------------------------------------


#: A record of the Phase 10.1 contract with every field at its null-ish value,
#: keyed from the dataclass itself so a field added upstream appears here too
#: rather than turning every fixture into a structural refusal.
_EMPTY_RECORD = {
    field.name: [] if field.name in ("absorbed_duplicate_ids", "sources",
                                     "geography_segments") else None
    for field in fields(EvaluationOpportunityRecord)
}


def record(
    opportunity_id: int,
    *,
    application_url=None,
    source_url=None,
    canonical_url=None,
    absorbed=(),
    **overrides,
):
    """One frozen record carrying **exactly** the contract's fields.

    Not reduced to the five this slice reads: `build_frozen_cohort_binding` now
    refuses a record whose field set is not the field set of
    `EvaluationOpportunityRecord`, and a fixture that skipped the others would
    be testing a shape the production path rejects.
    """
    return {
        **_EMPTY_RECORD,
        "opportunity_id": opportunity_id,
        "application_url": application_url,
        "source_url": source_url,
        "canonical_url": canonical_url,
        "absorbed_duplicate_ids": list(absorbed),
        **overrides,
    }


DEFAULT_RECORDS = (
    record(1, application_url=ALPHA, absorbed=(11, 12)),
    record(2, source_url=BETA, absorbed=(13,)),
    record(3),
)

UPSTREAM = {
    "matching_status": "PRESENT",
    "matching_run_id": 4,
    "matching_run_fingerprint": "1" * 64,
    "matching_engine_version": "matching-engine-v1",
    "matching_rules_version": "matching-rules-v1",
    "matching_selection_version": "matching-selection-v1",
    "recommendation_status": "PRESENT",
    "recommendation_run_id": 5,
    "recommendation_run_fingerprint": "2" * 64,
    "recommendation_engine_version": "recommendation-engine-v1",
    "recommendation_rules_version": "recommendation-rules-v1",
}


def manifest_payload(
    records=DEFAULT_RECORDS,
    *,
    generated_at=GENERATED_AT,
    profile_id=PROFILE_ID,
    profile_fingerprint=PROFILE_FINGERPRINT,
    merged_duplicate=None,
    schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
    cohort_version=EVALUATION_COHORT_VERSION,
    ordering=EVALUATION_CANONICAL_ORDER,
    criteria=EVALUATION_COHORT_CRITERIA,
    record_count=None,
    forge_content_fingerprint=None,
    forge_dataset_id=None,
):
    """A manifest in Phase 10.1's shape, digested through its own domain.

    Built rather than stubbed so that `build_frozen_cohort_binding` runs its real
    verification against these fixtures: the same `manifest_fingerprint_domain`,
    the same record digests, the same derived `dataset_id`.
    """
    if merged_duplicate is None:
        merged_duplicate = sum(len(item["absorbed_duplicate_ids"]) for item in records)
    payload = {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "git_commit": None,
        "record_count": len(records) if record_count is None else record_count,
        "profile_context": {
            "profile_id": profile_id,
            "user_id": USER_ID,
            "fingerprint": profile_fingerprint,
        },
        "cohort": {
            "version": cohort_version,
            "criteria": list(criteria),
            "ordering": ordering,
            "excluded_counts": {
                MERGED_DUPLICATE_EXCLUSION_KEY: merged_duplicate,
                "inactive": 7,
            },
        },
        "upstream": dict(UPSTREAM),
    }
    domain = manifest_fingerprint_domain(payload)
    domain["records"] = [
        hashlib.sha256(canonical_json(item).encode("utf-8")).hexdigest()
        for item in records
    ]
    fingerprint = hashlib.sha256(canonical_json(domain).encode("utf-8")).hexdigest()
    payload["content_fingerprint"] = (
        fingerprint if forge_content_fingerprint is None else forge_content_fingerprint
    )
    payload["dataset_id"] = (
        f"{schema_version}-{fingerprint[:16]}"
        if forge_dataset_id is None
        else forge_dataset_id
    )
    return payload


def cohort(records=DEFAULT_RECORDS, **overrides):
    """A sealed frozen cohort binding, verified rather than dictated."""
    return build_frozen_cohort_binding(records, manifest_payload(records, **overrides))


def target_binding(
    *,
    country_code="MA",
    rule_id=RESTRICTED_RULE,
    profile_fingerprint=PROFILE_FINGERPRINT,
    profile_id=PROFILE_ID,
):
    from evaluation.business_metrics import (
        PROFILE_TARGET_BINDING_VERSION,
        profile_target_binding_fingerprint,
    )

    draft = ProfileTargetBindingEvidence(
        binding_version=PROFILE_TARGET_BINDING_VERSION,
        profile_id=profile_id,
        profile_fingerprint=profile_fingerprint,
        country_code=country_code,
        rule_id=rule_id,
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    return replace(
        draft, binding_fingerprint=profile_target_binding_fingerprint(draft)
    )


@dataclass(frozen=True)
class _FakeEntry:
    """A source map entry with fields beyond the six a coverage rate projects."""

    id: str
    name: str = ""
    homepage_url: str = ""
    homepage_url_status: str = "WELL_KNOWN_UNVERIFIED"
    source_class: str = "JOB_BOARD"
    priority: str = "P0"
    coverage_role: str = "PRIMARY"
    collection_strategy: str = "FUTURE_COLLECTOR"
    integration_status: str = "ACTIVE"
    country: str = "MA"
    production_source_id: str | None = None
    live_canary: bool = False
    notes: str = "as written"

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.id.replace("_", " ").title())
        if not self.homepage_url:
            object.__setattr__(self, "homepage_url", f"https://{self.id}.example/")


@dataclass(frozen=True)
class _FakeSourceMap:
    sources: tuple[_FakeEntry, ...]
    scope_country: str = "MA"
    version: str = "v1"
    map_name: str = "invented_source_coverage"
    created_for_phase: str = "7C.1"
    is_production_registry: bool = False
    production_registry_path: str = "config/sources.yaml"

    def __init__(
        self,
        entries,
        *,
        scope_country: str = "MA",
        version: str = "v1",
    ) -> None:
        object.__setattr__(self, "sources", tuple(entries))
        object.__setattr__(self, "scope_country", scope_country)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "map_name", "invented_source_coverage")
        object.__setattr__(self, "created_for_phase", "7C.1")
        object.__setattr__(self, "is_production_registry", False)
        object.__setattr__(self, "production_registry_path", "config/sources.yaml")


def declared_universe(entries=None, *, scope_country="MA", git_commit=GIT_COMMIT):
    source_map = _FakeSourceMap(
        entries
        if entries is not None
        else (_FakeEntry("alpha_board"), _FakeEntry("beta_board")),
        scope_country=scope_country,
    )
    return declared_source_universe_from_source_map(
        source_map,
        git_commit=git_commit,
        provenance_path="evaluation/source_coverage/invented.yaml",
    )


def observation(url, *, outcome=UrlAuditOutcome.VALID, attempted=True, **kwargs):
    if attempted:
        kwargs.setdefault("audited_at", CAMPAIGN_AT)
        if outcome is UrlAuditOutcome.VALID:
            kwargs.setdefault("status_code", 200)
        elif outcome is UrlAuditOutcome.BROKEN:
            kwargs.setdefault("status_code", 404)
        elif "status_code" not in kwargs:
            kwargs.setdefault("reason", UrlAuditInconclusiveReason.TIMEOUT)
    else:
        kwargs.setdefault("reason", UrlAuditInconclusiveReason.ROBOTS_DISALLOWED)
    if kwargs.get("status_code") is not None and outcome is (
        UrlAuditOutcome.INCONCLUSIVE
    ):
        kwargs.setdefault("reason", INCONCLUSIVE_STATUS_REASONS[kwargs["status_code"]])
    return UrlAuditObservation(
        requested_url=url, attempted=attempted, outcome=outcome, **kwargs
    )


def url_audit(cohort_binding=None, observations=None, *, audit_started_at=CAMPAIGN_AT, **kwargs):
    binding = cohort_binding if cohort_binding is not None else cohort()
    if observations is None:
        outcomes = (UrlAuditOutcome.VALID, UrlAuditOutcome.BROKEN)
        observations = tuple(
            observation(url, outcome=outcomes[index % len(outcomes)])
            for index, url in enumerate(binding.expected_action_urls)
        )
    return build_url_audit_binding(
        cohort_binding=binding,
        audit_started_at=audit_started_at,
        observations=observations,
        **kwargs,
    )


def benchmark_row(index, *, note="as observed"):
    """One benchmark row, in the shape the real gold file holds."""
    return {
        "benchmark_id": f"invented-{index}",
        "title": f"Stage PFE - invented opportunity {index}",
        "organization": f"Invented Org {index}",
        "country_code": "MA",
        "source_name": "invented.example",
        "source_url": f"https://invented.example/offers/{index}",
        "expected_opportunity_type": "PFE",
        "expected_data_ai": True,
        "notes": note,
    }


def benchmark_manifest(
    *, ready=False, rows=2, target=60, scope_country="MA", declared_rows=None
):
    return {
        "benchmark_name": "invented_gold_v1",
        "version": "v1",
        "scope_country": scope_country,
        "status": "READY" if ready else "DRAFT",
        "target_minimum_rows": target,
        "current_rows": rows if declared_rows is None else declared_rows,
        "evaluation_ready": ready,
    }


def benchmark(
    *, ready=False, rows=2, target=60, scope_country="MA", records=None, **manifest
):
    """A benchmark binding derived from a manifest and the rows it describes."""
    supplied = (
        tuple(benchmark_row(index) for index in range(1, rows + 1))
        if records is None
        else tuple(records)
    )
    return build_benchmark_binding(
        benchmark_manifest(
            ready=ready, rows=len(supplied), target=target,
            scope_country=scope_country, **manifest
        ),
        supplied,
    )


def context(cohort_binding=None, **kwargs):
    binding = cohort_binding if cohort_binding is not None else cohort()
    kwargs.setdefault("cohort_binding", binding)
    return build_business_metric_run_context(**kwargs)


def full_context(cohort_binding=None, **kwargs):
    """A run that assembled *everything*. The leak-detector fixture."""
    binding = cohort_binding if cohort_binding is not None else cohort()
    kwargs.setdefault(
        "profile_target_binding",
        target_binding(
            profile_id=binding.profile_id,
            profile_fingerprint=binding.profile_fingerprint,
        ),
    )
    kwargs.setdefault("declared_source_universe", declared_universe())
    kwargs.setdefault("url_audit_binding", url_audit(binding))
    kwargs.setdefault("dedup_evidence", build_dedup_evidence(binding))
    kwargs.setdefault("benchmark_binding", benchmark())
    kwargs.setdefault("freshness_binding", build_freshness_binding(binding))
    return context(binding, **kwargs)


COHORT_SIZE = len(DEFAULT_RECORDS)
PRE_DEDUP_SIZE = COHORT_SIZE + 3
AUDITED_SIZE = 2
DECLARED_SIZE = 2

UNIVERSE_SIZES = {
    BusinessMetricUniverse.FROZEN_COHORT: COHORT_SIZE,
    BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE: PRE_DEDUP_SIZE,
    BusinessMetricUniverse.URL_AUDITED_UNIVERSE: AUDITED_SIZE,
    BusinessMetricUniverse.DECLARED_SOURCE_UNIVERSE: DECLARED_SIZE,
}


def distribution(counts=None, universe_size=COHORT_SIZE):
    """A freshness partition over the cohort, all MISSING unless told otherwise."""
    if counts is None:
        counts = {bucket: 0 for bucket in FRESHNESS_BUCKET_ORDER}
        counts[FreshnessBucket.MISSING] = universe_size
    return FreshnessBucketDistribution(
        entries=tuple(
            FreshnessBucketCount(bucket=bucket, count=counts[bucket])
            for bucket in FRESHNESS_BUCKET_ORDER
        ),
        universe_size=universe_size,
    )


def data_ai_breakdown(core=1, adjacent=0, out_of_scope=1, uncertain=1, unclassified=0):
    return DataAiQualificationBreakdown(
        core_target_count=core,
        adjacent_target_count=adjacent,
        out_of_scope_count=out_of_scope,
        uncertain_count=uncertain,
        unclassified_count=unclassified,
    )


def type_breakdown(pfe=1, internship=0, apprenticeship=1, graduate=0, job=1, unknown=0, unclassified=0):
    return OpportunityTypeBreakdown(
        pfe_count=pfe,
        internship_count=internship,
        apprenticeship_count=apprenticeship,
        graduate_count=graduate,
        job_count=job,
        unknown_type_count=unknown,
        unclassified_type_count=unclassified,
    )


def quality_breakdown(normal=1, non_job=1, insufficient=1, unclassified=0):
    return ListingQualityBreakdown(
        normal_listing_count=normal,
        possible_non_job_page_count=non_job,
        insufficient_content_count=insufficient,
        unclassified_count=unclassified,
    )


def location_diagnostics(ambiguous=1, no_segments=1, unknown_segment=0):
    return UnknownLocationDiagnostics(
        ambiguous_location_count=ambiguous,
        no_geography_segments_count=no_segments,
        unknown_segment_record_count=unknown_segment,
    )


#: The support block each host metric needs, with a numerator that matches it.
DEFAULT_SUPPORT_BLOCKS = {
    BusinessMetricName.DATA_AI_RATE: ("data_ai_breakdown", data_ai_breakdown, 1),
    BusinessMetricName.PFE_OR_INTERNSHIP_RATE: (
        "opportunity_type_breakdown",
        type_breakdown,
        1,
    ),
    BusinessMetricName.NORMAL_LISTING_RATE: (
        "listing_quality_breakdown",
        quality_breakdown,
        1,
    ),
    BusinessMetricName.UNKNOWN_LOCATION_RATE: (
        "unknown_location_diagnostics",
        location_diagnostics,
        1,
    ),
    BusinessMetricName.PUBLICATION_DATE_COVERAGE: (
        "freshness_distribution",
        distribution,
        0,
    ),
    BusinessMetricName.TARGET_COUNTRY_MATCH_RATE: (
        "target_verdict_breakdown",
        lambda: TargetVerdictBreakdown(1, 1, 1),
        1,
    ),
}


def key_of(metric, **kwargs):
    if metric in DIMENSIONAL_BUSINESS_METRICS and not kwargs:
        kwargs = {
            "dimension_kind": BusinessMetricDimension.SOURCE_ID,
            "dimension_value": "stage_ma",
        }
    return BusinessMetricKey(metric, **kwargs)


def computed(
    metric=BusinessMetricName.CORE_DATA_AI_RATE,
    *,
    run_context=None,
    value=0.5,
    numerator=None,
    denominator=None,
    universe_size=None,
    support=None,
    universe=None,
    **key_kwargs,
):
    """A COMPUTED result that satisfies the contract unless deliberately broken."""
    key = key_of(metric, **key_kwargs)
    declared = metric_definition(metric).universe
    size = UNIVERSE_SIZES[declared] if universe_size is None else universe_size
    if support is None:
        blocks = {}
        resolved_numerator = 1 if numerator is None else numerator
        required = DEFAULT_SUPPORT_BLOCKS.get(metric)
        if required is not None:
            attribute, factory, block_numerator = required
            blocks[attribute] = factory()
            if numerator is None:
                resolved_numerator = block_numerator
        support = BusinessMetricSupport(
            numerator=resolved_numerator,
            denominator=size if denominator is None else denominator,
            universe_size=size,
            **blocks,
        )
    return _build_business_metric_result(
        key=key,
        status=BusinessMetricStatus.COMPUTED,
        universe_kind=universe or declared,
        context=run_context if run_context is not None else full_context(),
        support=support,
        value=value,
    )


def unavailable(
    metric,
    reason,
    *,
    run_context=None,
    universe_size="auto",
    support=None,
    witness=None,
    **key_kwargs,
):
    key = key_of(metric, **key_kwargs)
    declared = metric_definition(metric).universe
    if support is None:
        size = UNIVERSE_SIZES[declared] if universe_size == "auto" else universe_size
        extra = {}
        if reason is BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES:
            extra["freshness_availability_witness"] = (
                FreshnessAvailabilityWitness(known_date_count=0)
                if witness is None
                else witness
            )
        support = BusinessMetricSupport(universe_size=size, **extra)
    return _build_business_metric_result(
        key=key,
        status=BusinessMetricStatus.N_A,
        universe_kind=declared,
        context=run_context if run_context is not None else full_context(),
        support=support,
        reason=reason,
    )


def reseal_scope(scope, **changes):
    draft = replace(scope, scope_fingerprint=PLACEHOLDER_FINGERPRINT, **changes)
    return replace(draft, scope_fingerprint=business_metric_scope_fingerprint(draft))


def reseal_evidence(evidence, **changes):
    draft = replace(evidence, evidence_fingerprint=PLACEHOLDER_FINGERPRINT, **changes)
    return replace(
        draft, evidence_fingerprint=business_metric_evidence_fingerprint(draft)
    )


# ====================================================================
# 1. closed enums, versions, and the metric table
# ====================================================================


def test_the_business_contract_has_its_own_version() -> None:
    assert (
        BUSINESS_METRIC_CONTRACT_VERSION == "business-data-quality-metric-contract-v1"
    )
    from evaluation.metrics import METRIC_CONTRACT_VERSION

    assert BUSINESS_METRIC_CONTRACT_VERSION != METRIC_CONTRACT_VERSION
    assert SUPPORTED_BUSINESS_METRIC_CONTRACT_VERSIONS == (
        BUSINESS_METRIC_CONTRACT_VERSION,
    )


def test_the_structural_versions_are_separate_from_the_semantic_contract() -> None:
    assert BUSINESS_METRIC_SCOPE_SCHEMA_VERSION == "business-metric-scope-v1"
    assert BUSINESS_METRIC_RESULT_SCHEMA_VERSION == "business-metric-result-v1"
    assert (
        BUSINESS_METRIC_RUN_CONTEXT_SCHEMA_VERSION == "business-metric-run-context-v1"
    )
    assert FROZEN_COHORT_BINDING_VERSION == "business-frozen-cohort-binding-v1"


def test_an_unsupported_contract_version_is_refused() -> None:
    with pytest.raises(BusinessMetricContractError, match="unsupported"):
        require_supported_business_metric_contract_version("v2")


def test_the_closed_vocabularies() -> None:
    assert {str(item) for item in BusinessMetricUniverse} == {
        "FROZEN_COHORT",
        "PRE_DEDUP_REPRESENTED_UNIVERSE",
        "URL_AUDITED_UNIVERSE",
        "DECLARED_SOURCE_UNIVERSE",
    }
    assert {str(item) for item in BusinessMetricStatus} == {"COMPUTED", "N_A"}
    assert {str(item) for item in BusinessMetricUnavailableReason} == {
        "URL_AUDIT_EVIDENCE_MISSING",
        "NO_CONCLUSIVE_URL_AUDIT",
        "SOURCE_BENCHMARK_NOT_EVALUATION_READY",
        "OPPORTUNITY_SKILLS_NOT_FROZEN",
        "TARGET_SCOPE_BINDING_MISSING",
        "TARGET_COUNTRY_UNKNOWN",
        "NO_VALID_PUBLICATION_DATES",
        "DECLARED_SOURCE_UNIVERSE_MISSING",
        "DEDUP_EVIDENCE_MISSING",
        "FRESHNESS_BINDING_MISSING",
    }
    assert {str(item) for item in BusinessEvidenceMember} == {
        "PROFILE_TARGET_BINDING",
        "DECLARED_SOURCE_UNIVERSE",
        "URL_AUDIT_BINDING",
        "DEDUP_EVIDENCE",
        "BENCHMARK_BINDING",
        "FRESHNESS_BINDING",
    }


def test_no_integrity_failure_is_available_as_an_n_a_reason() -> None:
    names = {str(item) for item in BusinessMetricUnavailableReason}
    for forbidden in (
        "INCONSISTENT_DEDUP_EVIDENCE",
        "FINGERPRINT_MISMATCH",
        "WRONG_UNIVERSE_SIZE",
        "INCOMPLETE_URL_AUDIT",
        "DUPLICATE_METRIC_KEY",
    ):
        assert forbidden not in names


def test_the_metric_registry_covers_every_architected_metric() -> None:
    assert {str(item) for item in BusinessMetricName} == {
        "TARGET_COUNTRY_MATCH_RATE",
        "DATA_AI_RATE",
        "CORE_DATA_AI_RATE",
        "CLASSIFICATION_COVERAGE_RATE",
        "PFE_OR_INTERNSHIP_RATE",
        "OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE",
        "APPLIED_DUPLICATE_RATE",
        "DUPLICATE_CLUSTER_RATE",
        "BROKEN_URL_RATE",
        "URL_AUDIT_COVERAGE_RATE",
        "URL_CONCLUSIVE_COVERAGE_RATE",
        "MISSING_ACTION_URL_RATE",
        "UNKNOWN_LOCATION_RATE",
        "PUBLICATION_DATE_COVERAGE",
        "MEAN_KNOWN_FRESHNESS_SCORE",
        "DESCRIPTION_PRESENCE_RATE",
        "LOCATION_TEXT_PRESENCE_RATE",
        "ACTION_URL_PRESENCE_RATE",
        "DEADLINE_DATE_COVERAGE",
        "SOURCE_PROVENANCE_COVERAGE_RATE",
        "NORMAL_LISTING_RATE",
        "OBSERVED_SOURCE_CONTRIBUTION",
        "MULTI_SOURCE_RECORD_RATE",
        "MISSING_SOURCE_PROVENANCE_RATE",
        "ACTIVE_DECLARED_SOURCE_RATE",
        "DECLARED_SOURCE_DISCOVERY_RECALL",
        "OPPORTUNITY_SKILL_COVERAGE",
    }


def test_every_metric_has_exactly_one_declared_universe() -> None:
    expected = {
        BusinessMetricUniverse.FROZEN_COHORT: {
            "TARGET_COUNTRY_MATCH_RATE",
            "DATA_AI_RATE",
            "CORE_DATA_AI_RATE",
            "CLASSIFICATION_COVERAGE_RATE",
            "PFE_OR_INTERNSHIP_RATE",
            "OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE",
            "DUPLICATE_CLUSTER_RATE",
            "MISSING_ACTION_URL_RATE",
            "UNKNOWN_LOCATION_RATE",
            "PUBLICATION_DATE_COVERAGE",
            "MEAN_KNOWN_FRESHNESS_SCORE",
            "DESCRIPTION_PRESENCE_RATE",
            "LOCATION_TEXT_PRESENCE_RATE",
            "ACTION_URL_PRESENCE_RATE",
            "DEADLINE_DATE_COVERAGE",
            "SOURCE_PROVENANCE_COVERAGE_RATE",
            "NORMAL_LISTING_RATE",
            "OBSERVED_SOURCE_CONTRIBUTION",
            "MULTI_SOURCE_RECORD_RATE",
            "MISSING_SOURCE_PROVENANCE_RATE",
            "OPPORTUNITY_SKILL_COVERAGE",
        },
        BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE: {
            "APPLIED_DUPLICATE_RATE"
        },
        BusinessMetricUniverse.URL_AUDITED_UNIVERSE: {
            "BROKEN_URL_RATE",
            "URL_AUDIT_COVERAGE_RATE",
            "URL_CONCLUSIVE_COVERAGE_RATE",
        },
        BusinessMetricUniverse.DECLARED_SOURCE_UNIVERSE: {
            "ACTIVE_DECLARED_SOURCE_RATE",
            "DECLARED_SOURCE_DISCOVERY_RECALL",
        },
    }
    actual = {universe: set() for universe in BusinessMetricUniverse}
    for metric, definition in BUSINESS_METRIC_DEFINITIONS.items():
        actual[definition.universe].add(str(metric))
    assert actual == expected


def test_a_universe_defining_artefact_is_required_by_its_metrics() -> None:
    for metric, definition in BUSINESS_METRIC_DEFINITIONS.items():
        defining = UNIVERSE_DEFINING_MEMBERS.get(definition.universe)
        if defining is not None:
            assert defining in definition.required_members, metric


def test_every_required_member_has_an_absence_reason_the_metric_may_state() -> None:
    for metric, definition in BUSINESS_METRIC_DEFINITIONS.items():
        for member in definition.required_members:
            assert MEMBER_ABSENCE_REASONS[member] in definition.permitted_reasons


def test_the_freshness_partition_is_the_frozen_eight_state_one() -> None:
    assert [str(item) for item in FRESHNESS_BUCKET_ORDER] == [
        "MISSING",
        "INVALID",
        "AGE_0_2",
        "AGE_3_7",
        "AGE_8_14",
        "AGE_15_30",
        "AGE_31_60",
        "AGE_GT_60",
    ]


def test_the_freshness_policy_id_matches_production() -> None:
    from services.priority.models import PRIORITY_FRESHNESS_VERSION

    assert EXPECTED_FRESHNESS_POLICY_VERSION == PRIORITY_FRESHNESS_VERSION


# ====================================================================
# 2. metric keys and dimensions
# ====================================================================


def test_source_id_is_allowed_only_for_the_dimensional_metric() -> None:
    assert DIMENSIONAL_BUSINESS_METRICS == frozenset(
        {BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION}
    )
    with pytest.raises(BusinessMetricContractError, match="not dimensional"):
        BusinessMetricKey(
            BusinessMetricName.BROKEN_URL_RATE,
            dimension_kind=BusinessMetricDimension.SOURCE_ID,
            dimension_value="stage_ma",
        )
    with pytest.raises(BusinessMetricContractError, match="is dimensional"):
        BusinessMetricKey(BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION)
    with pytest.raises(BusinessMetricContractError, match="no dimension to take"):
        BusinessMetricKey(
            BusinessMetricName.DESCRIPTION_PRESENCE_RATE, dimension_value="x"
        )


def test_a_source_id_dimension_needs_a_non_empty_value() -> None:
    for bad in ("", "   ", " stage_ma", None):
        with pytest.raises(BusinessMetricsError):
            BusinessMetricKey(
                BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
                dimension_kind=BusinessMetricDimension.SOURCE_ID,
                dimension_value=bad,
            )


def test_keys_order_by_metric_then_dimension_kind_then_value() -> None:
    keys = [
        BusinessMetricKey(
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=BusinessMetricDimension.SOURCE_ID,
            dimension_value="stagiaires_ma",
        ),
        BusinessMetricKey(BusinessMetricName.DATA_AI_RATE),
        BusinessMetricKey(
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=BusinessMetricDimension.SOURCE_ID,
            dimension_value="stage_ma",
        ),
    ]
    ordered = canonical_business_metric_keys(keys)
    assert ordered[0].metric is BusinessMetricName.DATA_AI_RATE
    assert ordered[1].dimension_value == "stage_ma"
    assert [business_metric_key_sort_key(item) for item in ordered] == sorted(
        business_metric_key_sort_key(item) for item in keys
    )


def test_a_duplicate_metric_key_is_refused_in_a_run() -> None:
    duplicated = [
        BusinessMetricKey(BusinessMetricName.DATA_AI_RATE),
        BusinessMetricKey(BusinessMetricName.DATA_AI_RATE),
    ]
    with pytest.raises(BusinessMetricBindingError, match="more than once"):
        assert_unique_business_metric_keys(duplicated)
    run = full_context()
    with pytest.raises(BusinessMetricBindingError, match="more than once"):
        verify_business_metric_results(
            [computed(run_context=run), computed(run_context=run, value=0.6)],
            context=run,
        )


# ====================================================================
# 3. status invariants
# ====================================================================


def test_computed_requires_a_value_a_fraction_and_a_universe() -> None:
    with pytest.raises(BusinessMetricContractError, match="states no value"):
        computed(value=None)
    with pytest.raises(BusinessMetricContractError, match="without the fraction"):
        computed(support=BusinessMetricSupport(universe_size=COHORT_SIZE))


def test_n_a_requires_a_reason_and_forbids_a_value() -> None:
    with pytest.raises(BusinessMetricContractError, match="without a stated reason"):
        _build_business_metric_result(
            key=BusinessMetricKey(BusinessMetricName.DUPLICATE_CLUSTER_RATE),
            status=BusinessMetricStatus.N_A,
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            context=full_context(),
            support=BusinessMetricSupport(universe_size=COHORT_SIZE),
        )
    with pytest.raises(BusinessMetricContractError, match="not even a zero"):
        _build_business_metric_result(
            key=BusinessMetricKey(BusinessMetricName.DUPLICATE_CLUSTER_RATE),
            status=BusinessMetricStatus.N_A,
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            context=context(),
            support=BusinessMetricSupport(universe_size=COHORT_SIZE),
            value=0.0,
            reason=BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING,
        )


def test_n_a_carries_no_fraction_either() -> None:
    with pytest.raises(BusinessMetricContractError, match="states a fraction"):
        unavailable(
            BusinessMetricName.DUPLICATE_CLUSTER_RATE,
            BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING,
            run_context=context(),
            support=BusinessMetricSupport(
                numerator=0, denominator=12, universe_size=COHORT_SIZE
            ),
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinity_are_refused_as_values(bad) -> None:
    with pytest.raises(BusinessMetricArgumentError, match="finite"):
        computed(value=bad)


@pytest.mark.parametrize("bad", [True, False, 1.5, "10"])
def test_a_universe_size_is_an_integer_count(bad) -> None:
    with pytest.raises(BusinessMetricBindingError):
        BusinessMetricSupport(universe_size=bad)


def test_a_result_is_frozen_and_recomputes_its_own_digest() -> None:
    result = computed()
    with pytest.raises(FrozenInstanceError):
        result.value = 0.9  # type: ignore[misc]
    assert business_metric_result_fingerprint(result) == result.result_fingerprint
    assert "result_fingerprint" not in canonical_business_metric_result_payload(result)
    forged = replace(result, result_fingerprint="e" * 64)
    with pytest.raises(BusinessMetricBindingError, match="is not what it says"):
        verify_business_metric_result_fingerprint(forged)


def test_a_result_survives_reverification_against_its_own_context() -> None:
    run = full_context()
    result = computed(run_context=run)
    assert verify_business_metric_result(result, context=run) is result


# ====================================================================
# 4. architecture guards
# ====================================================================

PACKAGE_ROOT = REPOSITORY_ROOT / "evaluation/business_metrics"
PURE_MODULES = ("schema.py", "fingerprint.py")

#: Every module of the package, Phase 10.4b's three included. The guards below
#: that apply to all of them — no human label, no ranking package, no network —
#: are guarantees of the package rather than of one slice, so adding formulas,
#: the run and storage to this tuple makes them broader, not weaker.
ALL_MODULES = (
    "__init__.py",
    "schema.py",
    "fingerprint.py",
    "bindings.py",
    "formulas.py",
    "run.py",
    "storage.py",
)

#: The modules that must touch no filesystem. `storage.py` is the one place in
#: the package that writes, which is the whole reason it is a separate module:
#: everything else stays pure and the write path is reviewable in one file.
NON_WRITING_MODULES = tuple(name for name in ALL_MODULES if name != "storage.py")

#: The modules that must contain no division at all. `formulas.py` is the one
#: place a metric value is derived, and `value = numerator / denominator` is
#: exactly what it exists to do — so the guard that used to say "this package
#: computes nothing" now says "only the formulas compute", which is the property
#: 10.4b needs to keep. `storage.py` is excluded here and checked separately
#: below: its only `/` operators are `Path` joins, which are not arithmetic, and
#: the companion test pins exactly which ones they are.
NON_COMPUTING_MODULES = tuple(
    name for name in ALL_MODULES if name not in ("formulas.py", "storage.py")
)


def _imports(path: Path) -> dict[str, set[str]]:
    """Every module this file imports, read from the syntax tree.

    Not from lines starting with `import`, because these modules *document* what
    they refuse to import.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.setdefault(alias.name, set())
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = "." * node.level + module
            found.setdefault(module, set()).update(alias.name for alias in node.names)
    return found


def test_the_package_never_imports_the_human_labelling_layer() -> None:
    for name in ALL_MODULES:
        for module in _imports(PACKAGE_ROOT / name):
            assert "labeling" not in module, f"{name}: {module}"


def test_the_package_never_imports_the_ranking_metrics_package() -> None:
    for name in ALL_MODULES:
        for module in _imports(PACKAGE_ROOT / name):
            assert not module.startswith("evaluation.metrics"), f"{name}: {module}"


def test_no_public_object_can_carry_a_human_label() -> None:
    from dataclasses import is_dataclass

    forbidden = ("labelset", "label_protocol", "judged", "grade", "unjudged", "rubric")
    for name in dir(business_package):
        item = getattr(business_package, name)
        if isinstance(item, type) and is_dataclass(item):
            for field in fields(item):
                for word in forbidden:
                    assert word not in field.name.lower(), f"{name}.{field.name}"


def test_the_pure_modules_open_no_database_and_no_socket() -> None:
    for name in PURE_MODULES:
        for module in _imports(PACKAGE_ROOT / name):
            lowered = module.lower()
            for forbidden in (
                "sqlite",
                "libsql",
                "httpx",
                "requests",
                "urllib.request",
                "socket",
                "yaml",
                "services.collector.database",
            ):
                assert forbidden not in lowered, f"{name}: {module}"


def test_only_bindings_accepts_a_database_connection() -> None:
    for name in PURE_MODULES:
        tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert "connection" not in [item.arg for item in node.args.args]
    takers = [
        node.name
        for node in ast.walk(
            ast.parse((PACKAGE_ROOT / "bindings.py").read_text(encoding="utf-8"))
        )
        if isinstance(node, ast.FunctionDef)
        and "connection" in [item.arg for item in node.args.args]
    ]
    assert takers == ["build_profile_target_binding"]


def test_no_module_in_the_package_makes_a_network_request() -> None:
    for name in ALL_MODULES:
        for module in _imports(PACKAGE_ROOT / name):
            assert not module.startswith(
                ("httpx", "requests", "socket", "http.client", "urllib.request")
            ), f"{name}: {module}"


def test_only_the_url_parser_comes_from_urllib() -> None:
    urllib_modules = {
        module: names
        for module, names in _imports(PACKAGE_ROOT / "schema.py").items()
        if module.startswith("urllib")
    }
    assert urllib_modules == {"urllib.parse": {"urlsplit"}}


def test_the_package_writes_nothing_outside_storage() -> None:
    for name in NON_WRITING_MODULES:
        tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                label = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert label not in (
                    "open",
                    "write_text",
                    "write_bytes",
                    "mkdir",
                    "unlink",
                    "commit",
                ), f"{name}: {label}"


def test_production_never_depends_on_the_business_metrics_package() -> None:
    offenders = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in (REPOSITORY_ROOT / "services").rglob("*.py")
        if "business_metrics" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_only_the_formulas_compute_anything() -> None:
    """The contract, the identities and the bindings still do no arithmetic.

    Phase 10.4a asserted that *nothing* in the package divided, because no
    formula existed. 10.4b adds exactly one module that may, and this guard is
    the same rule with one door in it: a division appearing in `schema.py`,
    `bindings.py`, `run.py` or `storage.py` would mean a second place where a
    rate can be produced — which is precisely the drift the closed resolver
    registry and the single public `compute_business_metric` exist to prevent.
    """
    for name in NON_COMPUTING_MODULES:
        tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Div, ast.FloorDiv)
            ):
                raise AssertionError(f"{name} divides something")


def test_every_slash_in_storage_is_a_path_join() -> None:
    """`storage.py` computes nothing either; its `/` operators join paths.

    Checked by pinning the left-hand operand of every division in the module,
    rather than by exempting the file. A real arithmetic expression — a rate
    recomputed while writing, a count divided while reading — would introduce an
    operand outside this set and fail here.
    """
    source = (PACKAGE_ROOT / "storage.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    operands = {
        ast.get_source_segment(source, node.left)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.FloorDiv))
    }
    assert operands == {"Path(root)", "root_path", "directory"}, operands


def test_the_business_package_reuses_the_phase_10_1_error_hierarchy() -> None:
    assert issubclass(BusinessMetricsError, EvaluationDatasetError)
    for error in (
        BusinessMetricContractError,
        BusinessMetricBindingError,
        BusinessMetricArgumentError,
        BusinessEvidenceClassError,
    ):
        assert issubclass(error, BusinessMetricsError)


def test_no_low_level_exception_escapes_a_public_binding_function() -> None:
    from evaluation.business_metrics import bindings as bindings_module

    for name in bindings_module.__all__:
        item = getattr(bindings_module, name)
        if not callable(item) or isinstance(item, type):
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(item)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                raised = getattr(node.exc.func, "id", "")
                assert raised in (
                    "BusinessMetricBindingError",
                    "BusinessMetricContractError",
                    "BusinessMetricArgumentError",
                    "BusinessEvidenceClassError",
                ), f"{name} raises {raised}"


def test_no_low_level_exception_escapes_the_phase_10_4b_modules() -> None:
    """Every refusal in the computing modules is one of this package's errors.

    The guard above walks `bindings.py`'s public functions; this one walks the
    whole source of the three modules Phase 10.4b adds, because their failure
    paths run over records, JSON documents and the filesystem — exactly the
    places a `KeyError`, a `ValueError` or an `OSError` would otherwise leak
    through a public boundary.
    """
    permitted = {
        "BusinessMetricBindingError",
        "BusinessMetricContractError",
        "BusinessMetricArgumentError",
        "BusinessEvidenceClassError",
    }
    for name in ("formulas.py", "run.py", "storage.py"):
        tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                raised = getattr(node.exc.func, "id", "")
                assert raised in permitted, f"{name} raises {raised}"


def test_the_business_contract_shares_no_object_with_the_ranking_contract() -> None:
    """Siblings, not layers. A shared *name* is allowed; a shared object is not."""
    import evaluation.metrics as ranking_package

    shared = set(business_package.__all__) & set(ranking_package.__all__)
    assert shared == {"validate_fingerprint"}
    for name in shared:
        assert getattr(business_package, name) is not getattr(ranking_package, name)


# ====================================================================
# REVIEW REGRESSIONS — v2 and v3 findings (still enforced)
# ====================================================================


def test_wrong_metric_universe_pairs_are_refused() -> None:
    with pytest.raises(BusinessMetricContractError, match="is defined over"):
        computed(
            BusinessMetricName.BROKEN_URL_RATE,
            universe=BusinessMetricUniverse.FROZEN_COHORT,
        )
    with pytest.raises(BusinessMetricContractError, match="is defined over"):
        computed(
            BusinessMetricName.DATA_AI_RATE,
            universe=BusinessMetricUniverse.URL_AUDITED_UNIVERSE,
        )
    with pytest.raises(BusinessMetricContractError, match="is defined over"):
        computed(
            BusinessMetricName.APPLIED_DUPLICATE_RATE,
            universe=BusinessMetricUniverse.FROZEN_COHORT,
        )
    for metric in BusinessMetricName:
        declared = metric_definition(metric).universe
        for universe in BusinessMetricUniverse:
            if universe is declared:
                assert require_metric_universe(metric, universe) is universe
            else:
                with pytest.raises(BusinessMetricContractError):
                    require_metric_universe(metric, universe)


def test_a_snapshot_metric_keeps_its_class_in_a_run_full_of_other_evidence() -> None:
    run = full_context()
    snapshot = computed(BusinessMetricName.DATA_AI_RATE, run_context=run)
    assert snapshot.evidence_class is BusinessEvidenceClass.SNAPSHOT_DESCRIPTIVE
    view = project_metric_evidence(
        run, BusinessMetricKey(BusinessMetricName.DATA_AI_RATE)
    )
    for member in BusinessEvidenceMember:
        assert not view.has(member), member


def test_an_unused_url_audit_cannot_move_a_snapshot_metric_s_identity() -> None:
    binding = cohort()
    march = full_context(binding, url_audit_binding=url_audit(binding))
    june = full_context(
        binding,
        url_audit_binding=url_audit(binding, audit_started_at="2026-06-01T08:00:00Z"),
    )
    assert march.run_context_fingerprint != june.run_context_fingerprint
    first = computed(run_context=march)
    second = computed(run_context=june)
    assert first.scope_fingerprint == second.scope_fingerprint
    assert first.evidence_fingerprint == second.evidence_fingerprint
    audited_march = computed(BusinessMetricName.BROKEN_URL_RATE, run_context=march)
    audited_june = computed(BusinessMetricName.BROKEN_URL_RATE, run_context=june)
    assert audited_march.evidence_fingerprint != audited_june.evidence_fingerprint


def test_a_scope_carrying_a_binding_its_metric_never_reads_is_refused() -> None:
    scope = project_metric_scope(
        full_context(),
        BusinessMetricKey(BusinessMetricName.DATA_AI_RATE),
        BusinessMetricUniverse.FROZEN_COHORT,
    )
    polluted = reseal_scope(scope, url_audit_binding=url_audit())
    with pytest.raises(BusinessMetricContractError, match="does not use"):
        verify_business_metric_scope_fingerprint(polluted)
    with pytest.raises(BusinessMetricContractError, match="does not use"):
        validate_business_metric_scope_structure(polluted)


def test_scope_and_evidence_must_name_the_same_artefacts() -> None:
    from evaluation.business_metrics import verify_metric_scope_and_evidence_agree

    binding = cohort()
    key = BusinessMetricKey(BusinessMetricName.BROKEN_URL_RATE)
    scope = project_metric_scope(
        context(binding, url_audit_binding=url_audit(binding)),
        key,
        BusinessMetricUniverse.URL_AUDITED_UNIVERSE,
    )
    evidence = project_metric_evidence(
        context(
            binding,
            url_audit_binding=url_audit(
                binding, audit_started_at="2026-06-01T08:00:00Z"
            ),
        ),
        key,
    )
    with pytest.raises(BusinessMetricBindingError, match="different artefacts"):
        verify_metric_scope_and_evidence_agree(scope, evidence)


def test_the_frozen_verdict_table_is_enforced() -> None:
    assert BROKEN_STATUS_CODES == frozenset({404, 410})
    assert outcome_for_status(200) is UrlAuditOutcome.VALID
    assert outcome_for_status(410) is UrlAuditOutcome.BROKEN
    with pytest.raises(BusinessMetricContractError, match="reads that status as"):
        UrlAuditObservation(
            requested_url=ALPHA,
            attempted=True,
            outcome=UrlAuditOutcome.VALID,
            status_code=404,
            audited_at=CAMPAIGN_AT,
        )
    with pytest.raises(BusinessMetricContractError, match="reads that status as"):
        UrlAuditObservation(
            requested_url=ALPHA,
            attempted=True,
            outcome=UrlAuditOutcome.BROKEN,
            status_code=200,
            audited_at=CAMPAIGN_AT,
        )


@pytest.mark.parametrize("code", [401, 403, 407, 429, 400, 500, 503, 302, 100])
def test_a_refusal_or_a_failure_is_never_broken(code) -> None:
    with pytest.raises(BusinessMetricContractError, match="reads that status as"):
        UrlAuditObservation(
            requested_url=ALPHA,
            attempted=True,
            outcome=UrlAuditOutcome.BROKEN,
            status_code=code,
            audited_at=CAMPAIGN_AT,
        )
    assert (
        UrlAuditObservation(
            requested_url=ALPHA,
            attempted=True,
            outcome=UrlAuditOutcome.INCONCLUSIVE,
            status_code=code,
            reason=INCONCLUSIVE_STATUS_REASONS[code],
            audited_at=CAMPAIGN_AT,
        ).conclusive
        is False
    )


def test_an_attempted_observation_needs_a_reproducible_timestamp() -> None:
    with pytest.raises(BusinessMetricBindingError, match="states no audited_at"):
        UrlAuditObservation(
            requested_url=ALPHA,
            attempted=True,
            outcome=UrlAuditOutcome.VALID,
            status_code=200,
        )
    for bad in ("2026-03-01 09:00:00", "2026-03-01T09:00:00", "yesterday"):
        with pytest.raises(BusinessMetricBindingError, match="RFC 3339|real instant"):
            UrlAuditObservation(
                requested_url=ALPHA,
                attempted=True,
                outcome=UrlAuditOutcome.VALID,
                status_code=200,
                audited_at=bad,
            )


def test_the_url_audit_policy_is_frozen() -> None:
    assert URL_AUDIT_POLICY_VERSION == "business-url-audit-policy-v1"
    assert URL_AUDIT_METHOD == "GET"
    assert URL_AUDIT_TIMEOUT_SECONDS == 20
    assert URL_AUDIT_RETRIES == 0
    assert URL_AUDIT_MAX_REDIRECTS == 3
    assert "OpportunityRadarAI" in URL_AUDIT_USER_AGENT
    with pytest.raises(
        BusinessMetricContractError, match="unsupported URL audit policy"
    ):
        url_audit(cohort(), audit_policy_version="invented-url-audit-v1")


def test_there_is_no_generic_not_attempted_reason() -> None:
    names = {str(item) for item in UrlAuditInconclusiveReason}
    assert "NOT_ATTEMPTED" not in names
    assert {str(item) for item in UNATTEMPTED_REASONS} == {
        "INVALID_URL",
        "UNSUPPORTED_SCHEME",
        "UNSAFE_DESTINATION",
        "ROBOTS_DISALLOWED",
        "ROBOTS_UNRESOLVED",
        "ROBOTS_BARRIER",
    }


def test_the_reason_must_fit_the_url_shape() -> None:
    assert url_shape(ALPHA) is UrlShape.HTTP
    assert url_shape("mailto:jobs@example.com") is UrlShape.NON_HTTP_SCHEME
    assert url_shape("/careers/3") is UrlShape.NO_SCHEME
    assert is_http_url(ALPHA) is True
    for reason in (
        UrlAuditInconclusiveReason.INVALID_URL,
        UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME,
    ):
        with pytest.raises(BusinessMetricContractError, match="ordinary http"):
            UrlAuditObservation(
                requested_url=ALPHA,
                attempted=False,
                outcome=UrlAuditOutcome.INCONCLUSIVE,
                reason=reason,
            )
    with pytest.raises(BusinessMetricContractError, match="NON_HTTP_SCHEME"):
        UrlAuditObservation(
            requested_url="mailto:jobs@example.com",
            attempted=False,
            outcome=UrlAuditOutcome.INCONCLUSIVE,
            reason=UrlAuditInconclusiveReason.INVALID_URL,
        )
    with pytest.raises(BusinessMetricContractError, match="NO_SCHEME"):
        UrlAuditObservation(
            requested_url="/careers/3",
            attempted=False,
            outcome=UrlAuditOutcome.INCONCLUSIVE,
            reason=UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME,
        )


def test_an_all_unattempted_audit_still_carries_a_campaign_timestamp() -> None:
    binding = cohort()
    audit = url_audit(
        binding,
        observations=tuple(
            observation(url, outcome=UrlAuditOutcome.INCONCLUSIVE, attempted=False)
            for url in binding.expected_action_urls
        ),
    )
    assert audit.attempted_count == 0
    assert all(item.audited_at is None for item in audit.observations)
    assert audit.audit_started_at == CAMPAIGN_AT
    later = url_audit(
        binding,
        observations=audit.observations,
        audit_started_at="2026-09-01T08:00:00Z",
    )
    assert later.binding_fingerprint != audit.binding_fingerprint
    with pytest.raises(BusinessMetricBindingError, match="campaign timestamp"):
        url_audit(binding, audit_started_at="2026-03-02")


def test_url_completeness_cannot_be_skipped() -> None:
    binding = cohort()
    with pytest.raises(BusinessMetricBindingError, match="omits 1 eligible URL"):
        url_audit(binding, observations=(observation(ALPHA),))
    assert set(inspect.signature(verify_business_metric_run_context).parameters) == {
        "context"
    }
    assert set(inspect.signature(assert_url_audit_covers).parameters) == {
        "binding",
        "cohort_binding",
    }
    with pytest.raises(BusinessMetricBindingError, match="does not imply"):
        url_audit(
            binding,
            observations=(
                observation(ALPHA),
                observation(BETA),
                observation("https://gamma.example/3"),
            ),
        )


def test_a_result_cannot_be_sealed_on_a_partial_audit_even_past_the_context() -> None:
    """A partial audit with a valid fingerprint is refused at the result too."""
    from evaluation.business_metrics import url_audit_binding_fingerprint

    binding = cohort()
    run = full_context(binding)
    partial = replace(
        run.url_audit_binding,
        observations=(observation(ALPHA),),
        declared_universe_size=1,
    )
    resealed_audit = replace(
        partial, binding_fingerprint=url_audit_binding_fingerprint(partial)
    )
    assert verify_url_audit_binding_fingerprint(resealed_audit) == (
        resealed_audit.binding_fingerprint
    )
    tampered = replace(
        run,
        url_audit_binding=resealed_audit,
        run_context_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    tampered = replace(
        tampered,
        run_context_fingerprint=business_metric_run_context_fingerprint(tampered),
    )
    with pytest.raises(
        BusinessMetricBindingError, match="omits 1 eligible URL|declares a universe of 1"
    ):
        computed(BusinessMetricName.BROKEN_URL_RATE, run_context=tampered)


def test_universe_sizes_are_checked_against_their_artefacts() -> None:
    with pytest.raises(BusinessMetricBindingError, match="holds 3 member"):
        computed(BusinessMetricName.CORE_DATA_AI_RATE, universe_size=394)
    with pytest.raises(BusinessMetricBindingError, match="holds 6 member"):
        computed(BusinessMetricName.APPLIED_DUPLICATE_RATE, universe_size=COHORT_SIZE)
    with pytest.raises(BusinessMetricBindingError, match="holds 2 member"):
        computed(BusinessMetricName.BROKEN_URL_RATE, universe_size=7)
    with pytest.raises(BusinessMetricBindingError, match="holds 2 member"):
        computed(BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE, universe_size=5)


def test_a_wrong_universe_size_is_caught_on_verification_too() -> None:
    run = full_context()
    good = computed(run_context=run)
    forged = replace(
        good,
        support=BusinessMetricSupport(numerator=1, denominator=3, universe_size=394),
    )
    resealed = replace(
        forged, result_fingerprint=business_metric_result_fingerprint(forged)
    )
    with pytest.raises(BusinessMetricBindingError, match="holds 3 member"):
        verify_business_metric_result(resealed, context=run)


def test_selection_is_not_validation() -> None:
    records = (
        record(1, application_url=ALPHA),
        record(2, application_url="mailto:jobs@example.com"),
        record(3, source_url="/careers/3"),
    )
    binding = cohort(records)
    # First occurrence in the snapshot's canonical order, not lexical order.
    assert binding.expected_action_urls == (
        ALPHA,
        "mailto:jobs@example.com",
        "/careers/3",
    )
    audit = url_audit(
        binding,
        observations=(
            observation(ALPHA),
            UrlAuditObservation(
                requested_url="mailto:jobs@example.com",
                attempted=False,
                outcome=UrlAuditOutcome.INCONCLUSIVE,
                reason=UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME,
            ),
            UrlAuditObservation(
                requested_url="/careers/3",
                attempted=False,
                outcome=UrlAuditOutcome.INCONCLUSIVE,
                reason=UrlAuditInconclusiveReason.INVALID_URL,
            ),
        ),
    )
    assert audit.declared_universe_size == 3
    assert audit.conclusive_count == 1


def test_the_frozen_action_url_protocol() -> None:
    assert FROZEN_ACTION_URL_PRECEDENCE == (
        "application_url",
        "source_url",
        "canonical_url",
    )
    assert FROZEN_ACTION_URL_PROTOCOL_VERSION == "frozen-action-url-v1"
    assert (
        frozen_action_url(
            record(1, application_url=ALPHA, source_url=BETA, canonical_url=BETA)
        )
        == ALPHA
    )
    assert frozen_action_url(record(2, source_url=BETA, canonical_url=ALPHA)) == BETA
    assert frozen_action_url(record(3, canonical_url=ALPHA)) == ALPHA
    assert frozen_action_url(record(4)) is None
    assert (
        frozen_action_url(
            record(5, application_url="   ", source_url="", canonical_url=ALPHA)
        )
        == ALPHA
    )
    assert frozen_action_url(record(6, application_url=f"  {ALPHA} ")) == ALPHA
    with pytest.raises(BusinessMetricBindingError, match="not a URL field"):
        frozen_action_url(record(7, application_url=42))
    with pytest.raises(BusinessMetricBindingError, match="not a mapping"):
        expected_frozen_action_urls([record(1), "not a record"])


def test_the_target_match_rate_denominator_is_the_whole_cohort() -> None:
    result = computed(
        BusinessMetricName.TARGET_COUNTRY_MATCH_RATE, value=1 / 3
    )
    assert result.support.denominator == result.support.universe_size == COHORT_SIZE
    with pytest.raises(BusinessMetricContractError, match="whole universe"):
        computed(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            support=BusinessMetricSupport(
                numerator=1,
                denominator=2,
                universe_size=COHORT_SIZE,
                target_verdict_breakdown=TargetVerdictBreakdown(1, 1, 1),
            ),
            value=0.5,
        )


def test_a_composition_metric_has_no_legitimate_refusal_at_all() -> None:
    for metric in (
        BusinessMetricName.DATA_AI_RATE,
        BusinessMetricName.DESCRIPTION_PRESENCE_RATE,
    ):
        assert metric_definition(metric).permitted_reasons == frozenset()
        for reason in BusinessMetricUnavailableReason:
            with pytest.raises(BusinessMetricContractError, match="may not state"):
                require_permitted_reason(metric, reason)


def test_a_reason_belonging_to_another_metric_is_refused() -> None:
    with pytest.raises(BusinessMetricContractError, match="may not state"):
        unavailable(
            BusinessMetricName.DATA_AI_RATE,
            BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING,
        )


def test_target_country_match_rate_states_the_right_refusal() -> None:
    no_binding = context()
    assert (
        unavailable(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING,
            run_context=no_binding,
        ).reason
        is BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING
    )
    with pytest.raises(BusinessMetricContractError, match="does not hold"):
        unavailable(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN,
            run_context=no_binding,
        )
    unknown = context(
        profile_target_binding=target_binding(
            country_code=None, rule_id="mobility-open-v1"
        )
    )
    assert (
        unavailable(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN,
            run_context=unknown,
        ).reason
        is BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN
    )
    with pytest.raises(BusinessMetricContractError, match="names no country"):
        computed(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            run_context=unknown,
            value=0.0,
        )


def test_opportunity_skill_coverage_can_never_be_computed() -> None:
    assert ALWAYS_UNAVAILABLE_METRICS == {
        BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE: (
            BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN
        )
    }
    with pytest.raises(BusinessMetricContractError, match="cannot be COMPUTED"):
        computed(BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE)
    assert (
        unavailable(
            BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE,
            BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN,
        ).status
        is BusinessMetricStatus.N_A
    )


def test_broken_url_rate_needs_a_conclusive_observation() -> None:
    binding = cohort()
    nothing_conclusive = context(
        binding,
        url_audit_binding=url_audit(
            binding,
            observations=tuple(
                observation(url, outcome=UrlAuditOutcome.INCONCLUSIVE, attempted=False)
                for url in binding.expected_action_urls
            ),
        ),
    )
    with pytest.raises(
        BusinessMetricContractError, match="no observation was conclusive"
    ):
        computed(BusinessMetricName.BROKEN_URL_RATE, run_context=nothing_conclusive)
    assert (
        unavailable(
            BusinessMetricName.BROKEN_URL_RATE,
            BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT,
            run_context=nothing_conclusive,
        ).support.universe_size
        == AUDITED_SIZE
    )
    for metric in (
        BusinessMetricName.URL_AUDIT_COVERAGE_RATE,
        BusinessMetricName.URL_CONCLUSIVE_COVERAGE_RATE,
    ):
        result = computed(
            metric, run_context=nothing_conclusive, numerator=0, value=0.0
        )
        assert result.status is BusinessMetricStatus.COMPUTED


def test_an_absent_member_is_reported_as_its_own_absence() -> None:
    bare = context()
    for metric, reason in (
        (
            BusinessMetricName.BROKEN_URL_RATE,
            BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING,
        ),
        (
            BusinessMetricName.APPLIED_DUPLICATE_RATE,
            BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING,
        ),
        (
            BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE,
            BusinessMetricUnavailableReason.DECLARED_SOURCE_UNIVERSE_MISSING,
        ),
        (
            BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
            BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING,
        ),
    ):
        declared = metric_definition(metric).universe
        size = COHORT_SIZE if declared is BusinessMetricUniverse.FROZEN_COHORT else None
        assert (
            unavailable(metric, reason, run_context=bare, universe_size=size).reason
            is reason
        )
    with pytest.raises(BusinessMetricContractError, match="does not hold"):
        unavailable(
            BusinessMetricName.BROKEN_URL_RATE,
            BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT,
            run_context=bare,
            universe_size=None,
        )


def test_a_source_map_and_a_benchmark_of_another_country_are_refused() -> None:
    run = full_context(
        declared_source_universe=declared_universe(
            entries=(_FakeEntry("alpha_board"),), scope_country="FR"
        ),
        benchmark_binding=benchmark(scope_country="MA"),
    )
    with pytest.raises(BusinessMetricBindingError, match="two different markets"):
        unavailable(
            BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL,
            BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY,
            run_context=run,
            universe_size=1,
        )


def test_source_discovery_recall_needs_both_the_map_and_a_ready_benchmark() -> None:
    with pytest.raises(BusinessEvidenceClassError, match="evaluation_ready=False"):
        computed(BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL)
    ready = full_context(benchmark_binding=benchmark(ready=True, rows=60, target=60))
    assert (
        computed(
            BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL, run_context=ready
        ).evidence_class
        is BusinessEvidenceClass.EVALUATION_READY_BENCHMARK
    )
    # The public path always computes a digest, so a ready benchmark without one
    # can only be assembled by hand — and does not survive re-validation.
    with pytest.raises(BusinessMetricBindingError, match="states no records_finger"):
        validate_benchmark_binding_structure(
            replace(
                benchmark(ready=True, rows=60, target=60), records_fingerprint=None
            )
        )
    with pytest.raises(BusinessMetricBindingError, match="contradicts itself"):
        benchmark(ready=True, rows=2, target=60)


def test_the_real_benchmark_manifest_is_not_evaluation_ready() -> None:
    import json

    payload = json.loads(
        (
            REPOSITORY_ROOT / "evaluation/benchmarks/morocco_pfe_gold_v1.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["evaluation_ready"] is False


def test_a_run_holds_results_of_several_evidence_classes_at_once() -> None:
    run = full_context()
    ordered = verify_business_metric_results(
        [
            computed(BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION, run_context=run),
            computed(BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE, run_context=run),
            computed(BusinessMetricName.BROKEN_URL_RATE, run_context=run),
            computed(BusinessMetricName.DATA_AI_RATE, run_context=run),
        ],
        context=run,
    )
    assert [str(item.key.metric) for item in ordered] == [
        "ACTIVE_DECLARED_SOURCE_RATE",
        "BROKEN_URL_RATE",
        "DATA_AI_RATE",
        "OBSERVED_SOURCE_CONTRIBUTION",
    ]
    assert {str(item.evidence_class) for item in ordered} == {
        "SNAPSHOT_DESCRIPTIVE",
        "EXTERNAL_AUDIT_DESCRIPTIVE",
        "DECLARED_UNIVERSE_DESCRIPTIVE",
    }


def test_a_projected_scope_and_evidence_always_agree() -> None:
    from evaluation.business_metrics import verify_metric_scope_and_evidence_agree

    run = full_context()
    for metric in BusinessMetricName:
        key = key_of(metric)
        verify_metric_scope_and_evidence_agree(
            project_metric_scope(run, key, metric_definition(metric).universe),
            project_metric_evidence(run, key),
        )


def test_malformed_arguments_never_leak_a_low_level_error() -> None:
    run = context()
    support = BusinessMetricSupport(
        numerator=1, denominator=COHORT_SIZE, universe_size=COHORT_SIZE
    )
    with pytest.raises(BusinessMetricContractError, match="not a business metric key"):
        _build_business_metric_result(
            key="DATA_AI_RATE",
            status=BusinessMetricStatus.COMPUTED,
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            context=run,
            support=support,
            value=0.5,
        )
    with pytest.raises(
        BusinessMetricContractError, match="not a business metric status"
    ):
        _build_business_metric_result(
            key=BusinessMetricKey(BusinessMetricName.CORE_DATA_AI_RATE),
            status="COMPUTED",
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            context=run,
            support=support,
            value=0.5,
        )
    with pytest.raises(BusinessMetricContractError, match="not a business metric sup"):
        _build_business_metric_result(
            key=BusinessMetricKey(BusinessMetricName.CORE_DATA_AI_RATE),
            status=BusinessMetricStatus.COMPUTED,
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            context=run,
            support={"numerator": 1},
            value=0.5,
        )
    with pytest.raises(BusinessMetricBindingError, match="not a URL audit obs"):
        url_audit(cohort(), observations=(observation(ALPHA), object()))


def test_a_forged_run_context_fingerprint_is_refused() -> None:
    forged = replace(context(), run_context_fingerprint="7" * 64)
    with pytest.raises(BusinessMetricBindingError, match="is not what it says"):
        verify_business_metric_run_context(forged)


# ====================================================================
# REVIEW REGRESSIONS — v4 findings
# ====================================================================

# ------------- 1. the snapshot is verified, not believed ------------------


def test_the_cohort_binding_recomputes_the_phase_10_1_content_fingerprint() -> None:
    """The finding: `content_fingerprint` was read as a fact about the snapshot."""
    forged = manifest_payload(DEFAULT_RECORDS, forge_content_fingerprint="f" * 64)
    with pytest.raises(BusinessMetricBindingError, match="is not what it says"):
        build_frozen_cohort_binding(DEFAULT_RECORDS, forged)


def test_a_record_edited_after_the_manifest_was_written_is_caught() -> None:
    payload = manifest_payload(DEFAULT_RECORDS)
    tampered = (
        replace_record(DEFAULT_RECORDS[0], canonical_url="https://edited.example/x"),
    ) + DEFAULT_RECORDS[1:]
    with pytest.raises(BusinessMetricBindingError, match="is not what it says"):
        build_frozen_cohort_binding(tampered, payload)


def replace_record(item, **changes):
    updated = dict(item)
    updated.update(changes)
    return updated


def test_an_unsupported_schema_or_cohort_version_is_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="schema version"):
        build_frozen_cohort_binding(
            DEFAULT_RECORDS,
            manifest_payload(DEFAULT_RECORDS, schema_version="evaluation-dataset-v2"),
        )
    with pytest.raises(BusinessMetricBindingError, match="cohort version"):
        build_frozen_cohort_binding(
            DEFAULT_RECORDS,
            manifest_payload(DEFAULT_RECORDS, cohort_version="evaluation-cohort-v1"),
        )


def test_a_dataset_id_edited_apart_from_its_content_is_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="is not the identifier"):
        build_frozen_cohort_binding(
            DEFAULT_RECORDS,
            manifest_payload(DEFAULT_RECORDS, forge_dataset_id="evaluation-dataset-v3-deadbeefdeadbeef"),
        )


def test_a_record_count_that_disagrees_with_the_records_is_refused() -> None:
    payload = manifest_payload(DEFAULT_RECORDS)
    payload["record_count"] = 99
    with pytest.raises(BusinessMetricBindingError, match="is not what it says|disagree"):
        build_frozen_cohort_binding(DEFAULT_RECORDS, payload)


def test_the_cohort_binding_takes_only_records_and_a_manifest() -> None:
    assert set(inspect.signature(build_frozen_cohort_binding).parameters) == {
        "records",
        "manifest",
    }
    assert not any(
        "verified" in field.name for field in fields(type(cohort()))
    )


def test_the_cohort_binding_derives_every_fact_it_carries() -> None:
    binding = cohort()
    assert binding.cohort_size == COHORT_SIZE
    assert binding.profile_id == PROFILE_ID
    assert binding.profile_fingerprint == PROFILE_FINGERPRINT
    assert binding.dataset_generated_at == GENERATED_AT
    assert binding.freshness_as_of_date == AS_OF_DATE
    assert binding.expected_action_urls == (ALPHA, BETA)
    assert binding.absorbed_duplicate_total == 3
    assert binding.records_with_absorbed_duplicates == 2
    assert binding.declared_merged_duplicate_count == 3
    assert binding.represented_universe_size == PRE_DEDUP_SIZE
    assert verify_frozen_cohort_binding_fingerprint(binding) == (
        binding.binding_fingerprint
    )
    assert frozen_cohort_binding_fingerprint(binding) == binding.binding_fingerprint


def test_calendar_date_of_reads_the_date_and_no_clock() -> None:
    assert calendar_date_of(GENERATED_AT, subject="x") == AS_OF_DATE
    with pytest.raises(BusinessMetricBindingError):
        calendar_date_of("2026-03-01", subject="x")


# ------------- 2. the profile identity belongs to the snapshot ------------


def test_the_run_context_takes_no_profile_identity() -> None:
    """The finding: a caller could state whose run this was."""
    parameters = set(
        inspect.signature(build_business_metric_run_context).parameters
    )
    assert "profile_id" not in parameters
    assert "profile_fingerprint" not in parameters
    run = context()
    assert run.profile_id == PROFILE_ID
    assert run.profile_fingerprint == PROFILE_FINGERPRINT


def test_the_profile_is_stated_once_and_carried_everywhere() -> None:
    """Derived identity is not repeated identity.

    The cohort binding is the one place a profile is named; the run context, the
    per-metric scope and the per-metric evidence view all read it from there.
    So none of their canonical payloads may restate it — a second copy is a
    second thing that can disagree with the first.
    """
    run = full_context()
    key = BusinessMetricKey(BusinessMetricName.CORE_DATA_AI_RATE)
    scope = project_metric_scope(run, key, metric_definition(key.metric).universe)
    evidence = project_metric_evidence(run, key)

    assert run.profile_id == scope.profile_id == evidence.profile_id == PROFILE_ID
    assert (
        run.profile_fingerprint
        == scope.profile_fingerprint
        == evidence.profile_fingerprint
        == PROFILE_FINGERPRINT
    )

    for payload in (
        canonical_business_metric_run_context_payload(run),
        canonical_business_metric_scope_payload(scope),
        canonical_business_metric_evidence_payload(evidence),
    ):
        assert "profile_id" not in payload
        assert "profile_fingerprint" not in payload
        assert payload["frozen_cohort_binding_fingerprint"] == (
            run.frozen_cohort_binding.binding_fingerprint
        )
    # The single statement of the profile is inside the binding's own payload,
    # which is what that fingerprint covers.
    binding_payload = canonical_frozen_cohort_binding_payload(
        run.frozen_cohort_binding
    )
    assert binding_payload["profile_id"] == PROFILE_ID
    assert binding_payload["profile_fingerprint"] == PROFILE_FINGERPRINT


def test_a_run_cannot_be_assembled_for_another_profile() -> None:
    """Profile A's snapshot, profile B's target: impossible by API and refused."""
    binding_a = cohort()
    assert binding_a.profile_id == PROFILE_ID
    foreign = target_binding(
        profile_id=OTHER_PROFILE_ID, profile_fingerprint=OTHER_PROFILE_FINGERPRINT
    )
    with pytest.raises(BusinessMetricBindingError, match="profile state this snapshot"):
        context(binding_a, profile_target_binding=foreign)
    # ...and a cohort built for profile B is simply a different cohort.
    binding_b = cohort(
        profile_id=OTHER_PROFILE_ID, profile_fingerprint=OTHER_PROFILE_FINGERPRINT
    )
    assert binding_b.profile_id == OTHER_PROFILE_ID
    assert binding_b.binding_fingerprint != binding_a.binding_fingerprint


class _RefusingConnection:
    """A connection that fails if anything tries to use it as one."""

    def execute(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the business metrics package executed SQL")

    def cursor(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the business metrics package opened a cursor")


class _Target:
    def __init__(self, country_code, rule_id, profile_id=PROFILE_ID):
        self.profile_id = profile_id
        self.country_code = country_code
        self.rule_id = rule_id


def _patch_profile(monkeypatch, *, fingerprint, country_code, rule_id):
    import evaluation.business_metrics.bindings as bindings_module

    monkeypatch.setattr(
        bindings_module,
        "profile_context_fingerprint",
        lambda connection, profile_id: fingerprint,
    )
    monkeypatch.setattr(
        bindings_module,
        "resolve_profile_target",
        lambda connection, profile_id: _Target(country_code, rule_id),
    )


def test_the_target_builder_takes_the_cohort_not_a_profile(monkeypatch) -> None:
    assert set(inspect.signature(build_profile_target_binding).parameters) == {
        "connection",
        "cohort_binding",
    }
    _patch_profile(
        monkeypatch,
        fingerprint=PROFILE_FINGERPRINT,
        country_code="MA",
        rule_id=RESTRICTED_RULE,
    )
    binding = build_profile_target_binding(_RefusingConnection(), cohort())
    assert binding.profile_id == PROFILE_ID
    assert binding.profile_fingerprint == PROFILE_FINGERPRINT
    assert binding.country_code == "MA"


def test_a_moved_profile_fingerprint_is_an_integrity_failure(monkeypatch) -> None:
    _patch_profile(
        monkeypatch,
        fingerprint=OTHER_PROFILE_FINGERPRINT,
        country_code="MA",
        rule_id=RESTRICTED_RULE,
    )
    with pytest.raises(BusinessMetricBindingError, match="has changed since the"):
        build_profile_target_binding(_RefusingConnection(), cohort())


def test_a_target_binding_with_no_country_is_still_coherent(monkeypatch) -> None:
    _patch_profile(
        monkeypatch,
        fingerprint=PROFILE_FINGERPRINT,
        country_code=None,
        rule_id="mobility-open-v1",
    )
    binding = build_profile_target_binding(_RefusingConnection(), cohort())
    assert binding.country_code is None
    bound = project_metric_scope(
        context(profile_target_binding=binding),
        BusinessMetricKey(BusinessMetricName.TARGET_COUNTRY_MATCH_RATE),
        BusinessMetricUniverse.FROZEN_COHORT,
    )
    assert bound.target_country_code is None


def test_a_resolver_returning_no_rule_is_refused(monkeypatch) -> None:
    import evaluation.business_metrics.bindings as bindings_module

    monkeypatch.setattr(
        bindings_module,
        "profile_context_fingerprint",
        lambda connection, profile_id: PROFILE_FINGERPRINT,
    )
    monkeypatch.setattr(
        bindings_module,
        "resolve_profile_target",
        lambda connection, profile_id: object(),
    )
    with pytest.raises(BusinessMetricBindingError, match="states no rule_id"):
        build_profile_target_binding(_RefusingConnection(), cohort())


def test_a_country_stated_under_a_withholding_rule_is_refused() -> None:
    from evaluation.business_metrics import validate_target_rule_binding

    with pytest.raises(BusinessMetricBindingError, match="withholds the target"):
        validate_target_rule_binding(
            target_binding(country_code="MA", rule_id="mobility-open-v1")
        )
    with pytest.raises(BusinessMetricBindingError, match="precisely to name"):
        validate_target_rule_binding(
            target_binding(country_code=None, rule_id=RESTRICTED_RULE)
        )
    with pytest.raises(BusinessMetricBindingError, match="not a profile target rule"):
        validate_target_rule_binding(
            target_binding(country_code=None, rule_id="mobility-invented-v9")
        )


def test_the_target_binding_carries_no_personal_data() -> None:
    from evaluation.business_metrics import canonical_profile_target_binding_payload

    assert set(canonical_profile_target_binding_payload(target_binding())) == {
        "binding_version",
        "profile_id",
        "profile_fingerprint",
        "country_code",
        "rule_id",
    }


# ------------- 3. generated_at is administrative -------------------------


def test_generated_at_does_not_move_a_non_freshness_metric() -> None:
    """The finding: the clock was inside every metric's identity."""
    early = cohort()
    late = cohort(generated_at=LATER_GENERATED_AT)
    assert early.dataset_generated_at != late.dataset_generated_at
    assert early.binding_fingerprint == late.binding_fingerprint
    payload = canonical_frozen_cohort_binding_payload(early)
    assert "dataset_generated_at" not in payload
    assert "freshness_as_of_date" not in payload

    key = BusinessMetricKey(BusinessMetricName.DATA_AI_RATE)
    first = computed(BusinessMetricName.DATA_AI_RATE, run_context=full_context(early))
    second = computed(BusinessMetricName.DATA_AI_RATE, run_context=full_context(late))
    assert first.scope_fingerprint == second.scope_fingerprint
    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert key.definition.permitted_members == frozenset()


def test_generated_at_moves_the_freshness_metrics() -> None:
    early = cohort()
    late = cohort(generated_at=LATER_GENERATED_AT)
    assert build_freshness_binding(early).as_of_date == AS_OF_DATE
    assert build_freshness_binding(late).as_of_date == LATER_AS_OF_DATE
    for metric in (
        BusinessMetricName.PUBLICATION_DATE_COVERAGE,
        BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
    ):
        key = BusinessMetricKey(metric)
        first_scope = project_metric_scope(
            full_context(early), key, BusinessMetricUniverse.FROZEN_COHORT
        )
        second_scope = project_metric_scope(
            full_context(late), key, BusinessMetricUniverse.FROZEN_COHORT
        )
        assert first_scope.scope_fingerprint != second_scope.scope_fingerprint
        assert (
            project_metric_evidence(full_context(early), key).evidence_fingerprint
            != project_metric_evidence(full_context(late), key).evidence_fingerprint
        )


# ------------- 4. the dedup invariant uses the real manifest -------------


def test_a_manifest_whose_dedup_count_disagrees_with_its_records_is_refused() -> None:
    """The finding: the invariant could be satisfied by passing it in."""
    records = (record(1, absorbed=(11, 12)), record(2))
    with pytest.raises(BusinessMetricBindingError, match="disagree"):
        build_frozen_cohort_binding(
            records, manifest_payload(records, merged_duplicate=3)
        )


def test_build_dedup_evidence_accepts_no_free_count() -> None:
    assert set(inspect.signature(build_dedup_evidence).parameters) == {
        "cohort_binding"
    }
    binding = cohort()
    evidence = build_dedup_evidence(binding)
    assert evidence.absorbed_duplicate_total == binding.absorbed_duplicate_total
    assert evidence.declared_merged_duplicate_count == 3
    assert evidence.represented_universe_size == PRE_DEDUP_SIZE
    assert verify_dedup_evidence_fingerprint(evidence) == evidence.evidence_fingerprint


def test_a_manifest_with_no_merged_duplicate_count_is_refused() -> None:
    payload = manifest_payload(DEFAULT_RECORDS)
    del payload["cohort"]["excluded_counts"][MERGED_DUPLICATE_EXCLUSION_KEY]
    with pytest.raises(BusinessMetricBindingError, match="is not what it says|refusing to assume"):
        build_frozen_cohort_binding(DEFAULT_RECORDS, payload)


# ------------- 5. the structured supports are frozen here ----------------


def test_every_required_support_block_has_its_host() -> None:
    assert {str(metric) for metric in REQUIRED_SUPPORT_BLOCKS} == {
        "TARGET_COUNTRY_MATCH_RATE",
        "DATA_AI_RATE",
        "PFE_OR_INTERNSHIP_RATE",
        "NORMAL_LISTING_RATE",
        "UNKNOWN_LOCATION_RATE",
        "PUBLICATION_DATE_COVERAGE",
    }
    for metric, attribute in REQUIRED_SUPPORT_BLOCKS.items():
        assert metric in SUPPORT_BLOCK_HOSTS[attribute]


@pytest.mark.parametrize(
    "metric",
    [
        BusinessMetricName.DATA_AI_RATE,
        BusinessMetricName.PFE_OR_INTERNSHIP_RATE,
        BusinessMetricName.NORMAL_LISTING_RATE,
        BusinessMetricName.UNKNOWN_LOCATION_RATE,
        BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
        BusinessMetricName.PUBLICATION_DATE_COVERAGE,
    ],
)
def test_a_computed_host_without_its_partition_is_refused(metric) -> None:
    attribute = REQUIRED_SUPPORT_BLOCKS[metric]
    with pytest.raises(BusinessMetricContractError, match=f"without its {attribute}"):
        computed(
            metric,
            support=BusinessMetricSupport(
                numerator=1, denominator=COHORT_SIZE, universe_size=COHORT_SIZE
            ),
        )


def test_the_data_ai_breakdown_must_cover_the_cohort_and_match_the_numerator() -> None:
    with pytest.raises(BusinessMetricBindingError, match="lost or double-counted|count 4 record"):
        computed(
            BusinessMetricName.DATA_AI_RATE,
            support=BusinessMetricSupport(
                numerator=1,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                data_ai_breakdown=data_ai_breakdown(core=2),
            ),
        )
    with pytest.raises(BusinessMetricBindingError, match="core plus adjacent"):
        computed(
            BusinessMetricName.DATA_AI_RATE,
            numerator=2,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                data_ai_breakdown=data_ai_breakdown(),
            ),
        )
    ok = computed(
        BusinessMetricName.DATA_AI_RATE,
        support=BusinessMetricSupport(
            numerator=1,
            denominator=COHORT_SIZE,
            universe_size=COHORT_SIZE,
            data_ai_breakdown=data_ai_breakdown(core=1, adjacent=0),
        ),
    )
    assert ok.support.data_ai_breakdown.data_ai_count == 1


def test_the_data_ai_breakdown_is_reused_not_redefined() -> None:
    """One picture of the cohort behind three rates."""
    assert SUPPORT_BLOCK_HOSTS["data_ai_breakdown"] == frozenset(
        {
            BusinessMetricName.DATA_AI_RATE,
            BusinessMetricName.CORE_DATA_AI_RATE,
            BusinessMetricName.CLASSIFICATION_COVERAGE_RATE,
        }
    )
    breakdown = data_ai_breakdown(core=1, adjacent=0, out_of_scope=1, uncertain=1)
    core = computed(
        BusinessMetricName.CORE_DATA_AI_RATE,
        support=BusinessMetricSupport(
            numerator=1,
            denominator=COHORT_SIZE,
            universe_size=COHORT_SIZE,
            data_ai_breakdown=breakdown,
        ),
    )
    assert core.support.data_ai_breakdown is breakdown
    coverage = computed(
        BusinessMetricName.CLASSIFICATION_COVERAGE_RATE,
        support=BusinessMetricSupport(
            numerator=3,
            denominator=COHORT_SIZE,
            universe_size=COHORT_SIZE,
            data_ai_breakdown=breakdown,
        ),
        value=1.0,
    )
    assert coverage.support.data_ai_breakdown.classified_count == 3
    with pytest.raises(BusinessMetricBindingError, match="classified records"):
        computed(
            BusinessMetricName.CLASSIFICATION_COVERAGE_RATE,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                data_ai_breakdown=breakdown,
            ),
        )


def test_apprenticeship_is_not_in_the_pfe_or_internship_numerator() -> None:
    breakdown = type_breakdown(pfe=1, internship=0, apprenticeship=1, job=1)
    assert breakdown.total == COHORT_SIZE
    assert breakdown.pfe_or_internship_count == 1
    ok = computed(
        BusinessMetricName.PFE_OR_INTERNSHIP_RATE,
        support=BusinessMetricSupport(
            numerator=1,
            denominator=COHORT_SIZE,
            universe_size=COHORT_SIZE,
            opportunity_type_breakdown=breakdown,
        ),
    )
    assert ok.support.opportunity_type_breakdown.apprenticeship_count == 1
    with pytest.raises(BusinessMetricBindingError, match="apprenticeships excluded"):
        computed(
            BusinessMetricName.PFE_OR_INTERNSHIP_RATE,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                opportunity_type_breakdown=breakdown,
            ),
        )


def test_the_listing_quality_breakdown_must_match_the_numerator() -> None:
    with pytest.raises(BusinessMetricBindingError, match="normal listings"):
        computed(
            BusinessMetricName.NORMAL_LISTING_RATE,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                listing_quality_breakdown=quality_breakdown(),
            ),
        )


def test_the_unknown_location_numerator_is_the_two_exclusive_conditions() -> None:
    diagnostics = location_diagnostics(ambiguous=2, no_segments=1, unknown_segment=1)
    assert diagnostics.unknown_location_count == 2
    ok = computed(
        BusinessMetricName.UNKNOWN_LOCATION_RATE,
        support=BusinessMetricSupport(
            numerator=2,
            denominator=COHORT_SIZE,
            universe_size=COHORT_SIZE,
            unknown_location_diagnostics=diagnostics,
        ),
    )
    assert ok.support.unknown_location_diagnostics.ambiguous_location_count == 2
    with pytest.raises(BusinessMetricBindingError, match="no segment or an UNKNOWN"):
        computed(
            BusinessMetricName.UNKNOWN_LOCATION_RATE,
            support=BusinessMetricSupport(
                numerator=4,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                unknown_location_diagnostics=diagnostics,
            ),
        )
    with pytest.raises(BusinessMetricBindingError, match="in a cohort of"):
        computed(
            BusinessMetricName.UNKNOWN_LOCATION_RATE,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                unknown_location_diagnostics=location_diagnostics(
                    ambiguous=99, no_segments=1, unknown_segment=1
                ),
            ),
        )


def test_the_target_verdicts_must_sum_to_the_cohort_and_match_the_numerator() -> None:
    with pytest.raises(BusinessMetricBindingError, match="target verdicts count"):
        computed(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            support=BusinessMetricSupport(
                numerator=1,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                target_verdict_breakdown=TargetVerdictBreakdown(1, 1, 0),
            ),
        )
    with pytest.raises(BusinessMetricBindingError, match="target matches"):
        computed(
            BusinessMetricName.TARGET_COUNTRY_MATCH_RATE,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                target_verdict_breakdown=TargetVerdictBreakdown(1, 1, 1),
            ),
        )


def test_a_block_on_a_metric_that_is_not_its_host_is_refused() -> None:
    with pytest.raises(BusinessMetricContractError, match="belongs to"):
        computed(
            BusinessMetricName.DESCRIPTION_PRESENCE_RATE,
            support=BusinessMetricSupport(
                numerator=1,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                listing_quality_breakdown=quality_breakdown(),
            ),
        )
    assert TARGET_VERDICT_HOST_METRIC is BusinessMetricName.TARGET_COUNTRY_MATCH_RATE
    assert (
        FRESHNESS_DISTRIBUTION_HOST_METRIC
        is BusinessMetricName.PUBLICATION_DATE_COVERAGE
    )


def test_the_structured_blocks_are_inside_the_result_fingerprint() -> None:
    base = computed(BusinessMetricName.DATA_AI_RATE)
    other = computed(
        BusinessMetricName.DATA_AI_RATE,
        support=BusinessMetricSupport(
            numerator=1,
            denominator=COHORT_SIZE,
            universe_size=COHORT_SIZE,
            data_ai_breakdown=data_ai_breakdown(
                core=1, adjacent=0, out_of_scope=0, uncertain=2
            ),
        ),
    )
    assert base.value == other.value
    assert base.result_fingerprint != other.result_fingerprint
    payload = canonical_business_metric_result_payload(base)
    assert payload["support"]["data_ai_breakdown"]["core_target_count"] == 1


def test_an_n_a_may_not_carry_a_computed_block() -> None:
    with pytest.raises(BusinessMetricContractError, match="was not allowed"):
        unavailable(
            BusinessMetricName.PUBLICATION_DATE_COVERAGE,
            BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING,
            run_context=context(),
            support=BusinessMetricSupport(
                universe_size=COHORT_SIZE, freshness_distribution=distribution()
            ),
        )


def test_publication_date_coverage_states_the_known_date_count() -> None:
    ok = computed(
        BusinessMetricName.PUBLICATION_DATE_COVERAGE, numerator=0, value=0.0
    )
    assert ok.support.freshness_distribution.known_date_count == 0
    dated = {bucket: 0 for bucket in FRESHNESS_BUCKET_ORDER}
    dated[FreshnessBucket.AGE_0_2] = 2
    dated[FreshnessBucket.MISSING] = 1
    assert (
        computed(
            BusinessMetricName.PUBLICATION_DATE_COVERAGE,
            numerator=2,
            support=BusinessMetricSupport(
                numerator=2,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                freshness_distribution=distribution(dated),
            ),
            value=2 / 3,
        ).support.numerator
        == 2
    )
    with pytest.raises(BusinessMetricBindingError, match="known publication dates"):
        computed(
            BusinessMetricName.PUBLICATION_DATE_COVERAGE,
            support=BusinessMetricSupport(
                numerator=1,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                freshness_distribution=distribution(),
            ),
        )


def test_publication_date_coverage_requires_the_frozen_freshness_binding() -> None:
    assert metric_definition(
        BusinessMetricName.PUBLICATION_DATE_COVERAGE
    ).required_members == frozenset({BusinessEvidenceMember.FRESHNESS_BINDING})
    with pytest.raises(BusinessEvidenceClassError, match="FRESHNESS_BINDING"):
        computed(BusinessMetricName.PUBLICATION_DATE_COVERAGE, run_context=context())


def test_a_freshness_as_of_date_other_than_the_snapshot_s_is_refused() -> None:
    from evaluation.business_metrics import freshness_binding_fingerprint

    assert set(inspect.signature(build_freshness_binding).parameters) == {
        "cohort_binding",
        "policy_version",
    }
    honest = build_freshness_binding(cohort())
    forged = replace(honest, as_of_date="2026-04-01")
    resealed = replace(
        forged, binding_fingerprint=freshness_binding_fingerprint(forged)
    )
    with pytest.raises(BusinessMetricBindingError, match="never chosen"):
        context(freshness_binding=resealed)


def test_the_distribution_survives_zero_valid_dates_and_the_mean_does_not() -> None:
    run = full_context()
    coverage = computed(
        BusinessMetricName.PUBLICATION_DATE_COVERAGE,
        run_context=run,
        numerator=0,
        value=0.0,
    )
    assert coverage.status is BusinessMetricStatus.COMPUTED
    assert coverage.support.freshness_distribution.known_date_count == 0
    mean = unavailable(
        BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
        BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES,
        run_context=run,
    )
    assert mean.status is BusinessMetricStatus.N_A
    assert mean.value is None


# ------------- 6. the freshness refusal carries a witness ----------------


def test_no_valid_publication_dates_needs_a_witness() -> None:
    """The finding: the one unverifiable refusal was granted for free."""
    run = full_context()
    with pytest.raises(BusinessMetricContractError, match="offers no witness"):
        _build_business_metric_result(
            key=BusinessMetricKey(BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE),
            status=BusinessMetricStatus.N_A,
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            context=run,
            support=BusinessMetricSupport(universe_size=COHORT_SIZE),
            reason=BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES,
        )
    with pytest.raises(BusinessMetricContractError, match="offers no witness"):
        unavailable(
            BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
            BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES,
            run_context=run,
            witness=FreshnessAvailabilityWitness(known_date_count=2),
        )
    stated = unavailable(
        BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
        BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES,
        run_context=run,
    )
    assert stated.support.freshness_availability_witness.known_date_count == 0
    assert (
        canonical_business_metric_result_payload(stated)["support"][
            "freshness_availability_witness"
        ]["known_date_count"]
        == 0
    )


def test_the_witness_belongs_to_one_metric_and_one_refusal() -> None:
    assert SUPPORT_BLOCK_HOSTS["freshness_availability_witness"] == frozenset(
        {BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE}
    )
    run = full_context()
    with pytest.raises(BusinessMetricContractError, match="belongs to"):
        unavailable(
            BusinessMetricName.BROKEN_URL_RATE,
            BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT,
            run_context=context(
                cohort(),
                url_audit_binding=url_audit(
                    cohort(),
                    observations=tuple(
                        observation(
                            url, outcome=UrlAuditOutcome.INCONCLUSIVE, attempted=False
                        )
                        for url in cohort().expected_action_urls
                    ),
                ),
            ),
            support=BusinessMetricSupport(
                universe_size=AUDITED_SIZE,
                freshness_availability_witness=FreshnessAvailabilityWitness(0),
            ),
        )
    with pytest.raises(BusinessMetricContractError, match="justifies a different"):
        unavailable(
            BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
            BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING,
            run_context=context(),
            support=BusinessMetricSupport(
                universe_size=COHORT_SIZE,
                freshness_availability_witness=FreshnessAvailabilityWitness(0),
            ),
        )
    with pytest.raises(BusinessMetricContractError, match="only to justify a refusal"):
        computed(
            BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
            run_context=run,
            support=BusinessMetricSupport(
                numerator=1,
                denominator=COHORT_SIZE,
                universe_size=COHORT_SIZE,
                freshness_availability_witness=FreshnessAvailabilityWitness(0),
            ),
        )


# ------------- 7. the declared source universe has no self-declared id ---


def test_the_public_path_computes_the_source_map_digest_itself() -> None:
    """The finding: a caller could state `"a" * 64` and call it an exact map."""
    from evaluation.business_metrics import bindings as bindings_module

    assert not hasattr(bindings_module, "build_declared_source_universe_evidence")
    assert "build_declared_source_universe_evidence" not in business_package.__all__
    public = inspect.signature(declared_source_universe_from_source_map).parameters
    assert "source_map_fingerprint" not in public
    universe = declared_universe()
    expected = hashlib.sha256(
        canonical_json(
            source_map_payload(
                _FakeSourceMap((_FakeEntry("alpha_board"), _FakeEntry("beta_board")))
            )
        ).encode("utf-8")
    ).hexdigest()
    assert universe.source_map_fingerprint == expected


def test_a_field_outside_the_projection_still_moves_the_source_map_fingerprint() -> None:
    """The real Phase 7C map, one `notes` amended, `version: v1` untouched."""
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        load_source_map,
    )

    source_map = load_source_map(REPOSITORY_ROOT / DEFAULT_SOURCE_MAP_PATH)
    before = declared_source_universe_from_source_map(
        source_map, git_commit=GIT_COMMIT
    )
    amended = replace(
        source_map,
        sources=(replace(source_map.sources[0], notes="amended in place"),)
        + source_map.sources[1:],
    )
    after = declared_source_universe_from_source_map(amended, git_commit=GIT_COMMIT)
    assert before.map_version == after.map_version
    assert [entry.source_id for entry in before.entries] == [
        entry.source_id for entry in after.entries
    ]
    assert before.source_map_fingerprint != after.source_map_fingerprint
    assert before.content_fingerprint != after.content_fingerprint
    assert verify_declared_source_universe_fingerprint(before) == (
        before.content_fingerprint
    )


def test_a_declared_source_universe_must_name_its_revision() -> None:
    with pytest.raises(BusinessMetricBindingError, match="must name the git commit"):
        declared_universe(git_commit=None)
    for bad in ("c" * 7, "C" * 40, "not a commit", "c" * 41):
        with pytest.raises(BusinessMetricBindingError, match="Git commit id"):
            declared_universe(git_commit=bad)
    payload = canonical_declared_source_universe_payload(declared_universe())
    assert payload["git_commit"] == GIT_COMMIT
    assert set(payload) >= {
        "map_name",
        "map_version",
        "scope_country",
        "source_map_fingerprint",
        "git_commit",
    }
    assert "provenance_path" not in payload
    elsewhere = declared_universe(git_commit="e" * 40)
    assert elsewhere.content_fingerprint != declared_universe().content_fingerprint


def test_something_that_is_not_a_source_map_is_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="not a declared source map"):
        declared_source_universe_from_source_map(object(), git_commit=GIT_COMMIT)

    class _HalfMap:
        map_name = "m"
        version = "v1"
        scope_country = "MA"
        sources = [object()]

    with pytest.raises(BusinessMetricBindingError, match="is missing"):
        declared_source_universe_from_source_map(_HalfMap(), git_commit=GIT_COMMIT)


def test_the_real_phase_7c_map_projects_into_bindable_evidence() -> None:
    from evaluation.morocco_pfe.validator import (
        DEFAULT_SOURCE_MAP_PATH,
        load_source_map,
    )

    source_map = load_source_map(REPOSITORY_ROOT / DEFAULT_SOURCE_MAP_PATH)
    projected = declared_source_universe_from_source_map(
        source_map,
        git_commit=GIT_COMMIT,
        provenance_path=str(DEFAULT_SOURCE_MAP_PATH),
    )
    assert projected.map_name == source_map.map_name
    assert len(projected.entries) == len(source_map.sources)
    assert all(
        isinstance(entry, DeclaredSourceEntry) for entry in projected.entries
    )
    assert verify_declared_source_universe_fingerprint(projected) == (
        projected.content_fingerprint
    )


def test_an_evidence_view_carrying_an_unused_member_is_refused() -> None:
    evidence = project_metric_evidence(
        full_context(), BusinessMetricKey(BusinessMetricName.DATA_AI_RATE)
    )
    polluted = reseal_evidence(evidence, url_audit_binding=url_audit())
    with pytest.raises(BusinessEvidenceClassError, match="does not use"):
        require_business_evidence_class(
            BusinessMetricKey(BusinessMetricName.DATA_AI_RATE),
            polluted,
            status=BusinessMetricStatus.COMPUTED,
        )


def test_a_result_cannot_be_constructed_with_a_mismatched_universe() -> None:
    with pytest.raises(BusinessMetricContractError, match="is defined over"):
        BusinessMetricResult(
            key=BusinessMetricKey(BusinessMetricName.BROKEN_URL_RATE),
            status=BusinessMetricStatus.COMPUTED,
            universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
            evidence_class=BusinessEvidenceClass.EXTERNAL_AUDIT_DESCRIPTIVE,
            support=BusinessMetricSupport(
                numerator=1, denominator=2, universe_size=2
            ),
            scope_fingerprint="1" * 64,
            evidence_fingerprint="2" * 64,
            result_fingerprint="3" * 64,
            contract_version=BUSINESS_METRIC_CONTRACT_VERSION,
            value=0.5,
        )


# --------------------------------------------------------------------------
# v5.1 — a valid SHA-256 over an invalid structure is not a valid dataset
# --------------------------------------------------------------------------


def _refuses(records, **manifest):
    """Build the binding from a manifest that *matches* these records.

    Every fixture here digests consistently with what it holds, so the refusal
    can only come from the structural check. A test that forged the digest as
    well would prove nothing: the digest check would catch it first, and the
    structural rule would stay untested.
    """
    return build_frozen_cohort_binding(records, manifest_payload(records, **manifest))


def test_a_record_missing_a_contract_field_is_refused() -> None:
    missing = dict(DEFAULT_RECORDS[0])
    del missing["deadline"]
    records = (missing,) + DEFAULT_RECORDS[1:]
    with pytest.raises(BusinessMetricBindingError, match="missing \\['deadline'\\]"):
        _refuses(records)


def test_a_record_carrying_an_unexpected_field_is_refused() -> None:
    records = (
        {**DEFAULT_RECORDS[0], "human_label": "RELEVANT"},
    ) + DEFAULT_RECORDS[1:]
    with pytest.raises(
        BusinessMetricBindingError, match="unexpected \\['human_label'\\]"
    ):
        _refuses(records)


@pytest.mark.parametrize("identifier", [True, False, "1", 1.0, None])
def test_an_opportunity_id_that_is_not_an_integer_identity_is_refused(
    identifier,
) -> None:
    """`True` is an `int` in Python, and would silently merge with the id 1."""
    records = (record(identifier), record(9))
    with pytest.raises(BusinessMetricBindingError, match="not an integer identity"):
        _refuses(records)


def test_a_repeated_opportunity_id_is_refused() -> None:
    records = (record(1, application_url=ALPHA), record(1, source_url=BETA))
    with pytest.raises(BusinessMetricBindingError, match="repeats opportunity_id 1"):
        _refuses(records)


def test_records_out_of_canonical_order_are_refused() -> None:
    reordered = tuple(reversed(DEFAULT_RECORDS))
    with pytest.raises(BusinessMetricBindingError, match="are not in 'opportunity_id"):
        _refuses(reordered)
    # The same records in the contract's order are accepted, so the refusal is
    # about the order and not about the records.
    assert _refuses(DEFAULT_RECORDS).cohort_size == len(DEFAULT_RECORDS)


def test_a_snapshot_ordered_some_other_way_is_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="cohort.ordering"):
        _refuses(DEFAULT_RECORDS, ordering="discovered_at DESC")


def test_criteria_edited_under_an_unchanged_version_are_refused() -> None:
    """The version is a label; the criteria are the rule.

    The manifest digests consistently with its own edited criteria — they are
    inside Phase 10.1's fingerprint domain, so both sides of that comparison
    moved together and the `dataset_id` still verifies. Only a check against the
    build's own constants catches it.
    """
    edited = ("opportunities.is_active = 1",)
    assert edited != EVALUATION_COHORT_CRITERIA
    with pytest.raises(BusinessMetricBindingError, match="cohort.criteria"):
        _refuses(DEFAULT_RECORDS, criteria=edited)
    # ...and the same criteria in the wrong order are a different rule too.
    with pytest.raises(BusinessMetricBindingError, match="their order is part of it"):
        _refuses(DEFAULT_RECORDS, criteria=tuple(reversed(EVALUATION_COHORT_CRITERIA)))


def test_the_structural_check_runs_before_the_digest() -> None:
    """A self-consistent digest is exactly the case the structure rule is for."""
    records = (record(2, application_url=ALPHA), record(1, source_url=BETA))
    payload = manifest_payload(records)
    # The manifest is internally sound: it is only the records that are not.
    assert payload["record_count"] == len(records)
    with pytest.raises(BusinessMetricBindingError, match="are not in 'opportunity_id"):
        build_frozen_cohort_binding(records, payload)


def test_the_record_contract_is_read_from_phase_10_1_not_restated() -> None:
    from evaluation.business_metrics.bindings import FROZEN_RECORD_FIELDS

    assert FROZEN_RECORD_FIELDS == frozenset(
        field.name for field in fields(EvaluationOpportunityRecord)
    )
    assert "opportunity_id" in FROZEN_RECORD_FIELDS
    assert "human_label" not in FROZEN_RECORD_FIELDS


# --------------------------------------------------------------------------
# v5.2 — a benchmark is its rows, not the numbers beside them
# --------------------------------------------------------------------------


def test_a_manifest_that_disagrees_with_its_rows_is_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="current_rows 60 and 2 row"):
        build_benchmark_binding(
            benchmark_manifest(rows=2, declared_rows=60),
            tuple(benchmark_row(index) for index in (1, 2)),
        )


def test_a_benchmark_ready_under_its_own_minimum_is_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="contradicts itself"):
        build_benchmark_binding(
            benchmark_manifest(ready=True, rows=2, target=60),
            tuple(benchmark_row(index) for index in (1, 2)),
        )


def test_different_rows_are_a_different_benchmark() -> None:
    base = tuple(benchmark_row(index) for index in (1, 2))
    edited = (base[0], {**base[1], "notes": "amended in place"})
    added = base + (benchmark_row(3),)
    reordered = tuple(reversed(base))

    first = build_benchmark_binding(benchmark_manifest(rows=2), base)
    second = build_benchmark_binding(benchmark_manifest(rows=2), edited)
    third = build_benchmark_binding(benchmark_manifest(rows=3), added)
    fourth = build_benchmark_binding(benchmark_manifest(rows=2), reordered)

    digests = {
        first.records_fingerprint,
        second.records_fingerprint,
        third.records_fingerprint,
        fourth.records_fingerprint,
    }
    assert len(digests) == 4
    # ...and the rows' digest is inside the binding's own identity.
    assert first.content_fingerprint != second.content_fingerprint
    # The same rows twice are the same benchmark.
    assert (
        build_benchmark_binding(benchmark_manifest(rows=2), base).content_fingerprint
        == first.content_fingerprint
    )


def test_the_caller_cannot_state_a_rows_digest_or_a_readiness() -> None:
    parameters = set(inspect.signature(build_benchmark_binding).parameters)
    assert parameters == {"manifest", "records", "provenance_path"}
    assert "records_fingerprint" not in parameters
    assert "evaluation_ready" not in parameters
    assert "record_count" not in parameters
    # Readiness is read from the manifest, and the digest is computed from rows.
    rows = tuple(benchmark_row(index) for index in range(1, 61))
    ready = build_benchmark_binding(benchmark_manifest(ready=True, rows=60), rows)
    assert ready.evaluation_ready is True
    assert ready.record_count == 60
    assert ready.records_fingerprint == benchmark_records_fingerprint(rows)


def test_a_non_boolean_readiness_is_refused() -> None:
    manifest = {**benchmark_manifest(rows=2), "evaluation_ready": "false"}
    with pytest.raises(BusinessMetricBindingError, match="is not a boolean"):
        build_benchmark_binding(
            manifest, tuple(benchmark_row(index) for index in (1, 2))
        )


def test_the_benchmark_builder_names_no_benchmark_and_no_country() -> None:
    """The generic engine holds nothing Morocco-specific.

    Checked over the function's own string *literals* rather than its text, so
    a manifest field name that happens to contain letters is not mistaken for a
    country code.
    """
    import ast

    import evaluation.business_metrics.bindings as bindings_module

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(bindings_module.build_benchmark_binding))
    )
    # The docstring may *name* today's benchmark; the code may not depend on it.
    function = tree.body[0]
    if (
        isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
        and isinstance(function.body[0].value.value, str)
    ):
        tree = ast.Module(body=function.body[1:], type_ignores=[])
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    forbidden = {"MA", "morocco_pfe_gold_v1", "morocco", "PFE", "7C.1", "v1"}
    assert not (literals & forbidden)
    for literal in literals:
        assert "morocco" not in literal.lower()
        assert "gold" not in literal.lower()


def test_the_real_gold_benchmark_binds_as_a_draft_of_two_rows() -> None:
    import json

    root = REPOSITORY_ROOT / "evaluation/benchmarks"
    manifest = json.loads(
        (root / "morocco_pfe_gold_v1.manifest.json").read_text(encoding="utf-8")
    )
    rows = tuple(
        json.loads(line)
        for line in (root / "morocco_pfe_gold_v1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    )
    binding = build_benchmark_binding(
        manifest,
        rows,
        provenance_path="evaluation/benchmarks/morocco_pfe_gold_v1.manifest.json",
    )
    assert binding.benchmark_name == "morocco_pfe_gold_v1"
    assert binding.record_count == 2
    assert binding.status == "DRAFT"
    assert binding.target_minimum_rows == 60
    assert binding.evaluation_ready is False
    assert binding.records_fingerprint == benchmark_records_fingerprint(rows)
    assert verify_benchmark_binding_fingerprint(binding) == binding.content_fingerprint

    # ...and that is exactly what licenses the one refusal it entitles.
    run = full_context(benchmark_binding=binding)
    refused = unavailable(
        BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL,
        BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY,
        run_context=run,
        support=BusinessMetricSupport(universe_size=DECLARED_SIZE),
    )
    assert refused.status is BusinessMetricStatus.N_A
    assert refused.value is None


# --------------------------------------------------------------------------
# v5.3 — the derived URL universe is ordered by the snapshot, not by strings
# --------------------------------------------------------------------------


def test_the_url_universe_keeps_first_occurrence_not_lexical_order() -> None:
    """The lexical order and the snapshot order disagree here, on purpose."""
    zulu = "https://zulu.example/jobs/1"
    alpha = "https://alpha.example/jobs/2"
    mike = "https://mike.example/jobs/3"
    records = (
        record(1, application_url=zulu),
        record(2, source_url=alpha),
        record(3, canonical_url=mike),
    )
    binding = cohort(records)
    assert binding.expected_action_urls == (zulu, alpha, mike)
    assert binding.expected_action_urls != tuple(sorted((zulu, alpha, mike)))


def test_a_url_republished_later_keeps_its_first_position() -> None:
    records = (
        record(1, application_url=ALPHA),
        record(2, source_url=BETA),
        record(3, canonical_url=ALPHA),
    )
    assert expected_frozen_action_urls(records) == (ALPHA, BETA)
    binding = cohort(records)
    assert binding.expected_audited_universe_size == 2


def test_two_snapshots_of_the_same_urls_in_another_order_are_not_the_same() -> None:
    """Which is the whole point of first occurrence over `sorted()`."""
    first = (record(1, application_url=ALPHA), record(2, source_url=BETA))
    second = (record(1, application_url=BETA), record(2, source_url=ALPHA))
    assert expected_frozen_action_urls(first) == (ALPHA, BETA)
    assert expected_frozen_action_urls(second) == (BETA, ALPHA)
    assert cohort(first).binding_fingerprint != cohort(second).binding_fingerprint


def test_the_audit_still_holds_the_universe_as_a_set() -> None:
    """Order is the universe's identity; completeness is still membership."""
    records = (
        record(1, application_url="https://zulu.example/jobs/1"),
        record(2, source_url="https://alpha.example/jobs/2"),
    )
    binding = cohort(records)
    audit = url_audit(
        binding,
        observations=tuple(
            observation(url) for url in reversed(binding.expected_action_urls)
        ),
    )
    assert_url_audit_covers(audit, binding)


# --------------------------------------------------------------------------
# v5.4 — 10.4a seals no value, and says so in its public surface
# --------------------------------------------------------------------------


def test_the_package_exposes_no_public_result_sealer() -> None:
    import evaluation.business_metrics as package
    import evaluation.business_metrics.bindings as bindings_module

    assert "build_business_metric_result" not in package.__all__
    assert not hasattr(package, "build_business_metric_result")
    assert "build_business_metric_result" not in bindings_module.__all__
    assert "_build_business_metric_result" not in bindings_module.__all__
    assert not hasattr(bindings_module, "build_business_metric_result")
    # No public builder takes a metric value, a numerator or a denominator.
    builders = [name for name in package.__all__ if name.startswith("build_")]
    assert builders, "the package exports no builders at all"
    for name in builders:
        parameters = set(inspect.signature(getattr(package, name)).parameters)
        assert "value" not in parameters, name
        assert "numerator" not in parameters, name
        assert "denominator" not in parameters, name
    # The one place a result can be assembled is reached through the module,
    # under a name that says it is not part of the surface.
    assert callable(bindings_module._build_business_metric_result)


def test_verifying_a_result_stays_public_and_is_still_strict() -> None:
    import evaluation.business_metrics as package

    assert "verify_business_metric_result" in package.__all__
    result = computed()
    assert verify_business_metric_result(result, context=full_context()) == result


def test_no_formula_is_smuggled_into_the_package() -> None:
    """10.4a computes nothing, so the assembly checks structure and not answers."""
    run = full_context()
    support = BusinessMetricSupport(
        numerator=1, denominator=COHORT_SIZE, universe_size=COHORT_SIZE
    )
    honest = computed(run_context=run, support=support, value=1 / COHORT_SIZE)
    dishonest = _build_business_metric_result(
        key=BusinessMetricKey(BusinessMetricName.CORE_DATA_AI_RATE),
        status=BusinessMetricStatus.COMPUTED,
        universe_kind=BusinessMetricUniverse.FROZEN_COHORT,
        context=run,
        support=support,
        value=0.99,
    )
    # Both seal, over the same support: whether a value is the *answer* is a
    # property of a formula, and there is none here to have it. Which is exactly
    # why the assembly that accepts a value is not a public API.
    assert honest.support == dishonest.support
    assert honest.value != dishonest.value
    assert honest.result_fingerprint != dishonest.result_fingerprint
