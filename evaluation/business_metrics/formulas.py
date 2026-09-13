"""The twenty-seven business / data-quality metrics — Phase 10.4b.

Phase 10.4a froze the contract and computed nothing. This module is where a
number is finally produced, and it is the **only** place in the package where one
can be: `compute_business_metric` is the single public surface that returns a
`BusinessMetricResult`, every metric reaches it through one closed registry, and
the assembly it seals through is still the private one in `bindings.py`.

## The trust boundary, and why a dataclass is not a proof

`BusinessMetricComputationContext` carries what a formula needs and a run context
cannot hold: the frozen **records** themselves. It is an ordinary frozen
dataclass, which means anybody can construct one — so holding one proves nothing,
and `compute_business_metric` re-verifies it on **every** call before it reads a
single record. The verification is not a formality:

1. the cohort binding is **rebuilt** from the records and the manifest through
   `build_frozen_cohort_binding` — Phase 10.1's digest domain, the cohort rule,
   the record contract, the strict id ordering, the dedup invariant;
2. that rebuilt binding is required to be *exactly* the one the run context was
   assembled around, by fingerprint and field by field;
3. the run context is re-established through `verify_business_metric_run_context`,
   which recomputes every nested digest and re-checks the URL audit's
   completeness;
4. the benchmark payload rules below are applied.

Only then do the records become readable. There is no path from a raw sequence of
mappings to a formula that skips it, and no cached "verified" flag a second call
could trust.

## The benchmark payload

`BenchmarkBinding` is an *identity*; it deliberately holds no gold rows. So the
rows travel here, as a transient payload, and never enter a `BusinessMetricRun`:
a run is a measurement of the frozen cohort, and republishing somebody's gold
file inside it would make every stored run a copy of a benchmark.

When the bound benchmark says `evaluation_ready=True` the rows are **mandatory**
and are checked against the binding's own count and its `records_fingerprint`,
recomputed here through the canonical primitive. No caller supplies a digest.

## The arithmetic rule, which has no exceptions

Every COMPUTED result in this package is produced by `_computed` below, in one
direction:

    verified facts -> numerator -> denominator -> universe_size
                   -> optional typed support block
                   -> value = numerator / denominator
                   -> the private sealer

The value is **derived**, never passed in. There is no rounding anywhere — a rate
is the quotient, and a number pulled to two decimals is a number nobody can
re-derive. `denominator` and `universe_size` are computed separately and are not
interchangeable: `BROKEN_URL_RATE` divides by the conclusive observations and
states the audited universe, `MEAN_KNOWN_FRESHNESS_SCORE` divides by the records
with a usable date and states the cohort.

A denominator of zero is `N_A` **only** where the contract already provides a
reason for it — an audit that concluded nothing, a cohort with no valid
publication date. Anywhere else it is a hard error, because a rate over nothing
that the contract did not anticipate means the derivation is wrong.

## One derivation per fact

Metrics that share facts share **one** pure derivation. The three Data/AI rates
read a single `DataAiQualificationBreakdown`; the two opportunity-type rates read
a single `OpportunityTypeBreakdown`; presence, provenance and target verdicts are
each derived once. No metric is ever computed from another metric's
`BusinessMetricResult` — a rate assembled out of a published rate inherits its
rounding, its status and its refusals, and stops being a measurement of the data.

## What is never read

No HTTP client and no socket: `BROKEN_URL_RATE` reads the frozen
`UrlAuditBinding` and re-interprets no status code, because
`URL_AUDIT_POLICY_VERSION` already fixed the verdict table and 10.4a enforces it
per observation. No database connection. No human label. No clock: the freshness
as-of date is the snapshot's own, through `FreshnessBinding`, and the score comes
from production's `services.priority.engine.freshness_score` rather than from a
second weight table written here. No fallback field anywhere — not `discovered_at`
for a missing `published_at`, not `record.location` for an unresolved geography,
not the top-level `record.opportunity_type` for an unclassified posting, and not
the description for anything at all.

**UNKNOWN is not FALSE, and absence is not contradiction.** A posting nobody could
place stays in the denominator of `TARGET_COUNTRY_MATCH_RATE` as an UNKNOWN
verdict; a classifier that never read a posting produces `unclassified`, which is
a different count from `OUT_OF_SCOPE`. But evidence that *contradicts itself* — a
segment RESOLVED to no country, a dedup count that disagrees with itself, a
qualification naming a value this contract has never heard of — is a hard error
and never an `N_A`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from services.priority.engine import freshness_score
from services.priority.input_assembly import parse_persisted_date

from .bindings import (
    _build_business_metric_result,
    benchmark_records_fingerprint,
    build_frozen_cohort_binding,
    verify_business_metric_run_context,
)
from .schema import (
    ALWAYS_UNAVAILABLE_METRICS,
    DECLARED_SOURCE_ACTIVE_STATUS,
    FRESHNESS_AGE_BUCKETS,
    FRESHNESS_BUCKET_DAY_BOUNDS,
    FRESHNESS_BUCKET_ORDER,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDimension,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricResult,
    BusinessMetricRunContext,
    BusinessMetricStatus,
    BusinessMetricSupport,
    BusinessMetricUnavailableReason,
    BusinessMetricUniverse,
    BusinessMetricsError,
    DataAiQualificationBreakdown,
    FreshnessAvailabilityWitness,
    FreshnessBucket,
    FreshnessBucketCount,
    FreshnessBucketDistribution,
    FrozenCohortBinding,
    ListingQualityBreakdown,
    OpportunityTypeBreakdown,
    TargetVerdictBreakdown,
    UnknownLocationDiagnostics,
    UrlAuditOutcome,
    frozen_action_url,
    metric_definition,
    validate_country_code,
    validate_text,
)

#: The whole public surface of this module. Everything else is private on
#: purpose: a caller who could reach a resolver, a derivation or the arithmetic
#: helper directly would have a second way to produce a number — one that had
#: not re-verified the computation context and had not gone through the closed
#: registry. Phase 10.3b made the same choice for the same reason.
__all__ = [
    "BusinessMetricComputationContext",
    "build_business_metric_computation_context",
    "compute_business_metric",
    "verify_business_metric_computation_context",
]


# --------------------------------------------------------------------------
# the computation context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessMetricComputationContext:
    """The frozen records, the manifest they came with, and the run's evidence.

    **Transient, and not a proof token.** It is a plain frozen dataclass and
    anybody can construct one with any records they like; what makes a number
    trustworthy is that `compute_business_metric` re-verifies this object before
    reading it, every single time. An earlier shape of this idea would have set a
    `verified=True` flag in the builder — which would mean the guarantee lived in
    whoever remembered to use the builder, rather than in the code that consumes
    it.

    `benchmark_records` is the one payload here that is **not** about the cohort.
    It is the gold benchmark's rows, carried because `BenchmarkBinding` is an
    identity and holds none, and it is used for exactly one thing: establishing
    that the rows in hand are the rows the binding names. It never reaches a
    result, a fingerprint or a stored run.
    """

    records: tuple[Mapping[str, Any], ...]
    manifest: Mapping[str, Any]
    run_context: BusinessMetricRunContext
    benchmark_records: tuple[Mapping[str, Any], ...] | None = None


@dataclass(frozen=True)
class _VerifiedComputation:
    """What survived verification. **Private, and the only thing formulas read.**

    Constructed solely by `_verified` below, so a resolver cannot be handed raw
    records: the type it takes does not exist outside the verification path.
    """

    records: tuple[Mapping[str, Any], ...]
    cohort: FrozenCohortBinding
    run_context: BusinessMetricRunContext
    benchmark_records: tuple[Mapping[str, Any], ...] | None


def _require_record_sequence(
    records: Any, *, subject: str
) -> tuple[Mapping[str, Any], ...]:
    """A sequence of mappings, as an immutable tuple, or a refusal."""
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise BusinessMetricBindingError(f"{subject} are not a sequence: {records!r}")
    for position, record in enumerate(records, start=1):
        if not isinstance(record, Mapping):
            raise BusinessMetricBindingError(
                f"{subject}: record {position} is not a mapping: {record!r}"
            )
    return tuple(records)


def build_business_metric_computation_context(
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    run_context: BusinessMetricRunContext,
    *,
    benchmark_records: Sequence[Mapping[str, Any]] | None = None,
) -> BusinessMetricComputationContext:
    """Bind records and a manifest to the evidence a run assembled, or refuse.

    The frontier of Phase 10.4b. It re-establishes the whole Phase 10.1 /
    Phase 10.4a trust chain before any formula exists to read the records:
    the cohort binding is rebuilt from the records and the manifest, required to
    be identically the one the run context was assembled around, and the run
    context is itself re-verified.

    It returns a context whose verification is nonetheless performed again on
    every use — see the class docstring. Building it here is a convenience and a
    early refusal, never a credential.
    """
    context = BusinessMetricComputationContext(
        records=_require_record_sequence(records, subject="the frozen records"),
        manifest=manifest,
        run_context=run_context,
        benchmark_records=(
            None
            if benchmark_records is None
            else _require_record_sequence(
                benchmark_records, subject="the benchmark rows"
            )
        ),
    )
    _verified(context)
    return context


def _verified(context: Any) -> _VerifiedComputation:
    """Re-establish a computation context from its own inputs. The whole gate.

    In order, and every step refuses rather than repairs:

    1. the object is a computation context and its records are records;
    2. the cohort binding is **rebuilt** from the records and the manifest —
       which re-runs Phase 10.1's digest domain, the cohort rule, the record
       contract, the strict `opportunity_id` ordering and the dedup invariant;
    3. the rebuilt binding must equal the run context's, by its own fingerprint
       and then field by field. The digest alone would be enough to catch edited
       records; the field comparison is what makes the failure legible;
    4. the run context is re-established, recomputing every nested digest and
       re-checking the URL audit's completeness against the cohort;
    5. the benchmark rows are held against the benchmark binding.
    """
    if not isinstance(context, BusinessMetricComputationContext):
        raise BusinessMetricContractError(
            f"{context!r} is not a business metric computation context"
        )
    records = _require_record_sequence(
        context.records, subject="the frozen records"
    )
    if not isinstance(context.run_context, BusinessMetricRunContext):
        raise BusinessMetricContractError(
            f"{context.run_context!r} is not a business metric run context"
        )
    rebuilt = build_frozen_cohort_binding(records, context.manifest)
    bound = context.run_context.frozen_cohort_binding
    if rebuilt.binding_fingerprint != bound.binding_fingerprint:
        raise BusinessMetricBindingError(
            f"these records and manifest derive cohort binding "
            f"{rebuilt.binding_fingerprint} and the run context was assembled "
            f"around {bound.binding_fingerprint}; refusing to compute a metric "
            "over records that are not the records this run's evidence is about"
        )
    for attribute in (
        "dataset_id",
        "dataset_fingerprint",
        "cohort_size",
        "profile_id",
        "profile_fingerprint",
        "dataset_generated_at",
        "freshness_as_of_date",
        "expected_action_urls",
        "absorbed_duplicate_total",
        "records_with_absorbed_duplicates",
        "declared_merged_duplicate_count",
    ):
        derived = getattr(rebuilt, attribute)
        stated = getattr(bound, attribute)
        if derived != stated:
            raise BusinessMetricBindingError(
                f"these records derive {attribute} {derived!r} and the run "
                f"context's cohort binding states {stated!r}"
            )
    verify_business_metric_run_context(context.run_context)
    benchmark_records = _verified_benchmark_records(context)
    return _VerifiedComputation(
        records=records,
        cohort=rebuilt,
        run_context=context.run_context,
        benchmark_records=benchmark_records,
    )


def _verified_benchmark_records(
    context: BusinessMetricComputationContext,
) -> tuple[Mapping[str, Any], ...] | None:
    """Hold the gold rows against the binding that names them, or refuse.

    Three rules, and the first is the one that fails closed:

    * a benchmark that says `evaluation_ready=True` **requires** its rows. The
      binding identifies them by digest and by count, and a recall measured
      against rows nobody produced would be a fraction of an assertion;
    * rows supplied beside any binding are always checked, ready or not. The
      count must match and the digest must match, recomputed here through
      `benchmark_records_fingerprint` — there is no parameter through which a
      caller could state one;
    * rows supplied with no benchmark bound at all are refused: there is nothing
      for them to be the rows *of*.
    """
    binding = context.run_context.benchmark_binding
    supplied = (
        None
        if context.benchmark_records is None
        else _require_record_sequence(
            context.benchmark_records, subject="the benchmark rows"
        )
    )
    if binding is None:
        if supplied is not None:
            raise BusinessMetricBindingError(
                f"{len(supplied)} benchmark row(s) were supplied and this run "
                "binds no benchmark; there is nothing for them to be the rows of"
            )
        return None
    if supplied is None:
        if binding.evaluation_ready:
            raise BusinessMetricBindingError(
                f"benchmark {binding.benchmark_name} {binding.benchmark_version} "
                "is evaluation-ready and no rows were supplied; a ready benchmark "
                "is identified by the digest of its rows, so the rows are what "
                "make it usable evidence rather than a claim about itself"
            )
        return None
    if len(supplied) != binding.record_count:
        raise BusinessMetricBindingError(
            f"benchmark {binding.benchmark_name} names {binding.record_count} "
            f"row(s) and {len(supplied)} were supplied"
        )
    recomputed = benchmark_records_fingerprint(supplied)
    if recomputed != binding.records_fingerprint:
        raise BusinessMetricBindingError(
            f"benchmark {binding.benchmark_name} names rows "
            f"{binding.records_fingerprint} and the rows supplied digest to "
            f"{recomputed}; refusing rows that are not the rows this binding is "
            "about"
        )
    return supplied


def verify_business_metric_computation_context(
    context: BusinessMetricComputationContext,
) -> FrozenCohortBinding:
    """Re-establish a computation context, and return the cohort it derives.

    The public name for the gate above. Returns the **rebuilt** cohort binding
    rather than the one the context carried, so a caller that uses the return
    value is using a fact derived from the records in front of it.
    """
    return _verified(context).cohort


# --------------------------------------------------------------------------
# reading one frozen record, and refusing one of another contract
# --------------------------------------------------------------------------
#
# `build_frozen_cohort_binding` has already established that every record
# carries exactly the field *names* of the Phase 10.1 contract. It says nothing
# about what those fields hold, so every nested shape below is checked here, and
# a value this contract cannot read is a hard error rather than a silently
# skipped record.


def _optional_block(
    record: Mapping[str, Any], field: str, position: int
) -> Mapping[str, Any] | None:
    value = record[field]
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BusinessMetricBindingError(
            f"frozen record {position} states {field}={value!r} "
            f"({type(value).__name__}), which is not the optional block of the "
            "Phase 10.1 record contract"
        )
    return value


def _block_sequence(
    record: Mapping[str, Any], field: str, position: int
) -> tuple[Mapping[str, Any], ...]:
    value = record[field]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise BusinessMetricBindingError(
            f"frozen record {position} states {field}={value!r}, which is not a "
            "sequence"
        )
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise BusinessMetricBindingError(
                f"frozen record {position} states {field}[{index}]={item!r}, "
                "which is not a mapping"
            )
    return tuple(value)


def _block_field(
    block: Mapping[str, Any], field: str, *, subject: str
) -> Any:
    if field not in block:
        raise BusinessMetricBindingError(
            f"{subject} states no {field!r}; refusing to read a block of another "
            "contract"
        )
    return block[field]


def _projected(
    value: Any, projection: Mapping[str, str], *, subject: str
) -> str:
    """One closed vocabulary projected onto this contract's counts, or a refusal.

    A value outside the projection is a **hard error**, never an "other" bucket
    and never an `unclassified`: `unclassified` means the classifier never read
    the posting, and folding a value this build has never heard of into it would
    report an unknown vocabulary as an unrun pipeline.
    """
    text = validate_text(value, subject=subject)
    if text not in projection:
        raise BusinessMetricBindingError(
            f"{subject} is {text!r}, which this contract does not define; the "
            f"values it reads are {sorted(projection)} — refusing to project an "
            "unknown classification onto a count rather than report a vocabulary "
            "this build cannot interpret"
        )
    return projection[text]


# --------------------------------------------------------------------------
# the canonical derivations — one per fact, shared by every metric that needs it
# --------------------------------------------------------------------------


def _present_count(verified: _VerifiedComputation, field: str) -> int:
    """How many records state `field` at all. **Presence, never quality.**

    `is not None` and nothing else: no `.strip()`, no parsing, no judgement about
    whether the content is any good. A description of one space is a description
    the posting carried, and a coverage rate that quietly disqualified it would
    be a content-quality metric wearing a coverage metric's name.
    """
    return sum(1 for record in verified.records if record[field] is not None)


def _action_url_present_count(verified: _VerifiedComputation) -> int:
    """Records for which the frozen protocol selects a URL.

    Through `frozen_action_url` — the contract's own helper — so the precedence
    `application_url, source_url, canonical_url` is applied in exactly one place
    in this repository and this module cannot come to disagree with the audited
    universe about which postings have a URL at all.
    """
    return sum(
        1 for record in verified.records if frozen_action_url(record) is not None
    )


def _observed_source_ids(
    record: Mapping[str, Any], position: int
) -> tuple[str, ...]:
    """The distinct `source_id`s one record was seen at, in first-seen order.

    Distinct: a posting listed twice under one source was seen at one source, and
    counting it twice would let a single collector inflate its own contribution.

    A malformed id — absent, not a string, empty, padded — is a **hard error**.
    Normalising it would invent a source, and dropping it would delete one: both
    are answers to a question this contract did not ask.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for index, source in enumerate(
        _block_sequence(record, "sources", position), start=1
    ):
        source_id = validate_text(
            _block_field(
                source,
                "source_id",
                subject=f"source {index} of frozen record {position}",
            ),
            subject=f"the source_id of source {index} of frozen record {position}",
        )
        if source_id not in seen:
            seen.add(source_id)
            ordered.append(source_id)
    return tuple(ordered)


@dataclass(frozen=True)
class _SourceProvenance:
    """What the cohort says about where its postings were seen. One derivation."""

    #: Records carrying at least one source row.
    provenance_count: int
    #: Records carrying none. The exact complement of the above.
    missing_count: int
    #: Records seen at two or more *distinct* sources.
    multi_source_count: int
    #: source_id -> how many records were seen at it. These sum to more than the
    #: cohort whenever any posting was seen twice, which is the point.
    contributions: Mapping[str, int]
    #: Every source_id the cohort actually observed, in canonical order.
    observed_source_ids: tuple[str, ...]


def _source_provenance(verified: _VerifiedComputation) -> _SourceProvenance:
    provenance = 0
    multi = 0
    contributions: dict[str, int] = {}
    for position, record in enumerate(verified.records, start=1):
        source_ids = _observed_source_ids(record, position)
        raw = _block_sequence(record, "sources", position)
        if raw:
            provenance += 1
        if len(source_ids) >= 2:
            multi += 1
        for source_id in source_ids:
            contributions[source_id] = contributions.get(source_id, 0) + 1
    cohort_size = verified.cohort.cohort_size
    missing = cohort_size - provenance
    if multi > provenance or provenance > cohort_size:
        raise BusinessMetricBindingError(
            f"the cohort derives {multi} multi-source record(s), {provenance} "
            f"with provenance and holds {cohort_size}; these counts cannot "
            "describe one cohort"
        )
    return _SourceProvenance(
        provenance_count=provenance,
        missing_count=missing,
        multi_source_count=multi,
        contributions=dict(contributions),
        observed_source_ids=tuple(sorted(contributions)),
    )


#: The Data/AI qualification vocabulary, projected onto the five counts of
#: `DataAiQualificationBreakdown`. The keys are the production classifier's own
#: members; the tests assert that this mapping's domain is exactly that enum, so
#: a value added upstream is a test failure here rather than a silent miscount.
_DATA_AI_PROJECTION: Mapping[str, str] = {
    "CORE_TARGET": "core_target_count",
    "ADJACENT_TARGET": "adjacent_target_count",
    "OUT_OF_SCOPE": "out_of_scope_count",
    "UNCERTAIN": "uncertain_count",
}

#: The opportunity type vocabulary, projected onto six of the seven counts of
#: `OpportunityTypeBreakdown`; the seventh is `unclassified`, which no value maps
#: to because it is the *absence* of a qualification row.
_OPPORTUNITY_TYPE_PROJECTION: Mapping[str, str] = {
    "PFE": "pfe_count",
    "INTERNSHIP": "internship_count",
    "APPRENTICESHIP": "apprenticeship_count",
    "GRADUATE": "graduate_count",
    "JOB": "job_count",
    "UNKNOWN": "unknown_type_count",
}

#: The listing quality vocabulary, likewise.
_LISTING_QUALITY_PROJECTION: Mapping[str, str] = {
    "NORMAL_LISTING": "normal_listing_count",
    "POSSIBLE_NON_JOB_PAGE": "possible_non_job_page_count",
    "INSUFFICIENT_CONTENT": "insufficient_content_count",
}


def _qualification_counts(
    verified: _VerifiedComputation,
    field: str,
    projection: Mapping[str, str],
    *,
    unclassified_attribute: str,
) -> dict[str, int]:
    """Count one qualification column across the cohort. The one walk.

    `record.qualification is None` means the Data/AI classifier never read the
    posting, and it is counted as `unclassified` — an upstream fact about the
    pipeline, and emphatically not `OUT_OF_SCOPE`, not `UNCERTAIN` and not
    `UNKNOWN`. There is no fallback to the title, the description, the
    `primary_domain`, the top-level `record.opportunity_type` or anything
    downstream: a metric that guessed a classification would be measuring its own
    guess.
    """
    counts = {attribute: 0 for attribute in projection.values()}
    counts[unclassified_attribute] = 0
    for position, record in enumerate(verified.records, start=1):
        qualification = _optional_block(record, "qualification", position)
        if qualification is None:
            counts[unclassified_attribute] += 1
            continue
        attribute = _projected(
            _block_field(
                qualification,
                field,
                subject=f"the qualification of frozen record {position}",
            ),
            projection,
            subject=f"the {field} of frozen record {position}",
        )
        counts[attribute] += 1
    return counts


def _data_ai_breakdown(
    verified: _VerifiedComputation,
) -> DataAiQualificationBreakdown:
    """The five-state Data/AI picture of the cohort. **Derived exactly once.**

    `DATA_AI_RATE`, `CORE_DATA_AI_RATE` and `CLASSIFICATION_COVERAGE_RATE` are
    all fractions of this one object, so the three can never be published from
    three different pictures of the same cohort.
    """
    counts = _qualification_counts(
        verified,
        "qualification",
        _DATA_AI_PROJECTION,
        unclassified_attribute="unclassified_count",
    )
    breakdown = DataAiQualificationBreakdown(**counts)
    _require_partition(breakdown.total, verified, "the Data/AI qualifications")
    return breakdown


def _opportunity_type_breakdown(
    verified: _VerifiedComputation,
) -> OpportunityTypeBreakdown:
    """The seven-state type picture, from `qualification.opportunity_type` only.

    Deliberately **not** the top-level `record.opportunity_type`: that column is
    whatever the collector parsed out of the posting, while this one is what the
    classifier decided, and the metric is about the classifier.
    """
    counts = _qualification_counts(
        verified,
        "opportunity_type",
        _OPPORTUNITY_TYPE_PROJECTION,
        unclassified_attribute="unclassified_type_count",
    )
    breakdown = OpportunityTypeBreakdown(**counts)
    _require_partition(breakdown.total, verified, "the opportunity types")
    return breakdown


def _listing_quality_breakdown(
    verified: _VerifiedComputation,
) -> ListingQualityBreakdown:
    """The four-state listing-quality picture, from the classifier and nothing else.

    No reclassification from the description, the title or the URL: "this page is
    probably not a job posting" is a judgement the qualification made, and
    re-deriving it here would produce a second opinion filed under the first
    one's name.
    """
    counts = _qualification_counts(
        verified,
        "listing_quality",
        _LISTING_QUALITY_PROJECTION,
        unclassified_attribute="unclassified_count",
    )
    breakdown = ListingQualityBreakdown(**counts)
    _require_partition(breakdown.total, verified, "the listing qualities")
    return breakdown


def _require_partition(
    total: int, verified: _VerifiedComputation, subject: str
) -> None:
    """A partition of the cohort covers the cohort. Checked, never assumed."""
    if total != verified.cohort.cohort_size:
        raise BusinessMetricBindingError(
            f"{subject} count {total} record(s) and the cohort holds "
            f"{verified.cohort.cohort_size}; a partition that does not cover its "
            "universe has lost or double-counted a record"
        )


#: The geography segment statuses this contract reads, with what each one is
#: allowed to say about a country. `RESOLVED` named exactly one country;
#: `AMBIGUOUS` found several places the text could mean; `UNKNOWN` found no
#: geographic signal at all. The last two are different failures and neither is
#: ever read as "not in the target country".
_SEGMENT_RESOLVED = "RESOLVED"
_SEGMENT_AMBIGUOUS = "AMBIGUOUS"
_SEGMENT_UNKNOWN = "UNKNOWN"
_SEGMENT_STATUSES: frozenset[str] = frozenset(
    {_SEGMENT_RESOLVED, _SEGMENT_AMBIGUOUS, _SEGMENT_UNKNOWN}
)


@dataclass(frozen=True)
class _RecordGeography:
    """One record's frozen geography, already checked for self-coherence."""

    #: Empty when the resolver produced no segment at all for this posting.
    statuses: tuple[str, ...]
    #: The countries its RESOLVED segments named.
    resolved_countries: tuple[str, ...]

    @property
    def has_segments(self) -> bool:
        return bool(self.statuses)

    @property
    def has_unknown_segment(self) -> bool:
        return _SEGMENT_UNKNOWN in self.statuses

    @property
    def has_ambiguous_segment(self) -> bool:
        return _SEGMENT_AMBIGUOUS in self.statuses

    @property
    def all_resolved(self) -> bool:
        return bool(self.statuses) and all(
            status == _SEGMENT_RESOLVED for status in self.statuses
        )


def _record_geography(
    record: Mapping[str, Any], position: int
) -> _RecordGeography:
    """Read one record's frozen segments, refusing any that contradicts itself.

    The frozen Phase 7A segments and **nothing else**: no live geography
    resolver, and no fallback to `record.location` or `record.country`. Those
    fields are the raw text the resolver was given and the column the collector
    wrote; reading a country out of either would be resolving geography here,
    under a metric's name, with no rule and no version.

    The coherence rule is the resolver's own: a RESOLVED segment names a country,
    and an AMBIGUOUS or UNKNOWN one does not. A segment that claims to have
    resolved to nothing — or to have resolved nothing to a country — is a hard
    error, because the two halves of it cannot both be true.
    """
    statuses: list[str] = []
    countries: list[str] = []
    for index, segment in enumerate(
        _block_sequence(record, "geography_segments", position), start=1
    ):
        subject = f"geography segment {index} of frozen record {position}"
        status = validate_text(
            _block_field(segment, "status", subject=subject),
            subject=f"the status of {subject}",
        )
        if status not in _SEGMENT_STATUSES:
            raise BusinessMetricBindingError(
                f"the status of {subject} is {status!r}; this contract reads "
                f"{sorted(_SEGMENT_STATUSES)}"
            )
        country = _block_field(segment, "country_code", subject=subject)
        if status == _SEGMENT_RESOLVED:
            if country is None:
                raise BusinessMetricBindingError(
                    f"{subject} is {status} and names no country; a resolved "
                    "segment resolved to somewhere"
                )
            countries.append(
                validate_country_code(country, subject=f"the country of {subject}")
            )
        elif country is not None:
            raise BusinessMetricBindingError(
                f"{subject} is {status} and names country {country!r}; only a "
                f"{_SEGMENT_RESOLVED} segment names one, and a country stated "
                "under either of the other two would be a placement the resolver "
                "did not make"
            )
        statuses.append(status)
    return _RecordGeography(
        statuses=tuple(statuses), resolved_countries=tuple(countries)
    )


def _unknown_location_diagnostics(
    verified: _VerifiedComputation,
) -> UnknownLocationDiagnostics:
    """Why the cohort's locations are unknown, in three counts. One derivation.

    A record is unknown-location iff it has **no segment at all** or **at least
    one UNKNOWN segment** — two conditions that cannot both hold, which is why
    the contract can require the numerator to be their sum.

    `AMBIGUOUS` is counted beside them and is **not** in that numerator. A
    resolver that found several possible places did not fail to find one, and a
    rate that merged the two would hide which of them the pipeline should fix.
    """
    ambiguous = 0
    no_segments = 0
    unknown_segment = 0
    for position, record in enumerate(verified.records, start=1):
        geography = _record_geography(record, position)
        if geography.has_ambiguous_segment:
            ambiguous += 1
        if not geography.has_segments:
            no_segments += 1
        elif geography.has_unknown_segment:
            unknown_segment += 1
    return UnknownLocationDiagnostics(
        ambiguous_location_count=ambiguous,
        no_geography_segments_count=no_segments,
        unknown_segment_record_count=unknown_segment,
    )


def _target_verdicts(
    verified: _VerifiedComputation, target_country: str
) -> TargetVerdictBreakdown:
    """The three target verdicts of the cohort, against one bound country.

    The production resolver's own rule, applied to the frozen segments:

        MATCH           at least one segment RESOLVED to the target country
        OUT_OF_TARGET   segments exist, every one RESOLVED, none to the target
        UNKNOWN         everything else

    `OUT_OF_TARGET` is the narrow one on purpose. A posting with one AMBIGUOUS
    segment might be in the target country, so calling it out of target would be
    reading uncertainty as a negative — and UNKNOWN stays in the denominator, so
    the rate carries the pipeline's own uncertainty rather than deleting it.

    **No country is hardcoded here.** `target_country` comes from the bound
    profile target binding; this function knows nothing about Morocco.
    """
    match = 0
    out_of_target = 0
    unknown = 0
    for position, record in enumerate(verified.records, start=1):
        geography = _record_geography(record, position)
        if target_country in geography.resolved_countries:
            match += 1
        elif geography.all_resolved:
            out_of_target += 1
        else:
            unknown += 1
    breakdown = TargetVerdictBreakdown(
        match_count=match,
        out_of_target_count=out_of_target,
        unknown_target_verdict_count=unknown,
    )
    _require_partition(breakdown.total, verified, "the target verdicts")
    return breakdown


@dataclass(frozen=True)
class _Freshness:
    """The cohort's publication ages, as the partition and the score sum.

    Both come from one walk, so the distribution `PUBLICATION_DATE_COVERAGE`
    hosts and the mean `MEAN_KNOWN_FRESHNESS_SCORE` divides can never describe
    different sets of records.
    """

    distribution: FreshnessBucketDistribution
    score_total: float

    @property
    def known_date_count(self) -> int:
        return self.distribution.known_date_count


def _age_bucket(age_days: int) -> FreshnessBucket:
    """Which age bucket a non-negative age falls in, from the frozen bounds.

    Walks `FRESHNESS_BUCKET_DAY_BOUNDS` rather than restating `0..2, 3..7, ...`:
    the partition is the contract's, its import-time assertions already prove it
    has no gap and no overlap, and a second copy of the boundaries here would be
    free to drift from the one the distribution is validated against.
    """
    for bucket in FRESHNESS_AGE_BUCKETS:
        low, high = FRESHNESS_BUCKET_DAY_BOUNDS[bucket]
        if age_days >= low and (high is None or age_days <= high):
            return bucket
    raise BusinessMetricContractError(
        f"an age of {age_days} day(s) falls in no freshness bucket; the frozen "
        "partition is exhaustive over every non-negative age"
    )


def _freshness(verified: _VerifiedComputation, as_of_date: str) -> _Freshness:
    """Classify every record's `published_at` against the snapshot's as-of date.

    `published_at` and the as-of date, and **nothing else**. There is no fallback
    to `discovered_at`, `first_seen_at`, `last_seen_at` or `deadline`: those are
    when we saw a posting, not when it was published, and an age measured from
    the first would report the collector's schedule as the market's freshness.
    No clock is read — the as-of date is the snapshot's own `generated_at`.

    Three states a date can fail in, and the contract keeps two of them apart:

    * `MISSING` — the record states no `published_at`;
    * `INVALID` — it states one this build cannot use: malformed, **or later than
      the as-of date**. A posting claiming to be published after the snapshot was
      taken has a date that cannot be an age, and reading it as "0 days old"
      would make the freshest bucket a collecting point for clock errors.

    The score comes from `services.priority.engine.freshness_score` — production's
    own `priority-freshness-v1` — rather than from a weight table copied into this
    package. A second copy would be a second policy the moment either moved.
    """
    counts = {bucket: 0 for bucket in FRESHNESS_BUCKET_ORDER}
    total = 0.0
    as_of = _calendar_date(as_of_date)
    for position, record in enumerate(verified.records, start=1):
        published = record["published_at"]
        if published is None:
            counts[FreshnessBucket.MISSING] += 1
            continue
        try:
            parsed = parse_persisted_date(published)
        except (ValueError, TypeError):
            parsed = None
        if parsed is None or parsed > as_of:
            counts[FreshnessBucket.INVALID] += 1
            continue
        score, age_days = _freshness_score(parsed, as_of, position)
        counts[_age_bucket(age_days)] += 1
        total += score
    distribution = FreshnessBucketDistribution(
        entries=tuple(
            FreshnessBucketCount(bucket=bucket, count=counts[bucket])
            for bucket in FRESHNESS_BUCKET_ORDER
        ),
        universe_size=verified.cohort.cohort_size,
    )
    return _Freshness(distribution=distribution, score_total=total)


def _calendar_date(value: str) -> date:
    """The as-of date as a `date`, or a refusal in this package's hierarchy."""
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as error:
        raise BusinessMetricBindingError(
            f"the freshness as-of date {value!r} is not a calendar date"
        ) from error


def _freshness_score(
    published: date, as_of: date, position: int
) -> tuple[float, int]:
    """Production's freshness score for one publication date, and its age.

    Wrapped so that `PriorityInputError` — which production raises for a date
    after the evaluation date — cannot escape a public function of this package.
    It is unreachable: `_freshness` has already filed a future date as `INVALID`.
    Reaching it would mean the two disagreed about what "future" means, which is
    a bug to surface rather than a score to substitute.
    """
    try:
        score, age_days = freshness_score(published, as_of)
    except Exception as error:  # noqa: BLE001 - converted, never swallowed
        raise BusinessMetricBindingError(
            f"the frozen publication date of record {position} was refused by "
            f"the production freshness policy: {error}"
        ) from error
    if score is None or age_days is None:
        raise BusinessMetricContractError(
            f"the production freshness policy scored record {position}'s stated "
            "publication date as absent; a date this build parsed is not absent"
        )
    return float(score), int(age_days)


# --------------------------------------------------------------------------
# universe sizes, and the two ways a result is assembled
# --------------------------------------------------------------------------


def _universe_size(
    verified: _VerifiedComputation, key: BusinessMetricKey
) -> int | None:
    """The real size of this metric's universe, or `None` when it has no artefact.

    `None` is the honest answer — never a zero — when the artefact that *defines*
    the universe is absent: nothing counted a set that is not there, and only an
    `N_A` can be in that position. The frozen cohort is always determinable
    because the cohort binding is mandatory.
    """
    universe = metric_definition(key.metric).universe
    context = verified.run_context
    if universe is BusinessMetricUniverse.FROZEN_COHORT:
        return verified.cohort.cohort_size
    if universe is BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE:
        dedup = context.dedup_evidence
        return None if dedup is None else dedup.represented_universe_size
    if universe is BusinessMetricUniverse.URL_AUDITED_UNIVERSE:
        audit = context.url_audit_binding
        return None if audit is None else audit.declared_universe_size
    declared = context.declared_source_universe
    return None if declared is None else len(declared.entries)


def _computed(
    verified: _VerifiedComputation,
    key: BusinessMetricKey,
    *,
    numerator: float,
    denominator: float,
    universe_size: int,
    **support_blocks: Any,
) -> BusinessMetricResult:
    """Seal one COMPUTED result. **The only arithmetic in this package.**

    The value is computed here, from the numerator and the denominator this
    function was given, and there is no parameter through which a caller could
    supply one instead. That is what closes the gap Phase 10.4a left open: the
    result dataclass checks a great many things about a support block and could
    never check that `value` was the quotient, because it had no way to know.

    No rounding. A rate is the quotient it is, and the moment one is pulled to
    two decimals it stops being re-derivable from the numbers printed beside it.

    A zero denominator raises rather than divides. Every legitimate zero — an
    audit that concluded nothing, a cohort with no usable date — is caught by the
    resolver above as a contractual `N_A`, so reaching this is a derivation bug.
    """
    if denominator == 0:
        raise BusinessMetricContractError(
            f"{key.metric} was derived with an empty denominator; every zero "
            "denominator this contract anticipates is an N_A with a reason, so "
            "this one is a bug in the derivation rather than a rate"
        )
    support = BusinessMetricSupport(
        numerator=numerator,
        denominator=denominator,
        universe_size=universe_size,
        **support_blocks,
    )
    return _build_business_metric_result(
        key=key,
        status=BusinessMetricStatus.COMPUTED,
        universe_kind=metric_definition(key.metric).universe,
        context=verified.run_context,
        support=support,
        value=numerator / denominator,
    )


def _unavailable(
    verified: _VerifiedComputation,
    key: BusinessMetricKey,
    reason: BusinessMetricUnavailableReason,
    *,
    witness: FreshnessAvailabilityWitness | None = None,
) -> BusinessMetricResult:
    """Seal one `N_A` result: a reason, the universe's size, and no number.

    No value, no numerator, no denominator and no fabricated zero. The universe
    size is derived rather than chosen, so an unavailable metric still says how
    big the question was whenever anything could answer that.
    """
    support = BusinessMetricSupport(
        universe_size=_universe_size(verified, key),
        freshness_availability_witness=witness,
    )
    return _build_business_metric_result(
        key=key,
        status=BusinessMetricStatus.N_A,
        universe_kind=metric_definition(key.metric).universe,
        context=verified.run_context,
        support=support,
        reason=reason,
    )


def _cohort_rate(
    verified: _VerifiedComputation,
    key: BusinessMetricKey,
    numerator: int,
    **support_blocks: Any,
) -> BusinessMetricResult:
    """A count over the whole frozen cohort — the shape most of these metrics take.

    The denominator and the universe are both the cohort size, and they are the
    same number here because the contract says so for these metrics, not because
    the two concepts are interchangeable.
    """
    cohort_size = verified.cohort.cohort_size
    return _computed(
        verified,
        key,
        numerator=numerator,
        denominator=cohort_size,
        universe_size=cohort_size,
        **support_blocks,
    )


# --------------------------------------------------------------------------
# the resolvers — presence, action URLs and provenance
# --------------------------------------------------------------------------


def _resolve_description_presence(verified, key):
    return _cohort_rate(verified, key, _present_count(verified, "description"))


def _resolve_location_text_presence(verified, key):
    return _cohort_rate(verified, key, _present_count(verified, "location"))


def _resolve_deadline_coverage(verified, key):
    return _cohort_rate(verified, key, _present_count(verified, "deadline"))


def _resolve_action_url_presence(verified, key):
    return _cohort_rate(verified, key, _action_url_present_count(verified))


def _resolve_missing_action_url(verified, key):
    """The exact complement of the presence rate, derived from the same count."""
    cohort_size = verified.cohort.cohort_size
    present = _action_url_present_count(verified)
    missing = cohort_size - present
    if present + missing != cohort_size:
        raise BusinessMetricContractError(
            f"{present} record(s) select an action URL and {missing} do not, in "
            f"a cohort of {cohort_size}; every record either selects one or does "
            "not"
        )
    return _cohort_rate(verified, key, missing)


def _resolve_source_provenance_coverage(verified, key):
    return _cohort_rate(verified, key, _source_provenance(verified).provenance_count)


def _resolve_missing_source_provenance(verified, key):
    return _cohort_rate(verified, key, _source_provenance(verified).missing_count)


def _resolve_multi_source_record(verified, key):
    return _cohort_rate(
        verified, key, _source_provenance(verified).multi_source_count
    )


def _resolve_observed_source_contribution(verified, key):
    """One source's share of the cohort, for a source the cohort actually observed.

    The dimension value must name a `source_id` that appears in the frozen
    records. A source that was declared but never observed, or one nobody has
    heard of, is a **hard error** and not a contribution of zero: zero would
    assert that this source found nothing, which is a measurement, and no
    measurement was made.

    These rates may sum to more than 1.0 across sources, and that is correct: a
    posting seen at two sources contributes to both.
    """
    if key.dimension_kind is not BusinessMetricDimension.SOURCE_ID:
        raise BusinessMetricContractError(
            f"{key.metric} is stated per source and this key carries dimension "
            f"{key.dimension_kind}"
        )
    provenance = _source_provenance(verified)
    source_id = key.dimension_value
    if source_id not in provenance.contributions:
        observed = list(provenance.observed_source_ids)
        raise BusinessMetricBindingError(
            f"{key.metric} was asked for source {source_id!r}, which no record in "
            f"this cohort was seen at; the sources it observed are "
            f"{observed[:10]}{'...' if len(observed) > 10 else ''} — a "
            "contribution of 0.0 would assert that this source found nothing, "
            "and nothing was measured about it"
        )
    return _cohort_rate(verified, key, provenance.contributions[source_id])


# --------------------------------------------------------------------------
# the resolvers — Data/AI, opportunity type, listing quality
# --------------------------------------------------------------------------


def _resolve_data_ai_rate(verified, key):
    """Core plus adjacent. Always COMPUTED over a coherent cohort, 0.0 included."""
    breakdown = _data_ai_breakdown(verified)
    return _cohort_rate(
        verified, key, breakdown.data_ai_count, data_ai_breakdown=breakdown
    )


def _resolve_core_data_ai_rate(verified, key):
    breakdown = _data_ai_breakdown(verified)
    return _cohort_rate(
        verified, key, breakdown.core_target_count, data_ai_breakdown=breakdown
    )


def _resolve_classification_coverage(verified, key):
    """How much of the cohort the classifier reached. `UNCERTAIN` counts as covered.

    The classifier read the posting and hedged; that is a classification, and
    calling it uncovered would report a decision as an absence.
    """
    breakdown = _data_ai_breakdown(verified)
    return _cohort_rate(
        verified, key, breakdown.classified_count, data_ai_breakdown=breakdown
    )


def _resolve_pfe_or_internship(verified, key):
    """PFE plus internship. **Apprenticeships are deliberately excluded.**

    A different contract, a different duration and a different application
    season; folding them in would inflate the one number this project exists to
    move. The count sits beside it in the breakdown for anybody who wants the
    wider figure and says so.
    """
    breakdown = _opportunity_type_breakdown(verified)
    return _cohort_rate(
        verified,
        key,
        breakdown.pfe_or_internship_count,
        opportunity_type_breakdown=breakdown,
    )


def _resolve_opportunity_type_coverage(verified, key):
    """How much of the cohort was typed. `UNKNOWN` counts as typed.

    The classifier read the posting and could not determine its type, which is a
    decision; only a posting it never read is uncovered.
    """
    breakdown = _opportunity_type_breakdown(verified)
    return _cohort_rate(
        verified,
        key,
        breakdown.typed_count,
        opportunity_type_breakdown=breakdown,
    )


def _resolve_normal_listing(verified, key):
    breakdown = _listing_quality_breakdown(verified)
    return _cohort_rate(
        verified,
        key,
        breakdown.normal_listing_count,
        listing_quality_breakdown=breakdown,
    )


# --------------------------------------------------------------------------
# the resolvers — geography
# --------------------------------------------------------------------------


def _resolve_unknown_location(verified, key):
    diagnostics = _unknown_location_diagnostics(verified)
    return _cohort_rate(
        verified,
        key,
        diagnostics.unknown_location_count,
        unknown_location_diagnostics=diagnostics,
    )


def _resolve_target_country_match(verified, key):
    """The share of the cohort in the profile's target country.

    Two refusals, distinguished by condition rather than by taste: no binding at
    all is `TARGET_SCOPE_BINDING_MISSING`, and a coherent binding whose target
    country is unknown is `TARGET_COUNTRY_UNKNOWN`. Neither is a zero — a match
    rate against an unknown target would read as "none of these postings is where
    they want to work".

    The denominator is the **whole cohort**, UNKNOWN verdicts included.
    """
    binding = verified.run_context.profile_target_binding
    if binding is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING,
        )
    if binding.country_code is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN,
        )
    breakdown = _target_verdicts(verified, binding.country_code)
    return _cohort_rate(
        verified,
        key,
        breakdown.match_count,
        target_verdict_breakdown=breakdown,
    )


# --------------------------------------------------------------------------
# the resolvers — deduplication
# --------------------------------------------------------------------------


def _dedup_facts(verified: _VerifiedComputation) -> tuple[int, int, int]:
    """`S`, `D` and `C` from the verified dedup evidence, held against each other.

    Read from `DedupEvidence` and never from `record.absorbed_duplicate_ids`
    directly. The evidence's *presence* is what a run decides, and a resolver
    that fell back to the records when it was absent would compute the two dedup
    metrics from the cohort binding everything else uses — quietly turning a
    stated `N_A` into a number.

    Most of these invariants are established by the evidence's own validator; they
    are restated here because this is where the two rates are formed, and a
    contradiction must stop a number rather than produce one.
    """
    evidence = verified.run_context.dedup_evidence
    cohort_size = evidence.cohort_size
    absorbed = evidence.absorbed_duplicate_total
    with_duplicates = evidence.records_with_absorbed_duplicates
    if cohort_size <= 0:
        raise BusinessMetricBindingError(
            "the dedup evidence counts a cohort of "
            f"{cohort_size}; there is no rate over an empty cohort"
        )
    if absorbed != evidence.declared_merged_duplicate_count:
        raise BusinessMetricBindingError(
            f"the dedup evidence counts {absorbed} absorbed duplicate(s) and "
            f"declares {evidence.declared_merged_duplicate_count} merged "
            "exclusion(s)"
        )
    if with_duplicates > cohort_size:
        raise BusinessMetricBindingError(
            f"{with_duplicates} record(s) absorbed a duplicate in a cohort of "
            f"{cohort_size}"
        )
    if absorbed == 0 and with_duplicates != 0:
        raise BusinessMetricBindingError(
            f"no duplicate was absorbed and {with_duplicates} record(s) claim to "
            "have absorbed one"
        )
    if with_duplicates > 0 and absorbed == 0:
        raise BusinessMetricBindingError(
            f"{with_duplicates} record(s) absorbed at least one duplicate and the "
            "total absorbed is zero"
        )
    if absorbed > 0 and with_duplicates > absorbed:
        raise BusinessMetricBindingError(
            f"{with_duplicates} record(s) absorbed at least one duplicate, which "
            f"cannot exceed the {absorbed} absorbed in total"
        )
    return cohort_size, absorbed, with_duplicates


def _resolve_applied_duplicate_rate(verified, key):
    """`D / (S + D)` over the universe the collector actually saw.

    The merged tombstones are outside the cohort by construction, so the same
    numerator over the cohort would be a rate whose denominator excludes half of
    what it counts. With no evidence bound it is `N_A / DEDUP_EVIDENCE_MISSING`
    and states **no** universe size: `S + D` is not establishable without the
    artefact that holds `D`.
    """
    if verified.run_context.dedup_evidence is None:
        return _unavailable(
            verified, key, BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING
        )
    cohort_size, absorbed, _ = _dedup_facts(verified)
    represented = cohort_size + absorbed
    return _computed(
        verified,
        key,
        numerator=absorbed,
        denominator=represented,
        universe_size=represented,
    )


def _resolve_duplicate_cluster_rate(verified, key):
    """`C / S`: how many cohort records absorbed at least one duplicate.

    A different question from how many duplicates there were, and stated over the
    cohort — so with no evidence bound it is still `N_A`, and it still states the
    cohort's size, which is determinable without the dedup artefact.
    """
    if verified.run_context.dedup_evidence is None:
        return _unavailable(
            verified, key, BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING
        )
    cohort_size, _, with_duplicates = _dedup_facts(verified)
    return _computed(
        verified,
        key,
        numerator=with_duplicates,
        denominator=cohort_size,
        universe_size=cohort_size,
    )


# --------------------------------------------------------------------------
# the resolvers — the URL audit
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _UrlAuditFacts:
    """The audit reduced to five counts, with its invariants established."""

    total: int
    attempted: int
    valid: int
    broken: int
    inconclusive: int

    @property
    def conclusive(self) -> int:
        return self.valid + self.broken


def _url_audit_facts(verified: _VerifiedComputation) -> _UrlAuditFacts:
    """Count the frozen audit's observations. **No status code is re-read.**

    `URL_AUDIT_POLICY_VERSION` fixed the verdict table — 2xx is VALID, 404 and
    410 are BROKEN, everything else is INCONCLUSIVE — and 10.4a enforces it on
    every observation as it is constructed, so a `VALID` on a 404 does not exist
    to be found here. This function therefore counts outcomes and does not
    interpret responses: re-deriving a verdict would be a second policy, and the
    two would disagree the moment either moved.
    """
    binding = verified.run_context.url_audit_binding
    total = len(binding.observations)
    attempted = binding.attempted_count
    valid = sum(
        1
        for item in binding.observations
        if item.outcome is UrlAuditOutcome.VALID
    )
    broken = sum(
        1
        for item in binding.observations
        if item.outcome is UrlAuditOutcome.BROKEN
    )
    inconclusive = sum(
        1
        for item in binding.observations
        if item.outcome is UrlAuditOutcome.INCONCLUSIVE
    )
    facts = _UrlAuditFacts(
        total=total,
        attempted=attempted,
        valid=valid,
        broken=broken,
        inconclusive=inconclusive,
    )
    if valid + broken + inconclusive != total:
        raise BusinessMetricBindingError(
            f"the URL audit holds {total} observation(s) and classifies "
            f"{valid + broken + inconclusive}; every observation has exactly one "
            "outcome"
        )
    if not broken <= facts.conclusive <= attempted <= total:
        raise BusinessMetricBindingError(
            f"the URL audit counts {broken} broken, {facts.conclusive} "
            f"conclusive, {attempted} attempted and {total} observed; a "
            "conclusion requires an attempt and a broken URL is a conclusion"
        )
    if facts.conclusive != binding.conclusive_count:
        raise BusinessMetricBindingError(
            f"the URL audit derives {facts.conclusive} conclusive observation(s) "
            f"and reports {binding.conclusive_count}"
        )
    return facts


def _resolve_url_audit_coverage(verified, key):
    """`attempted / observed`. Stays COMPUTED even when nothing was attempted.

    That is precisely the fact this metric exists to report: an audit in which
    every URL was withheld by robots has a coverage of 0.0, which is a
    measurement, not a missing one.
    """
    if verified.run_context.url_audit_binding is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING,
        )
    facts = _url_audit_facts(verified)
    return _computed(
        verified,
        key,
        numerator=facts.attempted,
        denominator=facts.total,
        universe_size=facts.total,
    )


def _resolve_url_conclusive_coverage(verified, key):
    """`(valid + broken) / observed`. Also COMPUTED at 0.0, for the same reason."""
    if verified.run_context.url_audit_binding is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING,
        )
    facts = _url_audit_facts(verified)
    return _computed(
        verified,
        key,
        numerator=facts.conclusive,
        denominator=facts.total,
        universe_size=facts.total,
    )


def _resolve_broken_url_rate(verified, key):
    """`broken / conclusive`, over the **audited** universe.

    The denominator is the observations that decided something and the universe
    is every observation, and the two are deliberately different numbers: a
    posting whose URL was refused, rate-limited or never reached says nothing
    about whether it is broken, so it belongs in the universe and not under the
    line.

    With nothing conclusive the rate would be 0.0 by construction — "nothing is
    broken", on the strength of having established nothing — so it is
    `N_A / NO_CONCLUSIVE_URL_AUDIT`, and it still states the audited universe's
    size.
    """
    if verified.run_context.url_audit_binding is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING,
        )
    facts = _url_audit_facts(verified)
    if facts.conclusive == 0:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT,
        )
    return _computed(
        verified,
        key,
        numerator=facts.broken,
        denominator=facts.conclusive,
        universe_size=facts.total,
    )


# --------------------------------------------------------------------------
# the resolvers — freshness
# --------------------------------------------------------------------------


def _resolve_publication_date_coverage(verified, key):
    """`known dates / cohort`, and the **host of the eight-state distribution**.

    Stays COMPUTED even when not one date is valid: the rate is then 0.0 and the
    partition still says, bucket by bucket, what the cohort's dates are — which
    is exactly the situation that picture is most worth having, and exactly the
    one the mean cannot report.
    """
    binding = verified.run_context.freshness_binding
    if binding is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING,
        )
    freshness = _freshness(verified, binding.as_of_date)
    return _cohort_rate(
        verified,
        key,
        freshness.known_date_count,
        freshness_distribution=freshness.distribution,
    )


def _resolve_mean_known_freshness(verified, key):
    """The mean `priority-freshness-v1` score over records with a usable date.

    The denominator is `known_date_count` and the universe is the **cohort**:
    this is an average over the records that have a date, stated about a cohort
    in which some do not. With no valid date anywhere it is
    `N_A / NO_VALID_PUBLICATION_DATES`, carrying the witness of zero the contract
    requires — the one refusal no binding can verify, because it is a fact about
    records rather than about artefacts.

    No rounding: the mean is the quotient.
    """
    binding = verified.run_context.freshness_binding
    if binding is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING,
        )
    freshness = _freshness(verified, binding.as_of_date)
    known = freshness.known_date_count
    if known == 0:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES,
            witness=FreshnessAvailabilityWitness(known_date_count=0),
        )
    return _computed(
        verified,
        key,
        numerator=freshness.score_total,
        denominator=known,
        universe_size=verified.cohort.cohort_size,
    )


# --------------------------------------------------------------------------
# the resolvers — the declared source universe
# --------------------------------------------------------------------------


def _resolve_active_declared_source(verified, key):
    """`active entries / declared entries`, over the declared source map.

    Counted from the `DeclaredSourceUniverseEvidence` Phase 10.4a already reduced
    — the YAML is not re-read here, and `config/sources.yaml` is not consulted:
    the binding identifies one exact revision of one exact map, and re-reading
    either file would measure whatever is on disk now under that binding's name.
    """
    universe = verified.run_context.declared_source_universe
    if universe is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.DECLARED_SOURCE_UNIVERSE_MISSING,
        )
    declared = len(universe.entries)
    active = sum(
        1
        for entry in universe.entries
        if entry.integration_status == DECLARED_SOURCE_ACTIVE_STATUS
    )
    return _computed(
        verified,
        key,
        numerator=active,
        denominator=declared,
        universe_size=declared,
    )


def _resolve_declared_source_discovery_recall(verified, key):
    """Fail-closed, and in v1 it never reaches a number.

    Two ordinary refusals first: no declared universe is
    `DECLARED_SOURCE_UNIVERSE_MISSING`, and a benchmark that is absent or says it
    is not evaluation-ready is `SOURCE_BENCHMARK_NOT_EVALUATION_READY` — the
    state of the world today, and an honest one.

    And then the case this function exists to refuse. A benchmark that *is*
    evaluation-ready makes its rows available, and rows are **not enough** to
    measure discovery recall. The Phase 10.1 snapshot holds only the active,
    non-duplicate cohort, and the gold rows may be historical, so a gold
    opportunity that is not in the cohort is not evidence that we failed to
    discover it — it may have been collected and since expired, or merged as a
    duplicate, or excluded by the cohort rule.

    Inventing a match on `source_url`, on the application URL, on title plus
    organization, on hostname, on `benchmark_id` or by fuzzy comparison would
    manufacture exactly that evidence. Each would produce a plausible number, and
    the number would be a measurement of the matching rule somebody chose.

    So this raises. It is not `N_A`: every reason code in this contract names
    evidence that is absent or insufficient in a way the contract anticipated,
    and "a ready benchmark arrived before the artefact that records FOUND /
    NOT_FOUND per declared source" is an implementation gap. Inventing a reason
    code for it here would freeze that gap into the contract as a normal state.
    Phase 10.4b deliberately does not create that artefact.
    """
    universe = verified.run_context.declared_source_universe
    if universe is None:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.DECLARED_SOURCE_UNIVERSE_MISSING,
        )
    benchmark = verified.run_context.benchmark_binding
    if benchmark is None or not benchmark.evaluation_ready:
        return _unavailable(
            verified,
            key,
            BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY,
        )
    raise BusinessMetricContractError(
        f"benchmark {benchmark.benchmark_name} {benchmark.benchmark_version} is "
        f"evaluation-ready and {key.metric} still has no discovery evidence to "
        "measure against. Its rows say which opportunities should exist; they do "
        "not say, per declared source, whether this pipeline found them — and the "
        "frozen cohort cannot answer that either, because it holds only active, "
        "non-duplicate postings while the gold rows may be historical. Refusing "
        "to infer a match from a source URL, an application URL, a hostname, a "
        "benchmark id or a title: each would measure the rule that was chosen "
        "rather than the discovery. A versioned FOUND / NOT_FOUND discovery "
        "evidence artefact is what this metric is waiting for, and this slice "
        "does not invent one"
    )


# --------------------------------------------------------------------------
# the resolvers — skills
# --------------------------------------------------------------------------


def _resolve_opportunity_skill_coverage(verified, key):
    """Always `N_A / OPPORTUNITY_SKILLS_NOT_FROZEN` under contract v1.

    Per-opportunity skill requirements are not in the Phase 10.1 record contract,
    so this metric has no input at all — absent, not empty. There is no fallback:
    parsing skills out of a description, or reading them off the profile, the
    matching assessment or a model, would replace a missing input with a
    fabricated one and report the fabrication's coverage.

    Freezing opportunity skills into the dataset is what would remove this entry
    from `ALWAYS_UNAVAILABLE_METRICS`; until then a COMPUTED result is refused by
    the contract itself.
    """
    return _unavailable(verified, key, ALWAYS_UNAVAILABLE_METRICS[key.metric])


# --------------------------------------------------------------------------
# the closed resolver registry
# --------------------------------------------------------------------------

#: **Every metric name, exactly once, with exactly one path to a number.**
#:
#: Closed and exhaustive, asserted at import. There is deliberately no default
#: handler, no `getattr` by name, no dynamic lookup and no per-metric public
#: function. Each of those would be a way for a metric to acquire an
#: implementation nobody reviewed — and the worst of them, a default handler,
#: would turn "this build has no formula for that" into an `N_A`, which is a
#: refusal the contract reserves for evidence that is genuinely unavailable.
#:
#: A missing handler is an implementation error and must look like one.
_METRIC_RESOLVERS: Mapping[
    BusinessMetricName,
    Callable[[_VerifiedComputation, BusinessMetricKey], BusinessMetricResult],
] = {
    BusinessMetricName.DESCRIPTION_PRESENCE_RATE: _resolve_description_presence,
    BusinessMetricName.LOCATION_TEXT_PRESENCE_RATE: _resolve_location_text_presence,
    BusinessMetricName.DEADLINE_DATE_COVERAGE: _resolve_deadline_coverage,
    BusinessMetricName.ACTION_URL_PRESENCE_RATE: _resolve_action_url_presence,
    BusinessMetricName.MISSING_ACTION_URL_RATE: _resolve_missing_action_url,
    BusinessMetricName.SOURCE_PROVENANCE_COVERAGE_RATE: (
        _resolve_source_provenance_coverage
    ),
    BusinessMetricName.MISSING_SOURCE_PROVENANCE_RATE: (
        _resolve_missing_source_provenance
    ),
    BusinessMetricName.MULTI_SOURCE_RECORD_RATE: _resolve_multi_source_record,
    BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION: (
        _resolve_observed_source_contribution
    ),
    BusinessMetricName.DATA_AI_RATE: _resolve_data_ai_rate,
    BusinessMetricName.CORE_DATA_AI_RATE: _resolve_core_data_ai_rate,
    BusinessMetricName.CLASSIFICATION_COVERAGE_RATE: _resolve_classification_coverage,
    BusinessMetricName.PFE_OR_INTERNSHIP_RATE: _resolve_pfe_or_internship,
    BusinessMetricName.OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE: (
        _resolve_opportunity_type_coverage
    ),
    BusinessMetricName.NORMAL_LISTING_RATE: _resolve_normal_listing,
    BusinessMetricName.UNKNOWN_LOCATION_RATE: _resolve_unknown_location,
    BusinessMetricName.TARGET_COUNTRY_MATCH_RATE: _resolve_target_country_match,
    BusinessMetricName.APPLIED_DUPLICATE_RATE: _resolve_applied_duplicate_rate,
    BusinessMetricName.DUPLICATE_CLUSTER_RATE: _resolve_duplicate_cluster_rate,
    BusinessMetricName.URL_AUDIT_COVERAGE_RATE: _resolve_url_audit_coverage,
    BusinessMetricName.URL_CONCLUSIVE_COVERAGE_RATE: _resolve_url_conclusive_coverage,
    BusinessMetricName.BROKEN_URL_RATE: _resolve_broken_url_rate,
    BusinessMetricName.PUBLICATION_DATE_COVERAGE: _resolve_publication_date_coverage,
    BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE: _resolve_mean_known_freshness,
    BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE: _resolve_active_declared_source,
    BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL: (
        _resolve_declared_source_discovery_recall
    ),
    BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE: (
        _resolve_opportunity_skill_coverage
    ),
}

#: Exhaustive, and asserted at import rather than discovered at run time: a
#: metric added to the registry with no resolver must fail to import, not fail
#: the one run that happened to ask for it.
assert set(_METRIC_RESOLVERS) == set(BusinessMetricName)


# --------------------------------------------------------------------------
# the one public way to produce a business metric result
# --------------------------------------------------------------------------


def compute_business_metric(
    context: BusinessMetricComputationContext,
    key: BusinessMetricKey,
) -> BusinessMetricResult:
    """Compute one business metric over a verified computation context.

    **The only public surface in this package that returns a result.** No other
    function exports a way to supply a `value`, a `numerator`, a `denominator`, a
    `status`, a `support` block or a `reason`, so there is no path to publishing
    an arbitrary float with a valid digest and a complete evidence trail wrapped
    around it.

    The context is re-verified here, before a single record is read, however it
    was obtained: it is an ordinary dataclass, anybody can construct one, and
    holding one is therefore not evidence of anything. The cohort binding is
    rebuilt from the records, held against the one the run context was assembled
    around, and the run context is re-established with every nested digest
    recomputed.

    Then exactly one resolver runs, found in a closed registry rather than by
    name. A COMPUTED result's value is derived from the fraction the resolver
    established; an `N_A` result carries a reason the contract entitles this
    metric to state and whose condition holds. A contradiction in the evidence —
    a partition that does not cover the cohort, a dedup count at odds with
    itself, a classification this contract has never heard of, a dimension naming
    a source nobody observed — raises. It is never reported as `N_A`.
    """
    verified = _verified(context)
    if not isinstance(key, BusinessMetricKey):
        raise BusinessMetricContractError(
            f"{key!r} is not a business metric key"
        )
    try:
        resolver = _METRIC_RESOLVERS[key.metric]
    except KeyError as error:  # pragma: no cover - the import assertion prevents it
        raise BusinessMetricContractError(
            f"{key.metric} has no resolver in this build; every metric name has "
            "exactly one, and a missing one is an implementation error rather "
            "than an unavailable metric"
        ) from error
    try:
        return resolver(verified, key)
    except BusinessMetricsError:
        raise
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        ZeroDivisionError,
        ArithmeticError,
    ) as error:
        # A low-level failure inside a derivation is a bug in this module, and it
        # leaves through this package's own hierarchy rather than as a stray
        # KeyError. It is never converted into an N_A: a metric that could not be
        # computed because the code broke is not a metric whose evidence was
        # unavailable.
        raise BusinessMetricBindingError(
            f"computing {key.metric} over this cohort failed: "
            f"{type(error).__name__}: {error}"
        ) from error


def _observed_source_id_universe(
    verified: _VerifiedComputation,
) -> tuple[str, ...]:
    """Every `source_id` the frozen cohort actually observed, canonically ordered.

    Private, and shared with `run.py`, which derives a run's dimensional keys
    from it. It is deliberately not public: the set of keys a complete run
    answers is the run builder's to derive, not a caller's to choose.
    """
    return _source_provenance(verified).observed_source_ids
