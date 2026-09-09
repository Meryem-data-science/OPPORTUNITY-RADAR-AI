"""The operational identity of a recommendation run, and what it must separate.

Phase 9A's `recommendation_batch_fingerprint` is a **content** digest: it holds
the versions, the size and the ranked assessment digests, and deliberately no
profile id, no opportunity id and no run id. Phase 9B.1 does not touch it. What
it adds is a second, **identity-aware** digest, and every test here is about the
line between the two.

The one that matters most is at the bottom: two batches whose assessments are
byte-identical in content but attached to different postings share a batch
fingerprint — correctly, they computed the same thing — and must not share a run
fingerprint, because they are not the same run. Persisting under the content
digest alone would collapse them into one stored row.
"""

from dataclasses import replace

import pytest

from services.recommendation import (
    RECOMMENDATION_ENGINE_VERSION,
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RECOMMENDATION_PERSISTENCE_VERSION,
    RECOMMENDATION_RULES_VERSION,
    RecommendationBatchResult,
    build_recommendation_assessment,
    canonical_recommendation_run_payload,
    recommendation_assessment_fingerprint,
    recommendation_batch_fingerprint,
    recommendation_run_fingerprint,
)
from tests.unit.recommendation_fixtures import (
    PROFILE_ID,
    opportunity,
    recommendation_input,
)

MATCHING_RUN_ID = 41
MATCHING_RUN_FINGERPRINT = "b" * 64


def assessment_for(opportunity_id: int, **kwargs):
    """One real 9A assessment, carrying its own real fingerprint."""
    return build_recommendation_assessment(
        recommendation_input(opp=opportunity(opportunity_id=opportunity_id), **kwargs)
    )


def batch_of(*assessments) -> RecommendationBatchResult:
    """A batch in exactly the order given, so a test can state the ranking."""
    raw = RecommendationBatchResult(
        profile_id=PROFILE_ID,
        assessments=tuple(assessments),
        assessment_count=len(assessments),
    )
    return replace(raw, batch_fingerprint=recommendation_batch_fingerprint(raw))


def run_values(batch, **overrides):
    values = dict(
        profile_id=batch.profile_id,
        source_matching_run_id=MATCHING_RUN_ID,
        source_matching_run_fingerprint=MATCHING_RUN_FINGERPRINT,
        input_assembly_version=RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
        recommendation_engine_version=batch.recommendation_engine_version,
        recommendation_rules_version=batch.recommendation_rules_version,
        batch_fingerprint=batch.batch_fingerprint,
        ranked_assessments=tuple(
            (item.opportunity_id, item.assessment_fingerprint)
            for item in batch.assessments
        ),
    )
    values.update(overrides)
    return values


def digest(batch, **overrides) -> str:
    return recommendation_run_fingerprint(**run_values(batch, **overrides))


@pytest.fixture
def two_postings():
    return batch_of(assessment_for(7), assessment_for(9))


def test_a_run_fingerprint_is_a_sha256_and_repeats_exactly(two_postings):
    first, second = digest(two_postings), digest(two_postings)

    assert first == second
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")


def test_the_payload_states_the_versions_the_source_and_the_ranking(two_postings):
    payload = canonical_recommendation_run_payload(**run_values(two_postings))

    assert payload["persistence_version"] == RECOMMENDATION_PERSISTENCE_VERSION
    assert payload["input_assembly_version"] == RECOMMENDATION_INPUT_ASSEMBLY_VERSION
    assert payload["profile_id"] == PROFILE_ID
    assert payload["source_matching_run_id"] == MATCHING_RUN_ID
    assert payload["source_matching_run_fingerprint"] == MATCHING_RUN_FINGERPRINT
    assert payload["recommendation_engine_version"] == RECOMMENDATION_ENGINE_VERSION
    assert payload["recommendation_rules_version"] == RECOMMENDATION_RULES_VERSION
    assert payload["batch_fingerprint"] == two_postings.batch_fingerprint
    assert payload["assessment_count"] == 2
    assert payload["ranked_assessments"] == [
        {
            "rank_position": position,
            "opportunity_id": item.opportunity_id,
            "assessment_fingerprint": item.assessment_fingerprint,
        }
        for position, item in enumerate(two_postings.assessments, start=1)
    ]


def test_the_payload_holds_no_timestamp_no_run_id_and_no_runtime_value(two_postings):
    payload = canonical_recommendation_run_payload(**run_values(two_postings))
    keys = set(payload)

    assert not {key for key in keys if "_at" in key or "time" in key or "date" in key}
    assert "run_id" not in keys and "id" not in keys
    assert not {key for key in keys if "path" in key or "database" in key}
    # The only ids anywhere are the two operational ones this digest exists to
    # carry, plus the opportunity ids inside the ranking.
    assert {key for key in keys if key.endswith("_id")} == {
        "profile_id",
        "source_matching_run_id",
    }
    assert set(payload["ranked_assessments"][0]) == {
        "rank_position",
        "opportunity_id",
        "assessment_fingerprint",
    }


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("profile", {"profile_id": PROFILE_ID + 1}),
        ("source matching run", {"source_matching_run_id": MATCHING_RUN_ID + 1}),
        ("source matching fingerprint", {"source_matching_run_fingerprint": "c" * 64}),
        ("input assembly version", {"input_assembly_version": "other-assembly-v9"}),
        ("persistence version", {"persistence_version": "other-persistence-v9"}),
        ("batch fingerprint", {"batch_fingerprint": "d" * 64}),
    ],
)
def test_every_operational_value_moves_the_run_fingerprint(
    two_postings, label, overrides
):
    assert digest(two_postings, **overrides) != digest(two_postings), label


def test_reordering_the_ranking_moves_the_run_fingerprint(two_postings):
    """The order *is* the product, so it is part of the identity."""
    reversed_ranking = tuple(reversed(run_values(two_postings)["ranked_assessments"]))

    assert digest(two_postings, ranked_assessments=reversed_ranking) != digest(
        two_postings
    )


def test_a_different_assessment_fingerprint_moves_the_run_fingerprint(two_postings):
    ranking = list(run_values(two_postings)["ranked_assessments"])
    ranking[0] = (ranking[0][0], "e" * 64)

    assert digest(two_postings, ranked_assessments=tuple(ranking)) != digest(
        two_postings
    )


def test_a_different_batch_fingerprint_moves_the_run_fingerprint(two_postings):
    other = batch_of(assessment_for(7, required=0.1), assessment_for(9))

    assert other.batch_fingerprint != two_postings.batch_fingerprint
    assert digest(other) != digest(two_postings)


def test_identical_content_on_different_postings_shares_content_and_not_identity():
    """The separation this whole module exists for.

    Both batches assess the same thing: the assessments are content-identical,
    so Phase 9A's digest is the same and must stay the same — that is a correct
    statement about what was computed. They are nevertheless recommendations
    about **different postings**, so the operational identity must differ, and a
    persistence layer keyed on the content digest alone would have merged them.
    """
    first = batch_of(assessment_for(7))
    second = batch_of(assessment_for(8))

    assert (
        first.assessments[0].assessment_fingerprint
        == second.assessments[0].assessment_fingerprint
    )
    assert first.batch_fingerprint == second.batch_fingerprint
    assert digest(first) != digest(second)


def test_permuting_the_opportunity_ids_under_identical_content_moves_the_identity():
    """Same content digests, same order, ids exchanged: a different run."""
    left, right = assessment_for(7), assessment_for(9)
    assert left.assessment_fingerprint == right.assessment_fingerprint

    ranking = ((7, left.assessment_fingerprint), (9, right.assessment_fingerprint))
    swapped = ((9, right.assessment_fingerprint), (7, left.assessment_fingerprint))
    batch = batch_of(left, right)

    assert digest(batch, ranked_assessments=ranking) != digest(
        batch, ranked_assessments=swapped
    )


def test_the_batch_fingerprint_of_phase_9a_is_not_the_run_fingerprint(two_postings):
    """Non-regression: 9B.1 may not turn the 9A content digest into an identity."""
    payload_of_9a = {
        "recommendation_engine_version": two_postings.recommendation_engine_version,
        "recommendation_rules_version": two_postings.recommendation_rules_version,
        "assessment_count": two_postings.assessment_count,
        "ranked_assessment_fingerprints": [
            item.assessment_fingerprint for item in two_postings.assessments
        ],
    }
    import hashlib

    from services.collector.matching.fingerprint import canonical_json

    assert (
        hashlib.sha256(canonical_json(payload_of_9a).encode("utf-8")).hexdigest()
        == two_postings.batch_fingerprint
    )
    assert digest(two_postings) != two_postings.batch_fingerprint
