from dataclasses import replace

from services.collector.matching import (
    OpportunitySkillSignal,
    OpportunitySkillSignals,
    RequirementsState,
    SkillSignalKind,
    SkillSignalSource,
    build_skill_fit,
    canonical_skill_fit_payload,
    skill_fit_fingerprint,
)
from services.collector.matching.models import (
    MatchingInput, MatchingOpportunityInput, MatchingProfileInput,
    MatchingProfileSkill, MatchingRequiredSkill, MatchingRequirements,
)


def _fit(profile_skills=(), signals=None, profile_id=1, opportunity_id=2,
         input_version="matching-input-v1", signal_version="skill-signals-v1"):
    if signals is None:
        signals = (OpportunitySkillSignal(
            "python", "Python", SkillSignalKind.REQUIRED,
            (SkillSignalSource.REQUIREMENTS,),
        ),)
    requirements = MatchingRequirements(
        "requirements-v1",
        tuple(
            MatchingRequiredSkill(
                item.canonical_key, item.canonical_name, item.kind.value
            )
            for item in signals
            if item.kind in (SkillSignalKind.REQUIRED, SkillSignalKind.PREFERRED)
        ),
    )
    inputs = MatchingInput(
        MatchingProfileInput(profile_id, tuple(profile_skills)),
        MatchingOpportunityInput(
            opportunity_id, "ignored", "ignored", "REMOTE",
            requirements=requirements,
        ),
        input_version,
    )
    upstream = OpportunitySkillSignals(
        opportunity_id, RequirementsState.EXTRACTED, "requirements-v1",
        tuple(signals), signal_version,
    )
    return build_skill_fit(inputs, upstream)


def test_same_semantics_and_different_technical_ids_have_same_fingerprint():
    first = _fit(profile_id=1, opportunity_id=2)
    second = _fit(profile_id=99, opportunity_id=88)
    assert first != second
    assert skill_fit_fingerprint(first) == skill_fit_fingerprint(second)
    payload = canonical_skill_fit_payload(first)
    assert "profile_id" not in payload and "opportunity_id" not in payload
    assert payload["requirements_extractor_version"] == "requirements-v1"


def test_irrelevant_profile_skill_does_not_change_result_or_fingerprint():
    base = _fit((MatchingProfileSkill("python", "Python", ("v1",)),))
    changed = _fit((MatchingProfileSkill("python", "Python", ("v1",)),
                    MatchingProfileSkill("excel", "Excel", ("v1",))))
    assert base == changed
    assert skill_fit_fingerprint(base) == skill_fit_fingerprint(changed)


def test_relevant_skill_and_normalizer_version_change_fingerprint():
    missing = _fit()
    matched_v1 = _fit((MatchingProfileSkill("python", "Python", ("v1",)),))
    matched_v2 = _fit((MatchingProfileSkill("python", "Python", ("v2",)),))
    assert not missing.evaluations[0].matched and matched_v1.evaluations[0].matched
    assert len({skill_fit_fingerprint(x) for x in (missing, matched_v1, matched_v2)}) == 3


def test_kind_sources_and_upstream_versions_change_fingerprint():
    required = _fit()
    preferred = _fit(signals=(replace(required.evaluations[0],
        kind=SkillSignalKind.PREFERRED),))
    title_source = _fit(signals=(OpportunitySkillSignal(
        "python", "Python", SkillSignalKind.REQUIRED,
        (SkillSignalSource.REQUIREMENTS, SkillSignalSource.TITLE)),))
    input_v2 = _fit(input_version="matching-input-v2")
    signal_v2 = _fit(signal_version="skill-signals-v2")
    fingerprints = {skill_fit_fingerprint(item) for item in
                    (required, preferred, title_source, input_v2, signal_v2)}
    assert len(fingerprints) == 5


def test_nullable_canonicalization_is_stable():
    unknown = OpportunitySkillSignals(
        2, RequirementsState.UNKNOWN, None, (), "skill-signals-v1"
    )
    inputs = MatchingInput(MatchingProfileInput(1),
                           MatchingOpportunityInput(2, "role", None, None))
    fit = build_skill_fit(inputs, unknown)
    assert canonical_skill_fit_payload(fit)["requirements_extractor_version"] is None
    assert skill_fit_fingerprint(fit) == skill_fit_fingerprint(fit)
