"""Invented Phase 10.4 artefacts shared by the Phase 10.4b unit tests.

Phase 10.4a's own tests build *structural* fixtures: records with every optional
field null, because that slice checked shapes and computed nothing. Phase 10.4b
computes, so the records here carry **content** — qualifications, geography
segments, source rows, publication dates — and this module is where that content
is built, once, for the formula, run and storage suites to share. Two copies of a
record builder eventually stop building the same record.

Everything is built in memory and every snapshot is invented, but invented **in
Phase 10.1's own shape**: `manifest_payload` assembles a manifest with the
sections that package's fingerprint domain covers and digests it through that
same domain, so `build_frozen_cohort_binding` verifies these fixtures exactly as
it would verify a real one. Nothing here opens the operational database, reads a
human label, makes an HTTP request or touches the 394 real opportunities.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, replace
from typing import Any

from evaluation.business_metrics import (
    MERGED_DUPLICATE_EXCLUSION_KEY,
    PLACEHOLDER_FINGERPRINT,
    PROFILE_TARGET_BINDING_VERSION,
    BusinessMetricComputationContext,
    ProfileTargetBindingEvidence,
    UrlAuditInconclusiveReason,
    UrlAuditObservation,
    UrlAuditOutcome,
    UrlShape,
    build_benchmark_binding,
    build_business_metric_computation_context,
    build_business_metric_run_context,
    build_dedup_evidence,
    build_freshness_binding,
    build_frozen_cohort_binding,
    build_url_audit_binding,
    declared_source_universe_from_source_map,
    is_http_url,
    profile_target_binding_fingerprint,
    url_shape,
)
from evaluation.dataset import (
    EVALUATION_CANONICAL_ORDER,
    EVALUATION_COHORT_CRITERIA,
    EVALUATION_COHORT_VERSION,
    EVALUATION_DATASET_SCHEMA_VERSION,
    EvaluationOpportunityRecord,
)
from evaluation.dataset.fingerprint import manifest_fingerprint_domain
from services.collector.matching.fingerprint import canonical_json

PROFILE_FINGERPRINT = "9" * 64
PROFILE_ID = 1
USER_ID = 7
RESTRICTED_RULE = "mobility-restricted-country-v1"
OPEN_RULE = "mobility-open-v1"
GENERATED_AT = "2026-03-01T09:15:00+00:00"
AS_OF_DATE = "2026-03-01"
CAMPAIGN_AT = "2026-03-02T08:00:00Z"
GIT_COMMIT = "c" * 40

TARGET_COUNTRY = "MA"
OTHER_COUNTRY = "FR"

ALPHA = "https://alpha.example/jobs/1"
BETA = "https://beta.example/jobs/2"
GAMMA = "https://gamma.example/jobs/3"


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

#: Every field of the Phase 10.1 contract at its null-ish value, keyed from the
#: dataclass itself so a field added upstream appears here too rather than
#: turning every fixture into a structural refusal.
_EMPTY_RECORD = {
    field.name: (
        []
        if field.name in ("absorbed_duplicate_ids", "sources", "geography_segments")
        else None
    )
    for field in fields(EvaluationOpportunityRecord)
}


def qualification(
    *,
    value: str = "CORE_TARGET",
    opportunity_type: str = "PFE",
    listing_quality: str = "NORMAL_LISTING",
    **overrides: Any,
) -> dict[str, Any]:
    """A qualification block in the shape Phase 10.1 freezes one."""
    return {
        "qualification": value,
        "primary_domain": "DATA_SCIENCE",
        "opportunity_type": opportunity_type,
        "employment_type": "FULL_TIME",
        "listing_quality": listing_quality,
        "classifier_version": "qualification-v1",
        "input_fingerprint": "1" * 64,
        "classified_at": "2026-02-20T10:00:00+00:00",
        "fine_primary_category": None,
        "fine_secondary_categories": None,
        "fine_classifier_version": None,
        **overrides,
    }


def segment(
    status: str = "RESOLVED",
    *,
    country_code: str | None = TARGET_COUNTRY,
    position: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    """One frozen geography segment.

    `country_code` defaults to the target country for a RESOLVED segment; pass
    `None` explicitly for AMBIGUOUS and UNKNOWN, whose coherence rule is that
    they name no country.
    """
    return {
        "segment_position": position,
        "raw_segment": "Casablanca",
        "status": status,
        "rule_id": "city-registry-v1",
        "country_code": None if status != "RESOLVED" else country_code,
        "city_key": None,
        "resolver_version": "geography-v1",
        **overrides,
    }


def source(source_id: str = "alpha_board", **overrides: Any) -> dict[str, Any]:
    """One `opportunity_sources` row as the snapshot holds it."""
    return {
        "source_id": source_id,
        "source_type": "JOB_BOARD",
        "source_url": f"https://{source_id}.example/listing",
        "application_url": None,
        "canonical_url": None,
        "discovered_at": "2026-02-10T08:00:00+00:00",
        **overrides,
    }


def record(
    opportunity_id: int,
    *,
    application_url: str | None = None,
    source_url: str | None = None,
    canonical_url: str | None = None,
    absorbed: tuple[int, ...] = (),
    sources: tuple[dict[str, Any], ...] = (),
    geography_segments: tuple[dict[str, Any], ...] = (),
    **overrides: Any,
) -> dict[str, Any]:
    """One frozen record carrying **exactly** the contract's fields.

    Not reduced to the fields one metric reads: `build_frozen_cohort_binding`
    refuses a record whose field set is not the field set of
    `EvaluationOpportunityRecord`, so a fixture that skipped the others would be
    testing a shape the production path rejects.
    """
    return {
        **_EMPTY_RECORD,
        "opportunity_id": opportunity_id,
        "application_url": application_url,
        "source_url": source_url,
        "canonical_url": canonical_url,
        "absorbed_duplicate_ids": list(absorbed),
        "sources": list(sources),
        "geography_segments": list(geography_segments),
        **overrides,
    }


#: A small cohort with content in it: three postings, three classifications,
#: three geographies, two action URLs and one absorbed duplicate.
DEFAULT_RECORDS = (
    record(
        1,
        application_url=ALPHA,
        absorbed=(11,),
        sources=(source("alpha_board"),),
        geography_segments=(segment("RESOLVED"),),
        description="A data engineering internship.",
        location="Casablanca, Morocco",
        deadline="2026-04-30",
        published_at="2026-02-27T09:00:00+00:00",
        qualification=qualification(),
    ),
    record(
        2,
        source_url=BETA,
        sources=(source("alpha_board"), source("beta_board")),
        geography_segments=(segment("RESOLVED", country_code=OTHER_COUNTRY),),
        published_at="2026-02-01T09:00:00+00:00",
        qualification=qualification(
            value="OUT_OF_SCOPE",
            opportunity_type="JOB",
            listing_quality="POSSIBLE_NON_JOB_PAGE",
        ),
    ),
    record(
        3,
        geography_segments=(segment("UNKNOWN", position=1),),
    ),
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
    records: tuple[dict[str, Any], ...] = DEFAULT_RECORDS,
    *,
    generated_at: str = GENERATED_AT,
    profile_id: int = PROFILE_ID,
    profile_fingerprint: str = PROFILE_FINGERPRINT,
    merged_duplicate: int | None = None,
    schema_version: str = EVALUATION_DATASET_SCHEMA_VERSION,
) -> dict[str, Any]:
    """A manifest in Phase 10.1's shape, digested through its own domain."""
    if merged_duplicate is None:
        merged_duplicate = sum(
            len(item["absorbed_duplicate_ids"]) for item in records
        )
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at": generated_at,
        "git_commit": None,
        "record_count": len(records),
        "profile_context": {
            "profile_id": profile_id,
            "user_id": USER_ID,
            "fingerprint": profile_fingerprint,
        },
        "cohort": {
            "version": EVALUATION_COHORT_VERSION,
            "criteria": list(EVALUATION_COHORT_CRITERIA),
            "ordering": EVALUATION_CANONICAL_ORDER,
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
    fingerprint = hashlib.sha256(
        canonical_json(domain).encode("utf-8")
    ).hexdigest()
    payload["content_fingerprint"] = fingerprint
    payload["dataset_id"] = f"{schema_version}-{fingerprint[:16]}"
    return payload


def cohort(records: tuple[dict[str, Any], ...] = DEFAULT_RECORDS, **overrides: Any):
    """A sealed frozen cohort binding, verified rather than dictated."""
    return build_frozen_cohort_binding(records, manifest_payload(records, **overrides))


# --------------------------------------------------------------------------
# the optional evidence members
# --------------------------------------------------------------------------


def target_binding(
    *,
    country_code: str | None = TARGET_COUNTRY,
    rule_id: str | None = None,
    profile_id: int = PROFILE_ID,
    profile_fingerprint: str = PROFILE_FINGERPRINT,
) -> ProfileTargetBindingEvidence:
    """A profile target binding. `country_code=None` is an UNKNOWN target."""
    resolved_rule = (
        (RESTRICTED_RULE if country_code is not None else OPEN_RULE)
        if rule_id is None
        else rule_id
    )
    draft = ProfileTargetBindingEvidence(
        binding_version=PROFILE_TARGET_BINDING_VERSION,
        profile_id=profile_id,
        profile_fingerprint=profile_fingerprint,
        country_code=country_code,
        rule_id=resolved_rule,
        binding_fingerprint=PLACEHOLDER_FINGERPRINT,
    )
    return replace(
        draft, binding_fingerprint=profile_target_binding_fingerprint(draft)
    )


@dataclass(frozen=True)
class FakeSourceEntry:
    """A source map entry with fields beyond the six a coverage rate projects."""

    id: str
    integration_status: str = "ACTIVE"
    name: str = ""
    homepage_url: str = ""
    homepage_url_status: str = "WELL_KNOWN_UNVERIFIED"
    source_class: str = "JOB_BOARD"
    priority: str = "P0"
    coverage_role: str = "PRIMARY"
    collection_strategy: str = "FUTURE_COLLECTOR"
    country: str = TARGET_COUNTRY
    production_source_id: str | None = None
    live_canary: bool = False
    notes: str = "as written"

    def __post_init__(self) -> None:
        if not self.name:
            object.__setattr__(self, "name", self.id.replace("_", " ").title())
        if not self.homepage_url:
            object.__setattr__(self, "homepage_url", f"https://{self.id}.example/")


@dataclass(frozen=True)
class FakeSourceMap:
    sources: tuple[FakeSourceEntry, ...]
    scope_country: str = TARGET_COUNTRY
    version: str = "v1"
    map_name: str = "invented_source_coverage"
    created_for_phase: str = "7C.1"
    is_production_registry: bool = False
    production_registry_path: str = "config/sources.yaml"


def declared_universe(
    entries: tuple[FakeSourceEntry, ...] | None = None,
    *,
    scope_country: str = TARGET_COUNTRY,
    git_commit: str = GIT_COMMIT,
    provenance_path: str | None = "evaluation/source_coverage/invented.yaml",
):
    """A declared source universe, through the one public builder.

    `provenance_path` is a parameter because it is deliberately **outside** this
    artefact's digest: two bindings differing only in it are the same declared
    universe, and the storage tests need to build exactly that pair.
    """
    source_map = FakeSourceMap(
        sources=(
            entries
            if entries is not None
            else (
                FakeSourceEntry("alpha_board", integration_status="ACTIVE"),
                FakeSourceEntry("beta_board", integration_status="CANDIDATE"),
            )
        ),
        scope_country=scope_country,
    )
    return declared_source_universe_from_source_map(
        source_map,
        git_commit=git_commit,
        provenance_path=provenance_path,
    )


def observation(
    url: str,
    *,
    outcome: UrlAuditOutcome = UrlAuditOutcome.VALID,
    attempted: bool = True,
    **kwargs: Any,
) -> UrlAuditObservation:
    """One URL observation, with the response fields its verdict requires."""
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
        from evaluation.business_metrics import INCONCLUSIVE_STATUS_REASONS

        kwargs.setdefault(
            "reason", INCONCLUSIVE_STATUS_REASONS[kwargs["status_code"]]
        )
    return UrlAuditObservation(
        requested_url=url, attempted=attempted, outcome=outcome, **kwargs
    )


def url_audit(cohort_binding=None, observations=None, **kwargs: Any):
    """A sealed audit, complete over the cohort's expected action URLs."""
    binding = cohort_binding if cohort_binding is not None else cohort()
    if observations is None:
        outcomes = (UrlAuditOutcome.VALID, UrlAuditOutcome.BROKEN)
        generated = []
        for index, url in enumerate(binding.expected_action_urls):
            if is_http_url(url):
                generated.append(
                    observation(url, outcome=outcomes[index % len(outcomes)])
                )
            else:
                # The frozen protocol selects whatever a posting carried, so a
                # `mailto:` or a relative path is in the audited universe and
                # must be represented — as an observation that was never
                # attempted, with the reason its own shape demands.
                shape = url_shape(url)
                generated.append(
                    observation(
                        url,
                        outcome=UrlAuditOutcome.INCONCLUSIVE,
                        attempted=False,
                        reason=(
                            UrlAuditInconclusiveReason.UNSUPPORTED_SCHEME
                            if shape is UrlShape.NON_HTTP_SCHEME
                            else UrlAuditInconclusiveReason.INVALID_URL
                        ),
                    )
                )
        observations = tuple(generated)
    kwargs.setdefault("audit_started_at", CAMPAIGN_AT)
    return build_url_audit_binding(
        cohort_binding=binding, observations=observations, **kwargs
    )


def benchmark_row(index: int) -> dict[str, Any]:
    """One benchmark row, in the shape the real gold file holds."""
    return {
        "benchmark_id": f"invented-{index}",
        "title": f"Stage PFE - invented opportunity {index}",
        "organization": f"Invented Org {index}",
        "country_code": TARGET_COUNTRY,
        "source_name": "invented.example",
        "source_url": f"https://invented.example/offers/{index}",
        "expected_opportunity_type": "PFE",
        "expected_data_ai": True,
        "notes": "as observed",
    }


def benchmark_rows(count: int) -> tuple[dict[str, Any], ...]:
    return tuple(benchmark_row(index) for index in range(1, count + 1))


def benchmark(
    *,
    ready: bool = False,
    rows: int = 2,
    target: int = 60,
    scope_country: str = TARGET_COUNTRY,
    records: tuple[dict[str, Any], ...] | None = None,
    provenance_path: str | None = None,
):
    """A benchmark binding derived from a manifest and the rows it describes.

    `ready=True` needs at least `target` rows, because the binding refuses a
    manifest that claims readiness below its own declared target.

    `provenance_path` is outside the binding's digest, for the same reason as
    above: where a gold file was read is not what it says.
    """
    supplied = benchmark_rows(rows) if records is None else tuple(records)
    manifest = {
        "benchmark_name": "invented_gold_v1",
        "version": "v1",
        "scope_country": scope_country,
        "status": "READY" if ready else "DRAFT",
        "target_minimum_rows": target,
        "current_rows": len(supplied),
        "evaluation_ready": ready,
    }
    return build_benchmark_binding(
        manifest, supplied, provenance_path=provenance_path
    )


def ready_benchmark(rows: int = 3):
    """An evaluation-ready benchmark, with a target its row count satisfies."""
    return benchmark(ready=True, rows=rows, target=rows)


# --------------------------------------------------------------------------
# run contexts and computation contexts
# --------------------------------------------------------------------------


def run_context(cohort_binding=None, **kwargs: Any):
    """A run context assembling exactly the members it is asked for."""
    binding = cohort_binding if cohort_binding is not None else cohort()
    kwargs.setdefault("cohort_binding", binding)
    return build_business_metric_run_context(**kwargs)


def full_run_context(cohort_binding=None, **kwargs: Any):
    """A run that assembled *everything* it could. The leak-detector fixture."""
    binding = cohort_binding if cohort_binding is not None else cohort()
    kwargs.setdefault(
        "profile_target_binding",
        target_binding(
            profile_id=binding.profile_id,
            profile_fingerprint=binding.profile_fingerprint,
        ),
    )
    kwargs.setdefault("declared_source_universe", declared_universe())
    if binding.expected_action_urls:
        # A cohort whose records select no action URL implies an empty audited
        # universe, and the contract refuses an audit with no observation in it —
        # rightly, since there would be nothing it was evidence about. Such a run
        # simply assembles no audit, and the three URL metrics are then `N_A`.
        kwargs.setdefault("url_audit_binding", url_audit(binding))
    kwargs.setdefault("dedup_evidence", build_dedup_evidence(binding))
    kwargs.setdefault("benchmark_binding", benchmark())
    kwargs.setdefault("freshness_binding", build_freshness_binding(binding))
    return run_context(binding, **kwargs)


def computation(
    records: tuple[dict[str, Any], ...] = DEFAULT_RECORDS,
    *,
    context=None,
    full: bool = True,
    benchmark_records: tuple[dict[str, Any], ...] | None = None,
    **context_kwargs: Any,
) -> BusinessMetricComputationContext:
    """A verified computation context over `records`.

    `full=True` assembles every optional member, so a metric under test is
    exercised beside evidence it does not read — which is how a projection leak
    would show up.
    """
    manifest = manifest_payload(records)
    binding = build_frozen_cohort_binding(records, manifest)
    if context is None:
        build = full_run_context if full else run_context
        context = build(binding, **context_kwargs)
    return build_business_metric_computation_context(
        records, manifest, context, benchmark_records=benchmark_records
    )
