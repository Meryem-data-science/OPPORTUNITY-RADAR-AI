from dataclasses import FrozenInstanceError

import pytest

from services.collector.matching import (
    DOMAIN_WEIGHT,
    REQUIRED_SKILL_WEIGHT,
    SEMANTIC_WEIGHT,
    AlignmentReason,
    AlignmentStatus,
    DomainPreferenceAlignment,
    MatchLane,
    MatchingEngineInputError,
    OpportunityTypeAlignment,
    RequirementsState,
    RoleDomainPreferencesResult,
    SemanticSimilarityResult,
    SemanticSimilarityStatus,
    SkillCoverage,
    SkillFitResult,
    WorkModeAlignment,
    build_matching_assessment,
    build_matching_assessments,
    semantic_percentiles,
)
from services.collector.matching.models import (
    MatchingOpportunityInput,
    MatchingPreferences,
    MatchingProfileInput,
)


def semantic(identifier, similarity, status=SemanticSimilarityStatus.AVAILABLE):
    return SemanticSimilarityResult(
        1,
        identifier,
        status,
        similarity,
        (),
        0,
        1,
        1,
        "corpus",
        "model",
        4,
        3,
        "semantic-doc-v1",
        "tfidf-v1",
        "sklearn",
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.1, 0.2, 0.3], [0.0, 0.5, 1.0]),
        ([0.1, 0.2, 0.2, 0.4], [0.0, 0.5, 0.5, 1.0]),
        ([0.2, 0.2, 0.2], [0.5, 0.5, 0.5]),
        ([0.2], [0.5]),
    ],
)
def test_semantic_percentile_midrank(values, expected):
    results = [semantic(index + 1, value) for index, value in enumerate(values)]
    actual = semantic_percentiles(results)
    assert [actual[index + 1] for index in range(len(values))] == expected


def test_unavailable_semantic_is_excluded_and_zero_is_available():
    results = (
        semantic(1, 0.0),
        semantic(2, None, SemanticSimilarityStatus.EMPTY_PROFILE_DOCUMENT),
    )
    assert semantic_percentiles(results) == {1: 0.5, 2: None}


def fit(required, domain_status=AlignmentStatus.MATCH, rank=1, type_status=None):
    type_status = type_status or AlignmentStatus.MATCH
    skill = SkillFitResult(
        1,
        7,
        RequirementsState.EXTRACTED,
        "extractor",
        (),
        SkillCoverage(
            0 if required is None else int(required * 10), 0 if required is None else 10
        ),
        SkillCoverage(1, 2),
        SkillCoverage(2, 3),
        "matching-input-v1",
        "skill-signals-v1",
    )
    structured = RoleDomainPreferencesResult(
        1,
        7,
        OpportunityTypeAlignment(
            type_status,
            AlignmentReason.OPPORTUNITY_TYPE_ALLOWED,
            "INTERNSHIP",
            "INTERNSHIP",
        ),
        DomainPreferenceAlignment(
            domain_status,
            AlignmentReason.DOMAIN_PREFERENCE_MATCHED
            if domain_status is AlignmentStatus.MATCH
            else (
                AlignmentReason.DOMAIN_PREFERENCE_NOT_LISTED
                if domain_status is AlignmentStatus.MISMATCH
                else AlignmentReason.PRIMARY_DOMAIN_UNKNOWN
            ),
            "DATA_ENGINEERING",
            rank if domain_status is AlignmentStatus.MATCH else None,
            1,
            0,
        ),
        WorkModeAlignment(
            AlignmentStatus.UNKNOWN, AlignmentReason.REMOTE_TYPE_ABSENT, None
        ),
        "matching-input-v1",
        "preferences-v1",
        "classifier-v1",
    )
    return skill, structured


def assessment(
    required=0.8,
    semantic_score=0.7,
    domain_status=AlignmentStatus.MATCH,
    rank=2,
    type_status=AlignmentStatus.MATCH,
):
    profile = MatchingProfileInput(
        1,
        preferences=MatchingPreferences(
            ("INTERNSHIP",),
            (),
            tuple(f"domain-{n}" for n in range(8)),
            "preferences-v1",
        ),
    )
    opportunity = MatchingOpportunityInput(7, "Python data", "Python data", None)
    skill, structured = fit(required, domain_status, rank, type_status)
    return build_matching_assessment(
        profile, opportunity, skill, semantic(7, 0.42), structured, semantic_score
    )


def test_weighted_quality_and_coverage_contract():
    result = assessment()
    assert (REQUIRED_SKILL_WEIGHT, SEMANTIC_WEIGHT, DOMAIN_WEIGHT) == (0.5, 0.3, 0.2)
    assert result.domain.normalized_score == 0.875
    assert result.match_quality == 0.785
    assert result.evidence_coverage == 1.0
    missing = assessment(required=None)
    assert missing.match_quality == 0.77
    assert missing.evidence_coverage == 0.5
    zero = assessment(required=0)
    assert zero.match_quality == 0.385
    assert zero.evidence_coverage == 1.0


@pytest.mark.parametrize(
    ("status", "lane"),
    [
        (AlignmentStatus.MATCH, MatchLane.PRIMARY),
        (AlignmentStatus.UNKNOWN, MatchLane.UNCERTAIN),
        (AlignmentStatus.MISMATCH, MatchLane.OUTSIDE_PREFERENCES),
    ],
)
def test_opportunity_type_selects_lane_without_changing_score(status, lane):
    result = assessment(type_status=status)
    assert result.lane is lane
    assert result.match_quality == 0.785


def test_domain_missing_semantics_and_models_are_immutable():
    mismatch = assessment(domain_status=AlignmentStatus.MISMATCH)
    assert mismatch.domain.normalized_score == 0.0
    assert mismatch.evidence_coverage == 1.0
    unknown = assessment(domain_status=AlignmentStatus.UNKNOWN)
    assert unknown.domain.normalized_score is None
    assert unknown.evidence_coverage == 0.8
    with pytest.raises(FrozenInstanceError):
        unknown.evidence_coverage = 0


def test_empty_and_duplicate_batches_fail_explicitly():
    profile = MatchingProfileInput(1)
    with pytest.raises(MatchingEngineInputError, match="must not be empty"):
        build_matching_assessments(profile, ())
    opportunity = MatchingOpportunityInput(1, "python", None, None)
    with pytest.raises(MatchingEngineInputError, match="duplicate opportunity_id"):
        build_matching_assessments(profile, (opportunity, opportunity))
