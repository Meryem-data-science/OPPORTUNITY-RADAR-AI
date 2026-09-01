from dataclasses import replace

import pytest

from services.collector.matching import (
    AlignmentReason,
    AlignmentStatus,
    MatchingInput,
    MatchingOpportunityInput,
    MatchingProfileInput,
    RoleDomainPreferencesInputError,
    build_role_domain_preference_signals,
)
from services.collector.matching.models import (
    MatchingPreferences,
    MatchingQualification,
)


def inputs(
    *,
    opportunity_type="PFE",
    domain="DATA_ENGINEERING",
    remote_type="remote",
    types=("PFE",),
    domains=("Data Engineering",),
    modes=("REMOTE",),
    preferences=True,
    qualification=True,
):
    prefs = (
        MatchingPreferences(types, modes, domains, "preferences-v1")
        if preferences
        else None
    )
    qual = (
        MatchingQualification("CORE_TARGET", domain, opportunity_type, "classifier-v1")
        if qualification
        else None
    )
    return MatchingInput(
        MatchingProfileInput(1, preferences=prefs),
        MatchingOpportunityInput(
            2, "ignored title", "ignored description", remote_type, qual
        ),
    )


@pytest.mark.parametrize(
    ("opportunity_type", "types", "status", "reason", "mapped"),
    [
        (
            "PFE",
            ("PFE",),
            AlignmentStatus.MATCH,
            AlignmentReason.OPPORTUNITY_TYPE_ALLOWED,
            "PFE",
        ),
        (
            "PFE",
            ("INTERNSHIP",),
            AlignmentStatus.MISMATCH,
            AlignmentReason.OPPORTUNITY_TYPE_NOT_ALLOWED,
            "PFE",
        ),
        (
            "INTERNSHIP",
            ("INTERNSHIP",),
            AlignmentStatus.MATCH,
            AlignmentReason.OPPORTUNITY_TYPE_ALLOWED,
            "INTERNSHIP",
        ),
        (
            "APPRENTICESHIP",
            ("ALTERNANCE",),
            AlignmentStatus.MATCH,
            AlignmentReason.OPPORTUNITY_TYPE_ALLOWED,
            "ALTERNANCE",
        ),
        (
            "GRADUATE",
            (),
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_TYPE_UNMAPPED,
            None,
        ),
        (
            "JOB",
            (),
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_TYPE_UNMAPPED,
            None,
        ),
        (
            "UNKNOWN",
            (),
            AlignmentStatus.UNKNOWN,
            AlignmentReason.OPPORTUNITY_TYPE_UNKNOWN,
            None,
        ),
    ],
)
def test_opportunity_type_bridge(opportunity_type, types, status, reason, mapped):
    result = build_role_domain_preference_signals(
        inputs(opportunity_type=opportunity_type, types=types)
    )
    assert (result.opportunity_type.status, result.opportunity_type.reason) == (
        status,
        reason,
    )
    assert result.opportunity_type.mapped_profile_type == mapped


@pytest.mark.parametrize(
    ("field", "value"), [("opportunity_type", "MAYBE"), ("domain", "MAYBE")]
)
def test_invalid_qualification_taxonomy_raises(field, value):
    with pytest.raises(RoleDomainPreferencesInputError):
        build_role_domain_preference_signals(inputs(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"), [("types", ("MAYBE",)), ("modes", ("MAYBE",))]
)
def test_invalid_profile_taxonomy_raises(field, value):
    with pytest.raises(RoleDomainPreferencesInputError):
        build_role_domain_preference_signals(inputs(**{field: value}))


def test_absent_preferences_makes_every_axis_unknown():
    result = build_role_domain_preference_signals(inputs(preferences=False))
    assert {
        result.opportunity_type.status,
        result.domain.status,
        result.work_mode.status,
    } == {AlignmentStatus.UNKNOWN}
    assert result.opportunity_type.reason is AlignmentReason.PROFILE_PREFERENCES_ABSENT


def test_absent_qualification_does_not_prevent_work_mode_match():
    result = build_role_domain_preference_signals(inputs(qualification=False))
    assert (
        result.opportunity_type.reason
        is AlignmentReason.OPPORTUNITY_QUALIFICATION_ABSENT
    )
    assert result.domain.reason is AlignmentReason.OPPORTUNITY_QUALIFICATION_ABSENT
    assert result.work_mode.status is AlignmentStatus.MATCH


@pytest.mark.parametrize(
    ("domain", "labels", "rank"),
    [
        ("DATA_ENGINEERING", ("Other Data/AI", "Data Engineering"), 2),
        ("MACHINE_LEARNING_AI", ("ML/AI",), 1),
        ("GENAI_LLM", ("GenAI/LLM",), 1),
        ("MLOPS_ML_PLATFORM", ("MLOps/ML Platform",), 1),
        ("BI_ANALYTICS", ("BI/Analytics",), 1),
        ("DATA_QUALITY_GOVERNANCE", ("Data Quality/Governance",), 1),
    ],
)
def test_domain_aliases_match_at_original_rank(domain, labels, rank):
    result = build_role_domain_preference_signals(inputs(domain=domain, domains=labels))
    assert result.domain.status is AlignmentStatus.MATCH
    assert result.domain.preferred_rank == rank


def test_domain_empty_is_unknown_and_complete_nonmatch_is_mismatch():
    empty = build_role_domain_preference_signals(inputs(domains=()))
    mismatch = build_role_domain_preference_signals(inputs(domains=("Data Science",)))
    assert empty.domain.reason is AlignmentReason.DOMAIN_PREFERENCES_EMPTY
    assert mismatch.domain.reason is AlignmentReason.DOMAIN_PREFERENCE_NOT_LISTED
    assert mismatch.domain.status is AlignmentStatus.MISMATCH


def test_unmapped_domain_text_prevents_mismatch_but_not_explicit_match():
    unknown = build_role_domain_preference_signals(
        inputs(domains=("Data Science", "Data Eng"))
    )
    matched = build_role_domain_preference_signals(
        inputs(domains=("custom", "Data Engineering"))
    )
    assert (
        unknown.domain.reason is AlignmentReason.DOMAIN_PREFERENCES_PARTIALLY_UNMAPPED
    )
    assert matched.domain.status is AlignmentStatus.MATCH
    assert matched.domain.preferred_rank == 2


def test_duplicate_semantic_alias_keeps_earliest_rank():
    result = build_role_domain_preference_signals(
        inputs(
            domain="MACHINE_LEARNING_AI",
            domains=("Data Science", "ML/AI", "Machine Learning / AI"),
        )
    )
    assert result.domain.preferred_rank == 2
    assert result.domain.mapped_preference_count == 2


@pytest.mark.parametrize(
    ("domain", "reason"),
    [
        ("UNKNOWN", AlignmentReason.PRIMARY_DOMAIN_UNKNOWN),
        ("NON_TARGET", AlignmentReason.PRIMARY_DOMAIN_NON_TARGET),
    ],
)
def test_non_evaluable_domains_are_unknown(domain, reason):
    result = build_role_domain_preference_signals(inputs(domain=domain))
    assert result.domain.status is AlignmentStatus.UNKNOWN
    assert result.domain.reason is reason


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("remote", "REMOTE"), ("HYBRID", "HYBRID"), ("on_site", "ON_SITE")],
)
def test_recognized_work_modes(raw, canonical):
    result = build_role_domain_preference_signals(
        inputs(remote_type=raw, modes=(canonical,))
    )
    assert result.work_mode.status is AlignmentStatus.MATCH
    assert result.work_mode.opportunity_mode == canonical


def test_work_mode_mismatch_absent_and_unmapped():
    mismatch = build_role_domain_preference_signals(inputs(modes=("HYBRID",)))
    absent = build_role_domain_preference_signals(inputs(remote_type="  "))
    unmapped = build_role_domain_preference_signals(inputs(remote_type="flexible"))
    assert mismatch.work_mode.status is AlignmentStatus.MISMATCH
    assert absent.work_mode.reason is AlignmentReason.REMOTE_TYPE_ABSENT
    assert unmapped.work_mode.reason is AlignmentReason.REMOTE_TYPE_UNMAPPED


def test_cross_axis_independence():
    result = build_role_domain_preference_signals(
        inputs(domains=("Data Science",), remote_type=None)
    )
    assert (
        result.opportunity_type.status,
        result.domain.status,
        result.work_mode.status,
    ) == (
        AlignmentStatus.MATCH,
        AlignmentStatus.MISMATCH,
        AlignmentStatus.UNKNOWN,
    )


def test_irrelevant_profile_and_opportunity_text_is_ignored():
    first = inputs()
    second = replace(
        first,
        profile=replace(first.profile, experiences=()),
        opportunity=replace(
            first.opportunity, canonical_title="different", description="different"
        ),
    )
    assert build_role_domain_preference_signals(
        first
    ) == build_role_domain_preference_signals(second)
