from services.collector.matching import (
    MATCHING_PERSISTENCE_VERSION,
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
