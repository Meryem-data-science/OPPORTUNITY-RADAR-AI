import pytest

from services.collector.matching import (
    MATCHING_PERSISTENCE_VERSION,
    SEMANTIC_BINDING_VERSION,
    canonical_matching_run_payload,
    matching_run_fingerprint,
)


def values():
    return dict(
        profile_id=7,
        selection_version="selection-test-v1",
        matching_engine_version="matching-engine-v1",
        matching_rules_version="matching-rules-v1",
        semantic_percentile_version="semantic-percentile-v1",
        corpus_fingerprint="a" * 64,
        tfidf_model_fingerprint="b" * 64,
        batch_fingerprint="c" * 64,
        assessments=((20, "d" * 64), (10, "e" * 64)),
    )


def test_run_payload_is_canonical_ordered_and_has_no_timestamp():
    payload = canonical_matching_run_payload(**values())
    assert payload["persistence_version"] == MATCHING_PERSISTENCE_VERSION
    assert [item["opportunity_id"] for item in payload["assessments"]] == [10, 20]
    assert "timestamp" not in repr(payload)
    assert matching_run_fingerprint(**values()) == matching_run_fingerprint(
        **{**values(), "assessments": tuple(reversed(values()["assessments"]))}
    )


def test_run_fingerprint_is_operationally_sensitive():
    original = matching_run_fingerprint(**values())
    variants = (
        {"profile_id": 8},
        {"selection_version": "selection-test-v2"},
        {"persistence_version": "matching-persistence-v2"},
        {"batch_fingerprint": "f" * 64},
        {"assessments": ((20, "d" * 64), (11, "e" * 64))},
        {"assessments": ((20, "f" * 64), (10, "e" * 64))},
        {"assessments": ((20, "d" * 64), (10, "e" * 64), (10, "e" * 64))},
    )
    assert all(
        matching_run_fingerprint(**{**values(), **change}) != original
        for change in variants
    )


# --------------------------------------------------------------------------
# semantic binding provenance: additive, optional, and part of the operational
# identity rather than the content identity
# --------------------------------------------------------------------------

BINDING = {
    "semantic_binding_version": SEMANTIC_BINDING_VERSION,
    "semantic_binding_fingerprint": "1" * 64,
}


def test_a_legacy_run_payload_is_byte_for_byte_what_it_always_was():
    """Every run fingerprint already stored has to keep verifying."""
    payload = canonical_matching_run_payload(**values())
    assert set(payload) == {
        "persistence_version",
        "selection_version",
        "profile_id",
        "matching_engine_version",
        "matching_rules_version",
        "semantic_percentile_version",
        "corpus_fingerprint",
        "tfidf_model_fingerprint",
        "batch_fingerprint",
        "assessment_count",
        "assessments",
    }
    explicitly_absent = canonical_matching_run_payload(
        **values(),
        semantic_binding_version=None,
        semantic_binding_fingerprint=None,
    )
    assert explicitly_absent == payload


def test_the_run_fingerprint_is_binding_sensitive():
    """Same content, different document-to-posting assignment, new identity."""
    legacy = matching_run_fingerprint(**values())
    bound = matching_run_fingerprint(**values(), **BINDING)
    rebound = matching_run_fingerprint(
        **values(),
        semantic_binding_version=SEMANTIC_BINDING_VERSION,
        semantic_binding_fingerprint="2" * 64,
    )
    assert legacy != bound
    assert bound != rebound
    # Nothing else moved: the batch fingerprint is identical in all three.
    assert values()["batch_fingerprint"] == "c" * 64


def test_the_binding_payload_keys_are_the_only_addition():
    payload = canonical_matching_run_payload(**values(), **BINDING)
    legacy = canonical_matching_run_payload(**values())
    assert {key: payload[key] for key in legacy} == legacy
    assert set(payload) - set(legacy) == set(BINDING)


@pytest.mark.parametrize(
    "partial",
    [
        {"semantic_binding_version": SEMANTIC_BINDING_VERSION},
        {"semantic_binding_fingerprint": "1" * 64},
    ],
)
def test_half_a_binding_provenance_is_refused(partial):
    with pytest.raises(ValueError, match="both a version and a fingerprint"):
        canonical_matching_run_payload(**values(), **partial)
