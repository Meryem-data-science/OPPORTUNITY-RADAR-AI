"""The Phase 10.4b formulas: what each of the twenty-seven metrics counts.

Phase 10.4a's suite asserts what makes a number *reportable*. This one asserts
what the numbers **are** — the numerator, the denominator, the universe and the
structured support of every metric family, over invented cohorts whose contents
are chosen so the expected value can be read off the fixture by hand.

Four properties are tested more than once on purpose, because they are the ones a
later change is most likely to erode:

* the resolver registry is closed and exhaustive, and no second public path to a
  result exists;
* `value` is always the quotient of the fraction beside it, never a number a
  caller supplied;
* a `denominator` and a `universe_size` are different fields and are *checked*
  to differ where the contract says they do;
* an incoherence in the evidence raises, and is never reported as `N_A`.

Nothing here opens the operational database, reads a human label, makes an HTTP
request or touches the 394 real opportunities. The fixtures live in
`tests/unit/business_metric_fixtures.py`.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

import evaluation.business_metrics as package
from evaluation.business_metrics import (
    ALWAYS_UNAVAILABLE_METRICS,
    DECLARED_SOURCE_ACTIVE_STATUS,
    EXPECTED_FRESHNESS_POLICY_VERSION,
    FRESHNESS_BUCKET_DAY_BOUNDS,
    BusinessMetricBindingError,
    BusinessMetricContractError,
    BusinessMetricDimension,
    BusinessMetricKey,
    BusinessMetricName,
    BusinessMetricStatus,
    BusinessMetricUnavailableReason,
    BusinessMetricUniverse,
    BusinessMetricsError,
    FreshnessBucket,
    UrlAuditOutcome,
    build_business_metric_computation_context,
    compute_business_metric,
    verify_business_metric_computation_context,
)
from evaluation.business_metrics.formulas import _METRIC_RESOLVERS
from tests.unit.business_metric_fixtures import (
    AS_OF_DATE,
    DEFAULT_RECORDS,
    OTHER_COUNTRY,
    TARGET_COUNTRY,
    FakeSourceEntry,
    benchmark_rows,
    build_frozen_cohort_binding,
    cohort,
    computation,
    declared_universe,
    manifest_payload,
    observation,
    qualification,
    ready_benchmark,
    record,
    run_context,
    segment,
    source,
    target_binding,
    url_audit,
)

SOURCE_ID = BusinessMetricDimension.SOURCE_ID


def key_of(metric, **kwargs):
    return BusinessMetricKey(metric, **kwargs)


def value_of(context, metric, **kwargs):
    """The result of one metric over one computation context."""
    return compute_business_metric(context, key_of(metric, **kwargs))


def only(records, **kwargs):
    """A computation context over exactly these records, everything assembled."""
    return computation(tuple(records), **kwargs)


# ====================================================================
# the dispatcher and the public surface
# ====================================================================


def test_the_resolver_registry_is_exhaustive_and_closed() -> None:
    assert set(_METRIC_RESOLVERS) == set(BusinessMetricName)
    assert len(_METRIC_RESOLVERS) == len(BusinessMetricName) == 27
    for metric, resolver in _METRIC_RESOLVERS.items():
        assert callable(resolver), metric


def test_every_metric_is_reachable_through_the_public_dispatcher() -> None:
    """All twenty-seven produce a contract-valid result over a full context."""
    context = computation()
    for metric in BusinessMetricName:
        if metric in package.DIMENSIONAL_BUSINESS_METRICS:
            result = value_of(
                context,
                metric,
                dimension_kind=SOURCE_ID,
                dimension_value="alpha_board",
            )
        else:
            result = value_of(context, metric)
        assert result.key.metric is metric
        assert result.status in (
            BusinessMetricStatus.COMPUTED,
            BusinessMetricStatus.N_A,
        )
        if result.status is BusinessMetricStatus.COMPUTED:
            assert result.value == pytest.approx(
                result.support.numerator / result.support.denominator
            )
        else:
            assert result.value is None
            assert result.reason is not None


def test_the_package_exposes_no_second_way_to_produce_a_result() -> None:
    """`compute_business_metric` is the only public function returning a result.

    The regression Phase 10.3b's suite guards for the ranking metrics, restated
    here: a public helper that took a `value`, a `numerator` or a `status` would
    be an API for publishing an arbitrary float with a complete evidence trail
    wrapped around it.
    """
    def parameters_of(item):
        try:
            return set(inspect.signature(item).parameters)
        except (TypeError, ValueError):  # pragma: no cover - builtins
            return set()

    public = [
        (name, getattr(package, name))
        for name in package.__all__
        if callable(getattr(package, name)) and not isinstance(
            getattr(package, name), type
        )
    ]

    # `numerator` and `denominator` are unambiguous: nothing in this package has
    # any business receiving either, whatever it is called or what it does.
    for name, item in public:
        leaked = parameters_of(item) & {"numerator", "denominator"}
        assert not leaked, f"{name} accepts {sorted(leaked)}"

    # And no *producer* — anything that makes an artefact rather than rendering,
    # coercing or verifying one — may receive a value, a status or a reason
    # either. `compute_business_metric` is the exception that proves the rule: it
    # takes a context and a key, and derives all three.
    producer_prefixes = ("build_", "compute_", "create_", "make_", "seal_", "new_")
    for name, item in public:
        if not name.startswith(producer_prefixes):
            continue
        leaked = parameters_of(item) & {"value", "status", "support", "reason"}
        assert not leaked, f"{name} accepts {sorted(leaked)}"
    assert parameters_of(package.compute_business_metric) == {"context", "key"}
    # And the resolvers themselves are not exported under any name.
    for name in package.__all__:
        assert not name.startswith("_resolve"), name
    assert "_METRIC_RESOLVERS" not in package.__all__
    assert not hasattr(package, "_METRIC_RESOLVERS")


def test_compute_reverifies_a_forged_computation_context() -> None:
    """A context is a dataclass, so holding one proves nothing.

    The records are swapped after the context was built. The manifest no longer
    digests to what it claims, and the metric is refused rather than computed
    over whatever arrived.
    """
    context = computation()
    forged = replace(
        context, records=(record(1), record(2), record(3), record(4))
    )
    with pytest.raises(BusinessMetricBindingError, match="refusing a snapshot"):
        value_of(forged, BusinessMetricName.DATA_AI_RATE)


def test_compute_refuses_a_coherent_snapshot_of_another_cohort() -> None:
    """The sharper case: records *and* manifest are internally consistent.

    Nothing about this snapshot is malformed — it simply is not the snapshot the
    run context's evidence was assembled around, so the rebuilt cohort binding
    disagrees with the bound one and the metric is refused.
    """
    other = (record(1, description="another snapshot entirely"), record(2))
    context = computation()
    forged = replace(context, records=other, manifest=manifest_payload(other))
    with pytest.raises(BusinessMetricBindingError, match="derive cohort binding"):
        value_of(forged, BusinessMetricName.DATA_AI_RATE)


def test_compute_refuses_a_context_whose_manifest_was_edited() -> None:
    records = DEFAULT_RECORDS
    manifest = manifest_payload(records)
    context = computation(records)
    edited = dict(manifest)
    edited["record_count"] = 99
    with pytest.raises(BusinessMetricsError):
        value_of(replace(context, manifest=edited), BusinessMetricName.DATA_AI_RATE)


def test_compute_refuses_something_that_is_not_a_computation_context() -> None:
    with pytest.raises(BusinessMetricContractError, match="computation context"):
        compute_business_metric(
            object(), key_of(BusinessMetricName.DATA_AI_RATE)
        )


def test_compute_refuses_something_that_is_not_a_key() -> None:
    with pytest.raises(BusinessMetricContractError, match="not a business metric key"):
        compute_business_metric(computation(), "DATA_AI_RATE")


def test_verifying_a_context_returns_the_rebuilt_cohort() -> None:
    records = DEFAULT_RECORDS
    context = computation(records)
    rebuilt = verify_business_metric_computation_context(context)
    assert rebuilt.cohort_size == len(records)
    assert rebuilt.binding_fingerprint == (
        context.run_context.frozen_cohort_binding.binding_fingerprint
    )


def test_a_computation_context_cannot_be_built_over_another_cohort() -> None:
    other = (record(7), record(8))
    with pytest.raises(BusinessMetricBindingError):
        build_business_metric_computation_context(
            other, manifest_payload(other), run_context(cohort(DEFAULT_RECORDS))
        )


# ====================================================================
# presence, action URLs and provenance — D18
# ====================================================================


def test_presence_rates_count_stated_fields_and_never_judge_them() -> None:
    """`is not None`, with no `.strip()` and no parsing.

    A description of one space is a description the posting carried; a coverage
    rate that disqualified it would be a content-quality metric under a coverage
    metric's name.
    """
    records = (
        record(1, description=" ", location="", deadline="not-a-date"),
        record(2, description=None, location=None, deadline=None),
    )
    context = only(records)
    for metric in (
        BusinessMetricName.DESCRIPTION_PRESENCE_RATE,
        BusinessMetricName.LOCATION_TEXT_PRESENCE_RATE,
        BusinessMetricName.DEADLINE_DATE_COVERAGE,
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.COMPUTED
        assert (result.support.numerator, result.support.denominator) == (1, 2)
        assert result.value == 0.5
        assert result.support.universe_size == 2


def test_presence_rates_are_zero_when_nothing_is_stated() -> None:
    context = only((record(1), record(2)))
    for metric in (
        BusinessMetricName.DESCRIPTION_PRESENCE_RATE,
        BusinessMetricName.LOCATION_TEXT_PRESENCE_RATE,
        BusinessMetricName.DEADLINE_DATE_COVERAGE,
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.COMPUTED
        assert result.value == 0.0
        assert result.support.numerator == 0


def test_action_url_presence_uses_the_frozen_precedence() -> None:
    """The protocol's own helper decides, and a `mailto:` is still a URL."""
    records = (
        record(1, application_url="https://a.example/1", source_url="https://b/2"),
        record(2, source_url="mailto:jobs@example.com"),
        record(3, canonical_url="/careers/3"),
        record(4, application_url="   "),
    )
    context = only(records)
    present = value_of(context, BusinessMetricName.ACTION_URL_PRESENCE_RATE)
    missing = value_of(context, BusinessMetricName.MISSING_ACTION_URL_RATE)
    assert present.support.numerator == 3
    assert missing.support.numerator == 1
    assert present.value == 0.75
    assert missing.value == 0.25


def test_action_url_presence_and_absence_are_exact_complements() -> None:
    for records in (
        (record(1, application_url="https://a.example/1"),),
        (record(1), record(2)),
        DEFAULT_RECORDS,
    ):
        context = only(records)
        present = value_of(context, BusinessMetricName.ACTION_URL_PRESENCE_RATE)
        missing = value_of(context, BusinessMetricName.MISSING_ACTION_URL_RATE)
        assert (
            present.support.numerator + missing.support.numerator == len(records)
        )
        assert present.value + missing.value == 1.0


def test_source_provenance_and_its_absence_are_exact_complements() -> None:
    records = (
        record(1, sources=(source("alpha"),)),
        record(2, sources=()),
        record(3, sources=(source("alpha"), source("beta"))),
    )
    context = only(records)
    covered = value_of(context, BusinessMetricName.SOURCE_PROVENANCE_COVERAGE_RATE)
    missing = value_of(context, BusinessMetricName.MISSING_SOURCE_PROVENANCE_RATE)
    multi = value_of(context, BusinessMetricName.MULTI_SOURCE_RECORD_RATE)
    assert covered.support.numerator == 2
    assert missing.support.numerator == 1
    assert multi.support.numerator == 1
    assert covered.support.numerator + missing.support.numerator == 3
    # multi_source <= provenance <= cohort
    assert multi.support.numerator <= covered.support.numerator <= 3


def test_a_source_repeated_in_one_record_is_one_source() -> None:
    """Distinct ids: a posting listed twice under one source was seen once there."""
    records = (record(1, sources=(source("alpha"), source("alpha"))),)
    context = only(records)
    multi = value_of(context, BusinessMetricName.MULTI_SOURCE_RECORD_RATE)
    assert multi.support.numerator == 0
    contribution = value_of(
        context,
        BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
        dimension_kind=SOURCE_ID,
        dimension_value="alpha",
    )
    assert contribution.support.numerator == 1
    assert contribution.value == 1.0


def test_observed_source_contributions_may_sum_above_one() -> None:
    records = (
        record(1, sources=(source("alpha"), source("beta"))),
        record(2, sources=(source("alpha"),)),
    )
    context = only(records)
    total = sum(
        value_of(
            context,
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=SOURCE_ID,
            dimension_value=source_id,
        ).value
        for source_id in ("alpha", "beta")
    )
    assert total == 1.5


def test_an_unobserved_source_dimension_is_refused_and_never_zero() -> None:
    context = only((record(1, sources=(source("alpha"),)),))
    with pytest.raises(BusinessMetricBindingError, match="no record in this cohort"):
        value_of(
            context,
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=SOURCE_ID,
            dimension_value="beta",
        )


def test_a_declared_but_unobserved_source_gets_no_contribution() -> None:
    """Declared is not observed. The source map cannot conjure a numerator."""
    records = (record(1, sources=(source("alpha_board"),)),)
    context = only(
        records,
        declared_source_universe=declared_universe(
            (
                FakeSourceEntry("alpha_board"),
                FakeSourceEntry("never_collected", integration_status="CANDIDATE"),
            )
        ),
    )
    with pytest.raises(BusinessMetricBindingError):
        value_of(
            context,
            BusinessMetricName.OBSERVED_SOURCE_CONTRIBUTION,
            dimension_kind=SOURCE_ID,
            dimension_value="never_collected",
        )


def test_a_malformed_source_id_is_a_hard_error() -> None:
    for broken in (None, "", "   ", " padded ", 7):
        records = (record(1, sources=({**source(), "source_id": broken},)),)
        with pytest.raises(BusinessMetricsError):
            value_of(
                only(records), BusinessMetricName.SOURCE_PROVENANCE_COVERAGE_RATE
            )


def test_a_cohort_with_no_sources_has_full_missing_provenance() -> None:
    context = only((record(1), record(2)))
    assert value_of(
        context, BusinessMetricName.MISSING_SOURCE_PROVENANCE_RATE
    ).value == 1.0
    assert value_of(
        context, BusinessMetricName.SOURCE_PROVENANCE_COVERAGE_RATE
    ).value == 0.0


# ====================================================================
# Data & AI — D19
# ====================================================================


def _classified(*values):
    return tuple(
        record(index, qualification=qualification(value=value))
        if value is not None
        else record(index)
        for index, value in enumerate(values, start=1)
    )


def test_the_three_data_ai_rates_read_one_breakdown() -> None:
    records = _classified(
        "CORE_TARGET", "ADJACENT_TARGET", "OUT_OF_SCOPE", "UNCERTAIN", None
    )
    context = only(records)
    data_ai = value_of(context, BusinessMetricName.DATA_AI_RATE)
    core = value_of(context, BusinessMetricName.CORE_DATA_AI_RATE)
    coverage = value_of(context, BusinessMetricName.CLASSIFICATION_COVERAGE_RATE)

    assert data_ai.support.numerator == 2  # core + adjacent
    assert core.support.numerator == 1
    assert coverage.support.numerator == 4  # everything but the unclassified one
    for result in (data_ai, core, coverage):
        assert result.support.denominator == 5
        assert result.support.universe_size == 5
        assert result.value == result.support.numerator / 5
    breakdown = data_ai.support.data_ai_breakdown
    assert breakdown == core.support.data_ai_breakdown
    assert breakdown == coverage.support.data_ai_breakdown
    assert breakdown.total == 5
    assert (
        breakdown.core_target_count,
        breakdown.adjacent_target_count,
        breakdown.out_of_scope_count,
        breakdown.uncertain_count,
        breakdown.unclassified_count,
    ) == (1, 1, 1, 1, 1)


def test_uncertain_is_classified_and_is_not_data_ai() -> None:
    context = only(_classified("UNCERTAIN"))
    assert value_of(context, BusinessMetricName.DATA_AI_RATE).value == 0.0
    assert value_of(
        context, BusinessMetricName.CLASSIFICATION_COVERAGE_RATE
    ).value == 1.0


def test_an_all_unclassified_cohort_is_computed_at_zero() -> None:
    """Never `N_A`: "the classifier has not run" is a measurement of the data."""
    context = only((record(1), record(2)))
    for metric in (
        BusinessMetricName.DATA_AI_RATE,
        BusinessMetricName.CORE_DATA_AI_RATE,
        BusinessMetricName.CLASSIFICATION_COVERAGE_RATE,
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.COMPUTED
        assert result.value == 0.0
        assert result.reason is None
    breakdown = value_of(
        context, BusinessMetricName.DATA_AI_RATE
    ).support.data_ai_breakdown
    assert breakdown.unclassified_count == 2


def test_an_unknown_qualification_value_is_a_hard_error() -> None:
    """Not folded into `unclassified`: an unread posting is not an unknown word."""
    records = (record(1, qualification=qualification(value="PROBABLY_FINE")),)
    with pytest.raises(BusinessMetricBindingError, match="does not define"):
        value_of(only(records), BusinessMetricName.DATA_AI_RATE)


def test_the_data_ai_projection_matches_the_production_vocabulary() -> None:
    """The shared vocabulary is the production classifier's, exactly.

    The *vocabulary* moved to `evaluation.frozen_facts` in Phase 10.5 so that
    both phases read one definition of "explicitly out of scope"; the invariant
    is unchanged and is asserted where the vocabulary now lives. A value added
    upstream is still a failure here rather than a silent miscount.
    """
    from evaluation.frozen_facts import FROZEN_DATA_AI_STATE_NAMES
    from services.collector.qualification.taxonomy import Qualification

    assert FROZEN_DATA_AI_STATE_NAMES == {str(item) for item in Qualification}


def test_the_data_ai_counts_cover_every_shared_state() -> None:
    """This contract's arithmetic covers the whole shared enum, and only it.

    `_DATA_AI_COUNTS` is what is left in Phase 10.4 after the reading moved out:
    which of the five counts each state feeds. A state added to
    `FrozenDataAiState` with no count here would raise a `KeyError` mid-walk, so
    the mapping is held against the enum directly.
    """
    from evaluation.business_metrics.formulas import _DATA_AI_COUNTS
    from evaluation.frozen_facts import FrozenDataAiState

    assert set(_DATA_AI_COUNTS) == set(FrozenDataAiState)
    assert len(set(_DATA_AI_COUNTS.values())) == len(FrozenDataAiState)


def test_no_data_ai_fallback_reads_any_other_field() -> None:
    """A posting screaming "machine learning" is still unclassified."""
    records = (
        record(
            1,
            canonical_title="Machine Learning Engineer",
            description="deep learning, data science, AI",
            opportunity_type="PFE",
        ),
    )
    result = value_of(only(records), BusinessMetricName.DATA_AI_RATE)
    assert result.value == 0.0
    assert result.support.data_ai_breakdown.unclassified_count == 1


# ====================================================================
# opportunity type / PFE — D20
# ====================================================================


def _typed(*values):
    return tuple(
        record(index, qualification=qualification(opportunity_type=value))
        if value is not None
        else record(index)
        for index, value in enumerate(values, start=1)
    )


def test_pfe_or_internship_excludes_apprenticeship() -> None:
    records = _typed(
        "PFE", "INTERNSHIP", "APPRENTICESHIP", "GRADUATE", "JOB", "UNKNOWN", None
    )
    context = only(records)
    rate = value_of(context, BusinessMetricName.PFE_OR_INTERNSHIP_RATE)
    assert rate.support.numerator == 2
    assert rate.support.denominator == 7
    breakdown = rate.support.opportunity_type_breakdown
    assert breakdown.apprenticeship_count == 1
    assert breakdown.pfe_or_internship_count == 2
    assert breakdown.total == 7


def test_unknown_type_counts_as_typed_and_unclassified_does_not() -> None:
    records = _typed("UNKNOWN", None)
    coverage = value_of(
        only(records),
        BusinessMetricName.OPPORTUNITY_TYPE_CLASSIFICATION_COVERAGE_RATE,
    )
    assert coverage.support.numerator == 1
    assert coverage.value == 0.5


def test_the_type_metrics_read_the_qualification_not_the_top_level_column() -> None:
    """`record.opportunity_type` is the collector's parse; the metric is the
    classifier's decision."""
    records = (
        record(1, opportunity_type="PFE"),  # no qualification at all
        record(
            2,
            opportunity_type="JOB",
            qualification=qualification(opportunity_type="PFE"),
        ),
    )
    rate = value_of(only(records), BusinessMetricName.PFE_OR_INTERNSHIP_RATE)
    assert rate.support.numerator == 1
    breakdown = rate.support.opportunity_type_breakdown
    assert breakdown.unclassified_type_count == 1
    assert breakdown.pfe_count == 1
    assert breakdown.job_count == 0


def test_an_unknown_opportunity_type_value_is_a_hard_error() -> None:
    records = (record(1, qualification=qualification(opportunity_type="SUMMER_GIG")),)
    with pytest.raises(BusinessMetricBindingError, match="does not define"):
        value_of(only(records), BusinessMetricName.PFE_OR_INTERNSHIP_RATE)


def test_the_type_projection_matches_the_production_vocabulary() -> None:
    from evaluation.business_metrics.formulas import _OPPORTUNITY_TYPE_PROJECTION
    from services.collector.qualification.taxonomy import OpportunityType

    assert set(_OPPORTUNITY_TYPE_PROJECTION) == {
        str(item) for item in OpportunityType
    }


# ====================================================================
# listing quality — D24
# ====================================================================


def test_normal_listing_rate_reads_the_frozen_quality() -> None:
    records = (
        record(1, qualification=qualification(listing_quality="NORMAL_LISTING")),
        record(
            2, qualification=qualification(listing_quality="POSSIBLE_NON_JOB_PAGE")
        ),
        record(
            3, qualification=qualification(listing_quality="INSUFFICIENT_CONTENT")
        ),
        record(4),
    )
    result = value_of(only(records), BusinessMetricName.NORMAL_LISTING_RATE)
    assert result.support.numerator == 1
    assert result.value == 0.25
    breakdown = result.support.listing_quality_breakdown
    assert (
        breakdown.normal_listing_count,
        breakdown.possible_non_job_page_count,
        breakdown.insufficient_content_count,
        breakdown.unclassified_count,
    ) == (1, 1, 1, 1)
    assert breakdown.total == 4


def test_an_unknown_listing_quality_is_a_hard_error() -> None:
    records = (record(1, qualification=qualification(listing_quality="FINE")),)
    with pytest.raises(BusinessMetricBindingError, match="does not define"):
        value_of(only(records), BusinessMetricName.NORMAL_LISTING_RATE)


def test_listing_quality_is_never_re_derived_from_the_page() -> None:
    records = (
        record(
            1,
            description="Apply here",
            source_url="https://example.com/about-us",
            qualification=qualification(listing_quality="NORMAL_LISTING"),
        ),
    )
    assert value_of(only(records), BusinessMetricName.NORMAL_LISTING_RATE).value == 1.0


def test_the_quality_projection_matches_the_production_vocabulary() -> None:
    from evaluation.business_metrics.formulas import _LISTING_QUALITY_PROJECTION
    from services.collector.qualification.taxonomy import ListingQuality

    assert set(_LISTING_QUALITY_PROJECTION) == {str(item) for item in ListingQuality}


# ====================================================================
# geography — D23
# ====================================================================


def test_unknown_location_counts_no_segments_and_unknown_segments() -> None:
    records = (
        record(1, geography_segments=(segment("RESOLVED"),)),
        record(2, geography_segments=()),
        record(3, geography_segments=(segment("UNKNOWN"),)),
        record(4, geography_segments=(segment("AMBIGUOUS"),)),
    )
    result = value_of(only(records), BusinessMetricName.UNKNOWN_LOCATION_RATE)
    assert result.support.numerator == 2
    assert result.value == 0.5
    diagnostics = result.support.unknown_location_diagnostics
    assert diagnostics.no_geography_segments_count == 1
    assert diagnostics.unknown_segment_record_count == 1
    assert diagnostics.ambiguous_location_count == 1
    assert diagnostics.unknown_location_count == 2


def test_ambiguous_alone_is_not_an_unknown_location() -> None:
    """Several possible places is a different failure from none at all."""
    records = (record(1, geography_segments=(segment("AMBIGUOUS"),)),)
    result = value_of(only(records), BusinessMetricName.UNKNOWN_LOCATION_RATE)
    assert result.value == 0.0
    assert result.support.unknown_location_diagnostics.ambiguous_location_count == 1


def test_the_diagnostics_may_overlap_where_the_contract_allows() -> None:
    records = (
        record(
            1,
            geography_segments=(
                segment("AMBIGUOUS", position=1),
                segment("UNKNOWN", position=2),
            ),
        ),
    )
    result = value_of(only(records), BusinessMetricName.UNKNOWN_LOCATION_RATE)
    diagnostics = result.support.unknown_location_diagnostics
    assert diagnostics.ambiguous_location_count == 1
    assert diagnostics.unknown_segment_record_count == 1
    assert result.support.numerator == 1


def test_target_match_needs_one_resolved_segment_to_the_target() -> None:
    records = (
        record(
            1,
            geography_segments=(
                segment("RESOLVED", country_code=OTHER_COUNTRY, position=1),
                segment("RESOLVED", country_code=TARGET_COUNTRY, position=2),
            ),
        ),
    )
    result = value_of(only(records), BusinessMetricName.TARGET_COUNTRY_MATCH_RATE)
    assert result.value == 1.0
    assert result.support.target_verdict_breakdown.match_count == 1


def test_all_resolved_elsewhere_is_out_of_target() -> None:
    records = (
        record(1, geography_segments=(segment("RESOLVED", country_code="FR"),)),
        record(2, geography_segments=(segment("RESOLVED", country_code="ES"),)),
    )
    result = value_of(only(records), BusinessMetricName.TARGET_COUNTRY_MATCH_RATE)
    breakdown = result.support.target_verdict_breakdown
    assert breakdown.out_of_target_count == 2
    assert breakdown.match_count == 0
    assert breakdown.unknown_target_verdict_count == 0
    assert result.value == 0.0


def test_an_ambiguous_segment_prevents_out_of_target() -> None:
    """It might be the target country, so a negative verdict would be a guess."""
    records = (
        record(
            1,
            geography_segments=(
                segment("RESOLVED", country_code="FR", position=1),
                segment("AMBIGUOUS", position=2),
            ),
        ),
    )
    breakdown = value_of(
        only(records), BusinessMetricName.TARGET_COUNTRY_MATCH_RATE
    ).support.target_verdict_breakdown
    assert breakdown.out_of_target_count == 0
    assert breakdown.unknown_target_verdict_count == 1


def test_a_record_with_no_segments_is_an_unknown_verdict() -> None:
    records = (record(1, geography_segments=()),)
    breakdown = value_of(
        only(records), BusinessMetricName.TARGET_COUNTRY_MATCH_RATE
    ).support.target_verdict_breakdown
    assert breakdown.unknown_target_verdict_count == 1
    assert breakdown.out_of_target_count == 0


def test_unknown_verdicts_stay_in_the_denominator() -> None:
    records = (
        record(1, geography_segments=(segment("RESOLVED"),)),
        record(2, geography_segments=()),
        record(3, geography_segments=(segment("UNKNOWN"),)),
    )
    result = value_of(only(records), BusinessMetricName.TARGET_COUNTRY_MATCH_RATE)
    assert result.support.denominator == 3
    assert result.support.universe_size == 3
    assert result.value == pytest.approx(1 / 3)
    assert result.support.target_verdict_breakdown.total == 3


def test_a_missing_target_binding_is_its_own_refusal() -> None:
    context = computation(full=False)
    result = value_of(context, BusinessMetricName.TARGET_COUNTRY_MATCH_RATE)
    assert result.status is BusinessMetricStatus.N_A
    assert result.reason is (
        BusinessMetricUnavailableReason.TARGET_SCOPE_BINDING_MISSING
    )
    assert result.support.universe_size == len(DEFAULT_RECORDS)
    assert result.support.numerator is None


def test_an_unknown_target_country_is_a_different_refusal() -> None:
    binding = cohort()
    context = computation(
        DEFAULT_RECORDS,
        profile_target_binding=target_binding(
            country_code=None,
            profile_id=binding.profile_id,
            profile_fingerprint=binding.profile_fingerprint,
        ),
    )
    result = value_of(context, BusinessMetricName.TARGET_COUNTRY_MATCH_RATE)
    assert result.reason is BusinessMetricUnavailableReason.TARGET_COUNTRY_UNKNOWN
    assert result.support.universe_size == len(DEFAULT_RECORDS)


def test_an_incoherent_geography_segment_is_a_hard_error() -> None:
    incoherent = (
        {**segment("RESOLVED"), "country_code": None},
        {**segment("AMBIGUOUS"), "country_code": "MA"},
        {**segment("UNKNOWN"), "country_code": "MA"},
        {**segment("RESOLVED"), "status": "PROBABLY"},
    )
    for broken in incoherent:
        records = (record(1, geography_segments=(broken,)),)
        with pytest.raises(BusinessMetricBindingError):
            value_of(only(records), BusinessMetricName.UNKNOWN_LOCATION_RATE)


def test_geography_never_falls_back_to_the_location_text() -> None:
    records = (
        record(1, location="Casablanca, Morocco", country="MA", geography_segments=()),
    )
    context = only(records)
    assert value_of(context, BusinessMetricName.UNKNOWN_LOCATION_RATE).value == 1.0
    assert value_of(context, BusinessMetricName.TARGET_COUNTRY_MATCH_RATE).value == 0.0


def test_no_country_is_hardcoded_in_the_target_formula() -> None:
    """The same cohort measured against two different bound targets."""
    records = (
        record(1, geography_segments=(segment("RESOLVED", country_code="FR"),)),
    )
    binding = build_frozen_cohort_binding(records, manifest_payload(records))
    for country, expected in ((TARGET_COUNTRY, 0.0), ("FR", 1.0)):
        context = computation(
            records,
            profile_target_binding=target_binding(
                country_code=country,
                profile_id=binding.profile_id,
                profile_fingerprint=binding.profile_fingerprint,
            ),
        )
        assert (
            value_of(context, BusinessMetricName.TARGET_COUNTRY_MATCH_RATE).value
            == expected
        )


# ====================================================================
# deduplication — D21
# ====================================================================


def test_applied_duplicate_rate_is_d_over_s_plus_d() -> None:
    records = (
        record(1, absorbed=(11, 12)),
        record(2, absorbed=(13,)),
        record(3),
    )
    result = value_of(only(records), BusinessMetricName.APPLIED_DUPLICATE_RATE)
    assert result.support.numerator == 3
    assert result.support.denominator == 6
    assert result.support.universe_size == 6
    assert result.value == 0.5
    assert result.universe_kind is (
        BusinessMetricUniverse.PRE_DEDUP_REPRESENTED_UNIVERSE
    )


def test_duplicate_cluster_rate_is_c_over_s() -> None:
    records = (
        record(1, absorbed=(11, 12)),
        record(2, absorbed=(13,)),
        record(3),
    )
    result = value_of(only(records), BusinessMetricName.DUPLICATE_CLUSTER_RATE)
    assert result.support.numerator == 2
    assert result.support.denominator == 3
    assert result.support.universe_size == 3
    assert result.value == pytest.approx(2 / 3)
    assert result.universe_kind is BusinessMetricUniverse.FROZEN_COHORT


def test_a_cohort_with_no_duplicates_computes_both_at_zero() -> None:
    records = (record(1), record(2))
    context = only(records)
    applied = value_of(context, BusinessMetricName.APPLIED_DUPLICATE_RATE)
    cluster = value_of(context, BusinessMetricName.DUPLICATE_CLUSTER_RATE)
    assert applied.value == 0.0
    assert applied.support.denominator == 2
    assert cluster.value == 0.0


def test_missing_dedup_evidence_refuses_both_with_different_universes() -> None:
    context = computation(full=False)
    applied = value_of(context, BusinessMetricName.APPLIED_DUPLICATE_RATE)
    cluster = value_of(context, BusinessMetricName.DUPLICATE_CLUSTER_RATE)
    for result in (applied, cluster):
        assert result.status is BusinessMetricStatus.N_A
        assert result.reason is (
            BusinessMetricUnavailableReason.DEDUP_EVIDENCE_MISSING
        )
    # S + D is not establishable without the artefact that holds D...
    assert applied.support.universe_size is None
    # ...but the cohort's own size is.
    assert cluster.support.universe_size == len(DEFAULT_RECORDS)


def test_dedup_never_falls_back_to_the_raw_records() -> None:
    """The records carry absorbed ids; with no evidence bound it is still N_A."""
    records = (record(1, absorbed=(11, 12)), record(2))
    context = computation(records, full=False)
    result = value_of(context, BusinessMetricName.APPLIED_DUPLICATE_RATE)
    assert result.status is BusinessMetricStatus.N_A
    assert result.value is None


def test_contradictory_dedup_evidence_is_a_hard_error() -> None:
    from evaluation.business_metrics.formulas import _dedup_facts, _verified

    context = computation()
    verified = _verified(context)
    evidence = verified.run_context.dedup_evidence
    broken = replace(evidence, records_with_absorbed_duplicates=99)
    forged = replace(
        verified.run_context,
        dedup_evidence=broken,
    )
    with pytest.raises(BusinessMetricBindingError):
        _dedup_facts(replace(verified, run_context=forged))


# ====================================================================
# the URL audit — D22
# ====================================================================


def _audit_context(outcomes, **kwargs):
    """A cohort of one record per outcome, with an audit stating each."""
    records = tuple(
        record(index, application_url=f"https://example.test/{index}")
        for index in range(1, len(outcomes) + 1)
    )
    binding = build_frozen_cohort_binding(records, manifest_payload(records))
    observations = tuple(
        observation(url, **spec)
        for url, spec in zip(binding.expected_action_urls, outcomes)
    )
    return computation(
        records, url_audit_binding=url_audit(binding, observations), **kwargs
    )


def test_only_404_and_410_are_broken() -> None:
    context = _audit_context(
        (
            {"outcome": UrlAuditOutcome.BROKEN, "status_code": 404},
            {"outcome": UrlAuditOutcome.BROKEN, "status_code": 410},
            {"outcome": UrlAuditOutcome.VALID, "status_code": 200},
        )
    )
    result = value_of(context, BusinessMetricName.BROKEN_URL_RATE)
    assert result.support.numerator == 2
    assert result.support.denominator == 3
    assert result.value == pytest.approx(2 / 3)


def test_refusals_and_failures_stay_inconclusive() -> None:
    """403, 429, 500, 3xx and a timeout are not evidence that a posting is gone."""
    context = _audit_context(
        (
            {"outcome": UrlAuditOutcome.VALID, "status_code": 200},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 403},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 429},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 500},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 302},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE},  # a timeout
        )
    )
    broken = value_of(context, BusinessMetricName.BROKEN_URL_RATE)
    assert broken.support.numerator == 0
    assert broken.support.denominator == 1  # only the 200 concluded anything
    assert broken.value == 0.0


def test_attempted_coverage_and_conclusive_coverage_are_different_numbers() -> None:
    context = _audit_context(
        (
            {"outcome": UrlAuditOutcome.VALID, "status_code": 200},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 403},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "attempted": False},
        )
    )
    attempted = value_of(context, BusinessMetricName.URL_AUDIT_COVERAGE_RATE)
    conclusive = value_of(context, BusinessMetricName.URL_CONCLUSIVE_COVERAGE_RATE)
    assert attempted.support.numerator == 2
    assert conclusive.support.numerator == 1
    assert attempted.support.denominator == conclusive.support.denominator == 3
    assert attempted.value == pytest.approx(2 / 3)
    assert conclusive.value == pytest.approx(1 / 3)


def test_broken_rate_is_na_when_nothing_concluded_but_coverage_is_computed() -> None:
    context = _audit_context(
        (
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "attempted": False},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 403},
        )
    )
    broken = value_of(context, BusinessMetricName.BROKEN_URL_RATE)
    assert broken.status is BusinessMetricStatus.N_A
    assert broken.reason is BusinessMetricUnavailableReason.NO_CONCLUSIVE_URL_AUDIT
    assert broken.support.universe_size == 2
    for metric, expected in (
        (BusinessMetricName.URL_AUDIT_COVERAGE_RATE, 0.5),
        (BusinessMetricName.URL_CONCLUSIVE_COVERAGE_RATE, 0.0),
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.COMPUTED
        assert result.value == expected


def test_broken_rate_states_the_audited_universe_not_its_denominator() -> None:
    context = _audit_context(
        (
            {"outcome": UrlAuditOutcome.BROKEN, "status_code": 404},
            {"outcome": UrlAuditOutcome.INCONCLUSIVE, "status_code": 403},
        )
    )
    result = value_of(context, BusinessMetricName.BROKEN_URL_RATE)
    assert result.support.denominator == 1
    assert result.support.universe_size == 2
    assert result.support.denominator != result.support.universe_size


def test_a_missing_audit_refuses_all_three_with_no_universe() -> None:
    context = computation(full=False)
    for metric in (
        BusinessMetricName.BROKEN_URL_RATE,
        BusinessMetricName.URL_AUDIT_COVERAGE_RATE,
        BusinessMetricName.URL_CONCLUSIVE_COVERAGE_RATE,
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.N_A
        assert result.reason is (
            BusinessMetricUnavailableReason.URL_AUDIT_EVIDENCE_MISSING
        )
        assert result.support.universe_size is None


def test_the_formulas_module_makes_no_request() -> None:
    import evaluation.business_metrics.formulas as formulas

    source_text = inspect.getsource(formulas)
    for forbidden in ("httpx", "requests", "urlopen", "socket", "http.client"):
        assert f"import {forbidden}" not in source_text


# ====================================================================
# freshness — D25
# ====================================================================


def _dated(*offsets):
    """One record per offset in days before the as-of date; `None` means missing."""
    from datetime import date, timedelta

    as_of = date.fromisoformat(AS_OF_DATE)
    records = []
    for index, offset in enumerate(offsets, start=1):
        if offset is None:
            published = None
        elif isinstance(offset, str):
            published = offset
        else:
            published = (as_of - timedelta(days=offset)).isoformat()
        records.append(record(index, published_at=published))
    return tuple(records)


@pytest.mark.parametrize(
    "age, bucket",
    [
        (0, FreshnessBucket.AGE_0_2),
        (2, FreshnessBucket.AGE_0_2),
        (3, FreshnessBucket.AGE_3_7),
        (7, FreshnessBucket.AGE_3_7),
        (8, FreshnessBucket.AGE_8_14),
        (14, FreshnessBucket.AGE_8_14),
        (15, FreshnessBucket.AGE_15_30),
        (30, FreshnessBucket.AGE_15_30),
        (31, FreshnessBucket.AGE_31_60),
        (60, FreshnessBucket.AGE_31_60),
        (61, FreshnessBucket.AGE_GT_60),
        (400, FreshnessBucket.AGE_GT_60),
    ],
)
def test_every_freshness_bucket_boundary(age, bucket) -> None:
    context = only(_dated(age))
    distribution = value_of(
        context, BusinessMetricName.PUBLICATION_DATE_COVERAGE
    ).support.freshness_distribution
    assert distribution.count(bucket) == 1
    assert sum(entry.count for entry in distribution.entries) == 1


@pytest.mark.parametrize(
    "age, score",
    [(0, 1.00), (2, 1.00), (3, 0.85), (7, 0.85), (8, 0.65), (14, 0.65),
     (15, 0.40), (30, 0.40), (31, 0.20), (60, 0.20), (61, 0.10)],
)
def test_the_freshness_score_is_the_production_policy(age, score) -> None:
    result = value_of(only(_dated(age)), BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE)
    assert result.value == pytest.approx(score)


def test_the_freshness_policy_version_matches_production() -> None:
    from services.priority.models import PRIORITY_FRESHNESS_VERSION

    assert EXPECTED_FRESHNESS_POLICY_VERSION == PRIORITY_FRESHNESS_VERSION
    assert EXPECTED_FRESHNESS_POLICY_VERSION == "priority-freshness-v1"


def test_a_missing_publication_date_is_missing_not_invalid() -> None:
    distribution = value_of(
        only(_dated(None)), BusinessMetricName.PUBLICATION_DATE_COVERAGE
    ).support.freshness_distribution
    assert distribution.count(FreshnessBucket.MISSING) == 1
    assert distribution.count(FreshnessBucket.INVALID) == 0


@pytest.mark.parametrize(
    "malformed", ["not-a-date", "", "2026-13-01", "  ", 20260101, 7.5]
)
def test_a_malformed_publication_date_is_invalid(malformed) -> None:
    """Unreadable, including a value that is not a string at all.

    `INVALID` rather than `MISSING`: the record *states* a publication date and
    this build cannot use it, which is a different fact from stating none.
    """
    records = (record(1, published_at=malformed),)
    distribution = value_of(
        only(records), BusinessMetricName.PUBLICATION_DATE_COVERAGE
    ).support.freshness_distribution
    assert distribution.count(FreshnessBucket.INVALID) == 1
    assert distribution.count(FreshnessBucket.MISSING) == 0


def test_a_future_publication_date_is_invalid_not_the_freshest_bucket() -> None:
    distribution = value_of(
        only(_dated(-5)), BusinessMetricName.PUBLICATION_DATE_COVERAGE
    ).support.freshness_distribution
    assert distribution.count(FreshnessBucket.INVALID) == 1
    assert distribution.count(FreshnessBucket.AGE_0_2) == 0


def test_the_distribution_sums_to_the_cohort() -> None:
    context = only(_dated(0, 5, 20, 90, None, "rubbish", -1))
    result = value_of(context, BusinessMetricName.PUBLICATION_DATE_COVERAGE)
    distribution = result.support.freshness_distribution
    assert sum(entry.count for entry in distribution.entries) == 7
    assert distribution.universe_size == 7
    assert distribution.known_date_count == 4
    assert result.support.numerator == 4
    assert result.support.denominator == 7


def test_coverage_stays_computed_when_no_date_is_valid_and_the_mean_is_not() -> None:
    context = only(_dated(None, "rubbish"))
    coverage = value_of(context, BusinessMetricName.PUBLICATION_DATE_COVERAGE)
    mean = value_of(context, BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE)
    assert coverage.status is BusinessMetricStatus.COMPUTED
    assert coverage.value == 0.0
    assert coverage.support.freshness_distribution is not None
    assert mean.status is BusinessMetricStatus.N_A
    assert mean.reason is BusinessMetricUnavailableReason.NO_VALID_PUBLICATION_DATES
    assert mean.support.freshness_availability_witness.known_date_count == 0
    assert mean.support.universe_size == 2


def test_the_mean_divides_by_known_dates_and_states_the_cohort() -> None:
    context = only(_dated(0, None, None, None))
    mean = value_of(context, BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE)
    assert mean.support.numerator == pytest.approx(1.0)
    assert mean.support.denominator == 1
    assert mean.support.universe_size == 4
    assert mean.support.denominator != mean.support.universe_size
    assert mean.value == 1.0


def test_the_mean_is_not_rounded() -> None:
    """Three scores of 1.00, 0.85 and 0.65 average to a number with a tail."""
    context = only(_dated(0, 5, 10))
    mean = value_of(context, BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE)
    expected = (1.00 + 0.85 + 0.65) / 3
    assert mean.value == expected
    assert mean.value != round(mean.value, 2)


def test_a_missing_freshness_binding_refuses_both_over_the_cohort() -> None:
    context = computation(full=False)
    for metric in (
        BusinessMetricName.PUBLICATION_DATE_COVERAGE,
        BusinessMetricName.MEAN_KNOWN_FRESHNESS_SCORE,
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.N_A
        assert result.reason is (
            BusinessMetricUnavailableReason.FRESHNESS_BINDING_MISSING
        )
        assert result.support.universe_size == len(DEFAULT_RECORDS)
        assert result.support.numerator is None
        assert result.support.denominator is None
        assert result.support.freshness_distribution is None


def test_freshness_reads_no_clock_and_no_fallback_field() -> None:
    """Only `published_at` and the snapshot's own as-of date.

    The record below is dated nowhere but in fields freshness must not read, and
    the as-of date is in the past, so a clock would change the answer.
    """
    records = (
        record(
            1,
            published_at=None,
            discovered_at="2026-02-28T09:00:00+00:00",
            first_seen_at="2026-02-28T09:00:00+00:00",
            last_seen_at="2026-02-28T09:00:00+00:00",
            deadline="2026-02-28",
        ),
    )
    coverage = value_of(only(records), BusinessMetricName.PUBLICATION_DATE_COVERAGE)
    assert coverage.value == 0.0
    assert coverage.support.freshness_distribution.count(FreshnessBucket.MISSING) == 1


def test_the_age_buckets_come_from_the_frozen_bounds() -> None:
    from evaluation.business_metrics.formulas import _age_bucket

    for bucket, (low, high) in FRESHNESS_BUCKET_DAY_BOUNDS.items():
        assert _age_bucket(low) is bucket
        if high is not None:
            assert _age_bucket(high) is bucket


# ====================================================================
# the declared source universe — D27
# ====================================================================


def test_active_declared_source_rate_counts_the_active_entries() -> None:
    universe = declared_universe(
        (
            FakeSourceEntry("a", integration_status="ACTIVE"),
            FakeSourceEntry("b", integration_status="ACTIVE"),
            FakeSourceEntry("c", integration_status="CANDIDATE"),
            FakeSourceEntry("d", integration_status="NOT_SELECTED"),
        )
    )
    context = computation(declared_source_universe=universe)
    result = value_of(context, BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE)
    assert result.support.numerator == 2
    assert result.support.denominator == 4
    assert result.support.universe_size == 4
    assert result.value == 0.5
    assert result.universe_kind is BusinessMetricUniverse.DECLARED_SOURCE_UNIVERSE


def test_the_active_status_matches_the_source_map_validator() -> None:
    from evaluation.morocco_pfe.validator import INTEGRATION_STATUSES

    assert DECLARED_SOURCE_ACTIVE_STATUS in INTEGRATION_STATUSES
    assert DECLARED_SOURCE_ACTIVE_STATUS == "ACTIVE"


def test_a_missing_source_map_refuses_both_declared_metrics() -> None:
    context = computation(full=False)
    for metric in (
        BusinessMetricName.ACTIVE_DECLARED_SOURCE_RATE,
        BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL,
    ):
        result = value_of(context, metric)
        assert result.status is BusinessMetricStatus.N_A
        assert result.reason is (
            BusinessMetricUnavailableReason.DECLARED_SOURCE_UNIVERSE_MISSING
        )
        assert result.support.universe_size is None


def test_discovery_recall_refuses_a_benchmark_that_is_not_ready() -> None:
    context = computation()
    result = value_of(context, BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL)
    assert result.status is BusinessMetricStatus.N_A
    assert result.reason is (
        BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY
    )
    # The universe is determinable from the declared map even though the
    # benchmark is not usable.
    assert result.support.universe_size == 2


def test_discovery_recall_refuses_an_absent_benchmark_the_same_way() -> None:
    context = computation(benchmark_binding=None)
    result = value_of(context, BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL)
    assert result.reason is (
        BusinessMetricUnavailableReason.SOURCE_BENCHMARK_NOT_EVALUATION_READY
    )


def test_an_evaluation_ready_benchmark_needs_its_rows() -> None:
    rows = benchmark_rows(3)
    with pytest.raises(BusinessMetricBindingError, match="evaluation-ready"):
        computation(benchmark_binding=ready_benchmark(3))
    context = computation(
        benchmark_binding=ready_benchmark(3), benchmark_records=rows
    )
    assert context.benchmark_records == rows


def test_evaluation_ready_benchmark_rows_are_fingerprint_checked() -> None:
    rows = benchmark_rows(3)
    tampered = (*rows[:-1], {**rows[-1], "title": "something else"})
    with pytest.raises(BusinessMetricBindingError, match="digest to"):
        computation(
            benchmark_binding=ready_benchmark(3), benchmark_records=tampered
        )
    short = rows[:2]
    with pytest.raises(BusinessMetricBindingError, match="row"):
        computation(benchmark_binding=ready_benchmark(3), benchmark_records=short)


def test_rows_with_no_benchmark_bound_are_refused() -> None:
    with pytest.raises(BusinessMetricBindingError, match="nothing for them"):
        computation(benchmark_binding=None, benchmark_records=benchmark_rows(2))


def test_a_ready_benchmark_makes_discovery_recall_fail_closed() -> None:
    """Rows are necessary and **not sufficient**, so this raises rather than
    inventing a match or a new reason code."""
    context = computation(
        benchmark_binding=ready_benchmark(3), benchmark_records=benchmark_rows(3)
    )
    with pytest.raises(BusinessMetricContractError, match="no discovery evidence"):
        value_of(context, BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL)


def test_discovery_recall_invents_no_matching_rule() -> None:
    """A gold row whose source_url is in the cohort still produces no number."""
    url = "https://invented.example/offers/1"
    records = (record(1, application_url=url, sources=(source("invented"),)),)
    context = computation(
        records,
        benchmark_binding=ready_benchmark(3),
        benchmark_records=benchmark_rows(3),
    )
    with pytest.raises(BusinessMetricContractError) as error:
        value_of(context, BusinessMetricName.DECLARED_SOURCE_DISCOVERY_RECALL)
    message = str(error.value)
    for refused in ("source URL", "hostname", "benchmark id", "title"):
        assert refused in message


# ====================================================================
# skills — D28
# ====================================================================


def test_opportunity_skill_coverage_is_always_unavailable() -> None:
    for context in (computation(), computation(full=False)):
        result = value_of(context, BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE)
        assert result.status is BusinessMetricStatus.N_A
        assert result.reason is (
            BusinessMetricUnavailableReason.OPPORTUNITY_SKILLS_NOT_FROZEN
        )
        assert result.value is None
        assert result.support.numerator is None
        assert result.support.denominator is None
        assert result.universe_kind is BusinessMetricUniverse.FROZEN_COHORT
        assert result.support.universe_size == len(DEFAULT_RECORDS)


def test_skill_coverage_is_unavailable_even_with_a_rich_description() -> None:
    records = (
        record(1, description="Required skills: Python, SQL, Spark, dbt, Airflow"),
    )
    result = value_of(
        only(records), BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE
    )
    assert result.status is BusinessMetricStatus.N_A
    assert BusinessMetricName.OPPORTUNITY_SKILL_COVERAGE in ALWAYS_UNAVAILABLE_METRICS


# ====================================================================
# the universal arithmetic rule
# ====================================================================


def test_every_computed_value_is_its_own_quotient() -> None:
    context = computation()
    for metric in BusinessMetricName:
        if metric in package.DIMENSIONAL_BUSINESS_METRICS:
            continue
        result = value_of(context, metric)
        if result.status is not BusinessMetricStatus.COMPUTED:
            continue
        assert result.value == (
            result.support.numerator / result.support.denominator
        ), metric


def test_no_computed_result_has_a_zero_denominator() -> None:
    context = computation()
    for metric in BusinessMetricName:
        if metric in package.DIMENSIONAL_BUSINESS_METRICS:
            continue
        result = value_of(context, metric)
        if result.status is BusinessMetricStatus.COMPUTED:
            assert result.support.denominator > 0, metric


def test_every_na_result_carries_a_reason_and_no_fraction() -> None:
    context = computation(full=False)
    for metric in BusinessMetricName:
        if metric in package.DIMENSIONAL_BUSINESS_METRICS:
            continue
        result = value_of(context, metric)
        if result.status is BusinessMetricStatus.N_A:
            assert result.reason is not None, metric
            assert result.value is None, metric
            assert result.support.numerator is None, metric
            assert result.support.denominator is None, metric
