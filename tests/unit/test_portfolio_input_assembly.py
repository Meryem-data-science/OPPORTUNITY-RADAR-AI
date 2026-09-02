from services.portfolio.input_assembly import (
    PORTFOLIO_INPUT_ASSEMBLY_VERSION,
    PortfolioAssemblyIssueCode,
    PortfolioAssemblyStatus,
    _required_skill,
)


def test_phase_version_and_status_contracts_are_stable():
    assert PORTFOLIO_INPUT_ASSEMBLY_VERSION == "portfolio-input-assembly-v1"
    assert PortfolioAssemblyStatus.READY.value == "READY"
    assert PortfolioAssemblyStatus.INCOMPLETE.value == "INCOMPLETE"
    assert {item.value for item in PortfolioAssemblyIssueCode} >= {
        "PRIORITY_NOT_SYNCED",
        "PRIORITY_AUDIT_CORRUPT",
        "MATCHING_RUN_MISSING",
        "MATCHING_PROFILE_MISMATCH",
        "MATCHING_RUN_FINGERPRINT_MISMATCH",
        "MATCHING_RUN_AUDIT_FAILED",
        "PORTFOLIO_COHORT_MISMATCH",
        "MATCHING_ASSESSMENT_PROVENANCE_MISMATCH",
        "MATCHING_LANE_PROVENANCE_MISMATCH",
        "MATCHING_VERSION_PROVENANCE_MISMATCH",
        "REQUIRED_SKILL_SNAPSHOT_INVALID",
    }


def test_required_skill_snapshot_values_are_preserved_exactly():
    cases = (
        (1.0, 3, 3),
        (2 / 3, 2, 3),
        (0.0, 0, 3),
        (None, 0, 0),
    )
    for score, matched, total in cases:
        assert _required_skill(
            {
                "required_skill": {
                    "normalized_score": score,
                    "matched_count": matched,
                    "total_count": total,
                    "base_weight": 0.5,
                    "upstream_fingerprint": "a" * 64,
                }
            }
        ) == (score, matched, total)


def test_required_skill_snapshot_rejects_malformed_values():
    assert _required_skill({}) is None
    assert _required_skill({"required_skill": []}) is None
    assert (
        _required_skill(
            {
                "required_skill": {
                    "normalized_score": 1.0,
                    "matched_count": 0,
                    "total_count": 0,
                }
            }
        )
        is None
    )
    assert (
        _required_skill(
            {
                "required_skill": {
                    "normalized_score": 0.5,
                    "matched_count": 2,
                    "total_count": 1,
                }
            }
        )
        is None
    )
