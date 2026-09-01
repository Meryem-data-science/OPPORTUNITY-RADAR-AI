from dataclasses import FrozenInstanceError

import pytest

from services.collector.matching import (
    MatchingOpportunityInput,
    MatchingProfileInput,
    audit_matching_components,
    numeric_distribution,
    pearson_correlation,
)


def opportunity(identifier: int, text: str = "python data") -> MatchingOpportunityInput:
    return MatchingOpportunityInput(identifier, text, text, "remote")


def test_observation_is_immutable():
    observation = audit_matching_components(
        MatchingProfileInput(1), (opportunity(2),)
    ).observations[0]
    with pytest.raises(FrozenInstanceError):
        observation.required_total = 2


def test_duplicate_opportunity_id_is_rejected():
    with pytest.raises(ValueError, match="duplicate opportunity_id"):
        audit_matching_components(
            MatchingProfileInput(1), (opportunity(2), opportunity(2))
        )


def test_unknown_coverage_remains_unavailable_and_zero_is_distinct():
    report = audit_matching_components(MatchingProfileInput(1), (opportunity(2),))
    assert report.observations[0].required_coverage is None
    distribution = numeric_distribution((None, 0, 0.5, 1))
    assert (
        distribution.count,
        distribution.zero_count,
        distribution.partial_count,
        distribution.full_count,
    ) == (3, 1, 1, 1)


def test_numeric_distribution_nearest_rank_and_population_stddev():
    empty = numeric_distribution(())
    assert empty.count == 0 and empty.median is None
    one = numeric_distribution((0.25,))
    assert one.standard_deviation == 0
    known = numeric_distribution((1, 2, 3, 4, 5), ratios=False)
    assert (known.median, known.p75, known.p90) == (3, 4, 5)
    assert known.standard_deviation == 1.414213562373


@pytest.mark.parametrize(
    "pairs, expected",
    [
        ([(1, 1), (2, 2)], 1.0),
        ([(1, 2), (2, 1)], -1.0),
        ([(1, 1), (1, 2)], None),
        ([(1, 1)], None),
    ],
)
def test_pearson_cases(pairs, expected):
    assert pearson_correlation(pairs).pearson_r == expected


def test_pearson_excludes_unavailable_rows():
    result = pearson_correlation(((1, 1), (None, 2), (2, 2)))
    assert (result.sample_count, result.pearson_r) == (2, 1.0)


def test_evidence_histogram_has_all_buckets():
    report = audit_matching_components(MatchingProfileInput(1), (opportunity(2),))
    assert tuple(key for key, _ in report.evidence_availability_histogram) == tuple(
        range(8)
    )
    assert sum(value for _, value in report.evidence_availability_histogram) == 1
