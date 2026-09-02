from services.portfolio import (
    canonical_portfolio_run_payload,
    portfolio_run_fingerprint,
)


def values():
    return dict(
        input_assembly_version="portfolio-input-assembly-v1",
        profile_id=1,
        priority_run_fingerprint="a" * 64,
        matching_run_fingerprint="b" * 64,
        portfolio_engine_version="portfolio-engine-v1",
        portfolio_rules_version="portfolio-rules-v1",
        assessment_count=2,
        included_count=1,
        excluded_count=1,
        bucket_counts={"AMBITIOUS": 0, "TARGET": 1, "SAFE": 0},
        assessments=((2, "d" * 64), (1, "c" * 64)),
    )


def test_run_payload_is_explicit_and_cohort_order_independent():
    original = values()
    reversed_cohort = original | {
        "assessments": tuple(reversed(original["assessments"]))
    }
    assert portfolio_run_fingerprint(**original) == portfolio_run_fingerprint(
        **reversed_cohort
    )
    payload = canonical_portfolio_run_payload(**original)
    assert [item["opportunity_id"] for item in payload["assessments"]] == [1, 2]
    assert payload["bucket_counts"] == {"SAFE": 0, "TARGET": 1, "AMBITIOUS": 0}


def test_every_semantic_authority_changes_identity():
    original = values()
    fingerprint = portfolio_run_fingerprint(**original)
    changes = (
        {"profile_id": 2},
        {"priority_run_fingerprint": "e" * 64},
        {"matching_run_fingerprint": "f" * 64},
        {"portfolio_engine_version": "portfolio-engine-v2"},
        {"assessments": ((1, "9" * 64), (2, "d" * 64))},
    )
    assert all(
        portfolio_run_fingerprint(**(original | change)) != fingerprint
        for change in changes
    )


def test_operational_ids_and_timestamps_are_not_inputs():
    original = values()
    assert portfolio_run_fingerprint(
        **original, priority_run_id=1, matching_run_id=2
    ) == portfolio_run_fingerprint(**original, priority_run_id=99, matching_run_id=100)
    assert set(canonical_portfolio_run_payload(**values())) == {
        "persistence_version",
        "input_assembly_version",
        "profile_id",
        "priority_run_fingerprint",
        "matching_run_fingerprint",
        "portfolio_engine_version",
        "portfolio_rules_version",
        "assessment_count",
        "included_count",
        "excluded_count",
        "bucket_counts",
        "assessments",
    }
