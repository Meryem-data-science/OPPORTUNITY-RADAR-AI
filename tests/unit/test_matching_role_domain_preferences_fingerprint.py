from dataclasses import replace

from services.collector.matching import (
    AlignmentStatus,
    build_role_domain_preference_signals,
    canonical_role_domain_preferences_payload,
    role_domain_preferences_fingerprint,
)
from tests.unit.test_matching_role_domain_preferences import inputs


def fingerprint(value):
    return role_domain_preferences_fingerprint(
        build_role_domain_preference_signals(value)
    )


def test_payload_excludes_ids_and_fingerprint_is_deterministic():
    result = build_role_domain_preference_signals(inputs())
    payload = canonical_role_domain_preferences_payload(result)
    assert "profile_id" not in payload and "opportunity_id" not in payload
    assert role_domain_preferences_fingerprint(
        result
    ) == role_domain_preferences_fingerprint(
        replace(result, profile_id=9, opportunity_id=10)
    )


def test_irrelevant_allowed_values_and_later_domain_do_not_change_fingerprint():
    base = inputs(domains=("Data Science", "Data Engineering"))
    expanded = inputs(
        types=("PFE", "INTERNSHIP"),
        modes=("REMOTE", "HYBRID"),
        domains=("Data Science", "Data Engineering", "BI/Analytics"),
    )
    assert fingerprint(base) == fingerprint(expanded)


def test_relevant_status_rank_and_input_versions_change_fingerprint():
    base = inputs(domains=("Data Science", "Data Engineering"))
    assert fingerprint(base) != fingerprint(
        inputs(types=(), domains=("Data Science", "Data Engineering"))
    )
    assert fingerprint(base) != fingerprint(
        inputs(modes=(), domains=("Data Science", "Data Engineering"))
    )
    assert fingerprint(base) != fingerprint(
        inputs(domains=("BI/Analytics", "Data Science", "Data Engineering"))
    )
    built = build_role_domain_preference_signals(base)
    for field in (
        "preferences_input_version",
        "qualification_classifier_version",
        "domain_preference_bridge_version",
    ):
        assert role_domain_preferences_fingerprint(
            built
        ) != role_domain_preferences_fingerprint(replace(built, **{field: "changed"}))


def test_official_alias_spelling_is_semantically_stable():
    left = build_role_domain_preference_signals(
        inputs(domain="MACHINE_LEARNING_AI", domains=("ML/AI",))
    )
    right = build_role_domain_preference_signals(
        inputs(domain="MACHINE_LEARNING_AI", domains=("ML / AI",))
    )
    assert left == right
    assert role_domain_preferences_fingerprint(
        left
    ) == role_domain_preferences_fingerprint(right)
    assert left.domain.status is AlignmentStatus.MATCH


def test_new_alias_is_semantically_identical_to_existing_canonical_alias():
    existing = build_role_domain_preference_signals(
        inputs(domain="DATA_ENGINEERING", domains=("Data Engineering",))
    )
    added = build_role_domain_preference_signals(
        inputs(domain="DATA_ENGINEERING", domains=("Big Data / Data Platforms",))
    )
    assert existing == added
    assert role_domain_preferences_fingerprint(
        existing
    ) == role_domain_preferences_fingerprint(added)


def test_v2_versions_change_fingerprint_from_v1_versions():
    current = build_role_domain_preference_signals(inputs())
    previous = replace(
        current,
        role_domain_preferences_version="role-domain-preferences-v1",
        opportunity_type_bridge_version="opportunity-type-bridge-v1",
        domain_preference_bridge_version="domain-preference-bridge-v1",
    )
    assert role_domain_preferences_fingerprint(
        current
    ) != role_domain_preferences_fingerprint(previous)


def test_job_unknown_and_safe_negative_mismatch_have_distinct_fingerprints():
    mismatch = build_role_domain_preference_signals(
        inputs(opportunity_type="JOB", types=("PFE", "INTERNSHIP"))
    )
    unknown = build_role_domain_preference_signals(
        inputs(opportunity_type="JOB", types=("FIRST_JOB",))
    )
    assert mismatch.opportunity_type.status is AlignmentStatus.MISMATCH
    assert unknown.opportunity_type.status is AlignmentStatus.UNKNOWN
    assert role_domain_preferences_fingerprint(
        mismatch
    ) != role_domain_preferences_fingerprint(unknown)
