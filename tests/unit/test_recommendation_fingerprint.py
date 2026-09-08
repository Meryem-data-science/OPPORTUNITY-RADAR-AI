"""Determinism of the recommendation digests, and what they are allowed to see."""

from dataclasses import replace

import pytest

from services.collector.qualification.fine_taxonomy import FineCategory
from services.collector.qualification.taxonomy import Domain, OpportunityType
from services.eligibility import GlobalStatus
from services.recommendation import (
    CONFIRMED_GAP_CODES,
    STRENGTH_CODES,
    UNKNOWN_CODES,
    RecommendationReasonCode,
    build_recommendation_assessment,
    build_recommendation_batch,
    canonical_recommendation_assessment_payload,
    recommendation_assessment_fingerprint,
)
from tests.unit.recommendation_fixtures import (
    LEGACY_FINE,
    fine,
    geography,
    opportunity,
    recommendation_input,
    rule_result,
)


def assess(**kwargs):
    return build_recommendation_assessment(recommendation_input(**kwargs))


def digest(**kwargs):
    return assess(**kwargs).assessment_fingerprint


def test_a_fingerprint_is_a_sha256_and_repeats_exactly():
    first, second = digest(), digest()
    assert first == second
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_the_fingerprint_is_the_digest_of_the_canonical_payload():
    result = assess()
    recomputed = recommendation_assessment_fingerprint(
        replace(result, assessment_fingerprint="")
    )
    assert recomputed == result.assessment_fingerprint
    payload = canonical_recommendation_assessment_payload(result)
    assert payload["result"]["recommendation_score"] == result.recommendation_score
    assert "profile_id" not in payload and "opportunity_id" not in payload


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("required skill", {"required": 0.9}),
        ("semantic", {"semantic": 0.1}),
        ("fine category", {"fine_classification": fine(FineCategory.GENERATIVE_AI)}),
        (
            "coarse domain in fallback",
            {
                "opp": opportunity(domain=Domain.DATA_ENGINEERING),
                "fine_classification": LEGACY_FINE,
            },
        ),
        ("opportunity type", {"opp": opportunity(opportunity_type=OpportunityType.PFE)}),
        ("work mode", {"opp": opportunity(remote_type="on_site")}),
        ("geography", {"geo": geography(locations=("Paris, France",))}),
        ("eligibility", {"eligible": GlobalStatus.INELIGIBLE}),
        ("baseline provenance", {"fingerprint": "b" * 64}),
    ],
)
def test_changing_one_business_signal_changes_the_fingerprint(label, kwargs):
    assert digest(**kwargs) != digest(), label


def test_the_unused_coarse_reading_cannot_reach_the_fingerprint():
    """The digest holds what the result depends on, and nothing it ignores.

    While the fine reading answers, the coarse domain contributes no weight and
    no value, so changing it changes nothing here — which is the anti
    double-counting rule restated as a digest. The coarse reading is not lost:
    it travels in `baseline.assessment_fingerprint`, the digest of the whole
    persisted Phase 4 assessment.
    """
    data_science = digest(fine_classification=fine(FineCategory.DATA_SCIENCE))
    engineering_coarse = digest(
        opp=opportunity(domain=Domain.DATA_ENGINEERING),
        fine_classification=fine(FineCategory.DATA_SCIENCE),
    )
    assert data_science == engineering_coarse
    payload = canonical_recommendation_assessment_payload(assess())
    assert payload["baseline"]["assessment_fingerprint"] == "a" * 64


def test_two_opportunities_with_identical_content_fingerprint_identically():
    first = assess()
    second = assess(opp=opportunity(opportunity_id=99), geo=geography(opportunity_id=99))
    assert first.opportunity_id != second.opportunity_id
    assert first.assessment_fingerprint == second.assessment_fingerprint


def _batch(*ids, **kwargs):
    inputs = [
        recommendation_input(
            opp=opportunity(opportunity_id=item), geo=geography(opportunity_id=item),
            **kwargs,
        )
        for item in ids
    ]
    return build_recommendation_batch(inputs[0].matching.profile_id, inputs)


def test_the_batch_fingerprint_ignores_the_input_order_and_states_the_ranking():
    forward = _batch(1, 2, 3)
    backward = _batch(3, 2, 1)
    assert forward.batch_fingerprint == backward.batch_fingerprint
    assert len(forward.batch_fingerprint) == 64
    assert forward.assessment_count == 3


def test_a_batch_that_ranks_differently_fingerprints_differently():
    base = _batch(1, 2)
    changed = build_recommendation_batch(
        base.profile_id,
        [
            recommendation_input(
                opp=opportunity(opportunity_id=1),
                geo=geography(opportunity_id=1),
                required=0.1,
            ),
            recommendation_input(
                opp=opportunity(opportunity_id=2), geo=geography(opportunity_id=2)
            ),
        ],
    )
    assert base.batch_fingerprint != changed.batch_fingerprint


def test_no_clock_or_path_can_reach_a_fingerprint():
    payload = canonical_recommendation_assessment_payload(assess())
    flat = repr(payload)
    for forbidden in ("created_at", "evaluated_at", "run_id", "/", "\\\\"):
        assert forbidden not in flat


def test_every_reason_code_is_partitioned_exactly_once():
    assert STRENGTH_CODES | CONFIRMED_GAP_CODES | UNKNOWN_CODES == set(
        RecommendationReasonCode
    )
    assert not STRENGTH_CODES & CONFIRMED_GAP_CODES
    assert not STRENGTH_CODES & UNKNOWN_CODES
    assert not CONFIRMED_GAP_CODES & UNKNOWN_CODES


def test_reason_codes_are_sorted_in_the_payload_so_emission_order_cannot_leak():
    payload = canonical_recommendation_assessment_payload(assess())
    for bucket in ("strengths", "confirmed_gaps", "unknowns"):
        assert payload["result"][bucket] == sorted(payload["result"][bucket])


# --------------------------------------------------------------------------
# the explanation is part of the result, so it is part of the digest
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("skill evidence", {"profile_skills": ("Python",)}),
        ("declared constraints", {"declared_constraints": ("TEST ONLY contrainte",)}),
        (
            "eligibility reasons",
            {
                "eligibility_results": (
                    rule_result(explanation="TEST ONLY another explanation."),
                )
            },
        ),
    ],
)
def test_changing_what_a_reader_would_be_shown_changes_the_fingerprint(label, kwargs):
    assert digest(**kwargs) != digest(), label


def test_the_upstream_fingerprints_actually_consumed_are_in_the_payload():
    payload = canonical_recommendation_assessment_payload(assess())
    assert len(payload["required_skill"]["upstream_fingerprint"]) == 64
    assert payload["baseline"]["assessment_fingerprint"] == "a" * 64
    assert payload["eligibility"]["upstream_fingerprint"] == "e" * 64
    assert payload["versions"]["geographic_resolver"] == "geographic-resolver-v1"
    assert payload["versions"]["eligibility_engine"] == "eligibility-rules-v1"


def test_the_evidence_keeps_the_order_that_carries_meaning():
    payload = canonical_recommendation_assessment_payload(
        assess(profile_skills=("Python",))
    )
    skills = payload["required_skill"]["evidence"]
    # Upstream's own deterministic order, which is what a reader is shown.
    assert [item["canonical_key"] for item in skills] == ["aws", "python", "sql"]
    assert [item["confirmed_in_profile"] for item in skills] == [False, True, False]
    assert payload["declared_constraints"] == []
    assert payload["geography"]["resolved_countries"] == ["MA"]


def test_the_same_explanation_in_a_different_upstream_order_is_the_same_digest():
    """A digest is over content; a normalizer version list is a set, not a list."""
    first = assess(profile_skills=("Python", "SQL"))
    second = assess(profile_skills=("SQL", "Python"))
    assert first.assessment_fingerprint == second.assessment_fingerprint
