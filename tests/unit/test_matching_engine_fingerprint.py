from dataclasses import replace

from services.collector.matching import (
    SEMANTIC_BINDING_VERSION,
    MatchLane,
    MatchingBatchResult,
    canonical_matching_batch_payload,
    matching_assessment_fingerprint,
    matching_batch_fingerprint,
)
from tests.unit.test_matching_engine import assessment


def test_assessment_fingerprint_excludes_ids_and_is_semantically_sensitive():
    original = assessment()
    assert matching_assessment_fingerprint(original) == matching_assessment_fingerprint(
        replace(original, profile_id=999, opportunity_id=888)
    )
    variants = (
        replace(original, matching_rules_version="matching-rules-v2"),
        replace(original, semantic_percentile_version="semantic-percentile-v2"),
        replace(original, lane=MatchLane.UNCERTAIN),
        replace(
            original,
            required_skill=replace(original.required_skill, normalized_score=0.7),
        ),
        replace(original, semantic=replace(original.semantic, raw_similarity=0.41)),
        replace(original, semantic=replace(original.semantic, percentile=0.6)),
        replace(
            original, semantic=replace(original.semantic, corpus_fingerprint="other")
        ),
        replace(
            original, semantic=replace(original.semantic, model_fingerprint="other")
        ),
        replace(original, domain=replace(original.domain, preferred_rank=3)),
        replace(original, evidence_coverage=0.8),
        replace(
            original, preferred_skill=replace(original.preferred_skill, matched_count=0)
        ),
        replace(
            original, required_skill=replace(original.required_skill, base_weight=0.4)
        ),
    )
    assert all(
        matching_assessment_fingerprint(item) != original.assessment_fingerprint
        for item in variants
    )


def test_batch_fingerprint_is_order_independent_and_multiplicity_sensitive():
    first = assessment()
    second = replace(first, assessment_fingerprint="f" * 64)
    batch = MatchingBatchResult((first, second), "corpus", "model", 2)
    reversed_batch = replace(batch, assessments=(second, first))
    assert matching_batch_fingerprint(batch) == matching_batch_fingerprint(
        reversed_batch
    )
    duplicated = replace(batch, assessments=(first, first))
    assert matching_batch_fingerprint(batch) != matching_batch_fingerprint(duplicated)


def test_batch_fingerprint_stays_identity_independent_of_semantic_binding():
    """The boundary between the two fingerprint layers, stated as a test.

    The batch fingerprint is a **content** identity: what was computed, with no
    opportunity id anywhere in it. Semantic binding provenance is an identity
    statement about which document belongs to which posting, so it is carried on
    the batch result and deliberately kept out of this digest — it is protected
    by `matching_run_fingerprint`, the layer that already owns operational ids.

    Two batches differing only in their binding therefore share this digest, and
    that is intentional rather than an oversight.
    """
    item = assessment()
    base = MatchingBatchResult((item,), "a" * 64, "b" * 64, 1)
    bound = replace(
        base,
        semantic_binding_version=SEMANTIC_BINDING_VERSION,
        semantic_binding_fingerprint="1" * 64,
    )
    rebound = replace(bound, semantic_binding_fingerprint="2" * 64)

    assert matching_batch_fingerprint(base) == matching_batch_fingerprint(bound)
    assert matching_batch_fingerprint(bound) == matching_batch_fingerprint(rebound)
    payload = canonical_matching_batch_payload(bound)
    assert "semantic_binding_version" not in payload
    assert "semantic_binding_fingerprint" not in payload
    assert "opportunity_id" not in repr(payload)
