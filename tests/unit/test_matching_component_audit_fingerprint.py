from dataclasses import replace

from services.collector.matching import (
    MatchingOpportunityInput,
    MatchingProfileInput,
    audit_matching_components,
    component_audit_fingerprint,
    component_observation_fingerprint,
)


def test_observation_fingerprint_excludes_technical_ids():
    profile = MatchingProfileInput(1)
    first = audit_matching_components(
        profile, (MatchingOpportunityInput(2, "python", "data python", None),)
    ).observations[0]
    changed = replace(first, profile_id=10, opportunity_id=20)
    assert component_observation_fingerprint(
        first
    ) == component_observation_fingerprint(changed)


def test_report_is_order_independent_and_sensitive_to_multiplicity_and_components():
    kwargs = dict(corpus_fingerprint="c", model_fingerprint="m")
    assert component_audit_fingerprint(
        **kwargs, observation_fingerprints=["a", "b"]
    ) == component_audit_fingerprint(**kwargs, observation_fingerprints=["b", "a"])
    assert component_audit_fingerprint(
        **kwargs, observation_fingerprints=["a"]
    ) != component_audit_fingerprint(**kwargs, observation_fingerprints=["a", "a"])
    assert component_audit_fingerprint(
        **kwargs, observation_fingerprints=["a"]
    ) != component_audit_fingerprint(
        corpus_fingerprint="x", model_fingerprint="m", observation_fingerprints=["a"]
    )
    assert component_audit_fingerprint(
        **kwargs, observation_fingerprints=["a"], audit_version="v2"
    ) != component_audit_fingerprint(**kwargs, observation_fingerprints=["a"])
    assert component_audit_fingerprint(
        **kwargs, observation_fingerprints=["a"], selection_version="v2"
    ) != component_audit_fingerprint(**kwargs, observation_fingerprints=["a"])


def test_core_report_is_input_order_independent():
    profile = MatchingProfileInput(1)
    a = MatchingOpportunityInput(2, "python data", "engineering", None)
    b = MatchingOpportunityInput(3, "machine learning", "python", None)
    left = audit_matching_components(profile, (a, b))
    right = audit_matching_components(profile, (b, a))
    assert left == right
