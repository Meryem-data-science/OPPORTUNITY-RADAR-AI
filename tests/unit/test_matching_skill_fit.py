from dataclasses import FrozenInstanceError

import pytest

from services.collector.matching import (
    SKILL_FIT_VERSION,
    OpportunitySkillSignal,
    OpportunitySkillSignals,
    RequirementsState,
    SkillCoverage,
    SkillFitInputError,
    SkillSignalKind,
    SkillSignalSource,
    build_skill_fit,
)
from services.collector.matching.models import (
    MatchingInput,
    MatchingOpportunityInput,
    MatchingProfileInput,
    MatchingProfileSkill,
    MatchingRequiredSkill,
    MatchingRequirements,
)


def _input(
    skills=(), opportunity_id=7, profile_id=3, version="matching-input-v1",
    requirements=(), extractor="requirements-v1",
):
    persisted = (
        None
        if requirements is None
        else MatchingRequirements(extractor, tuple(requirements))
    )
    return MatchingInput(
        MatchingProfileInput(profile_id, tuple(skills)),
        MatchingOpportunityInput(
            opportunity_id, "Raw title", None, None, requirements=persisted
        ),
        version,
    )


def _signal(key, name=None, kind=SkillSignalKind.REQUIRED, sources=None):
    return OpportunitySkillSignal(
        key,
        name or key.title(),
        kind,
        sources or (SkillSignalSource.REQUIREMENTS,),
    )


def _signals(
    items=(), state=RequirementsState.EXTRACTED, extractor="requirements-v1",
    opportunity_id=7, version="skill-signals-v1",
):
    return OpportunitySkillSignals(
        opportunity_id, state, extractor, tuple(items), version
    )


def _requirement(key, name=None, kind=SkillSignalKind.REQUIRED):
    return MatchingRequiredSkill(key, name or key.title(), kind.value)


def test_version_immutability_and_zero_denominator():
    fit = build_skill_fit(_input(), _signals())
    assert fit.skill_fit_version == SKILL_FIT_VERSION == "skill-fit-v1"
    assert fit.required_coverage == SkillCoverage(0, 0)
    assert fit.required_coverage.ratio is None
    with pytest.raises(FrozenInstanceError):
        fit.profile_id = 4
    with pytest.raises(FrozenInstanceError):
        fit.required_coverage.total_count = 1


def test_exact_key_only_not_name_or_related_technology():
    profile = (
        MatchingProfileSkill("other-key", "Python", ("v1",)),
        MatchingProfileSkill("pyspark", "PySpark", ("v2", "v1", "v2")),
    )
    fit = build_skill_fit(
        _input(profile, requirements=(
            _requirement("python", "Python"),
            _requirement("apache spark", "PySpark"),
        )),
        _signals((_signal("python", "Python"), _signal("apache spark", "PySpark"))),
    )
    assert all(not item.matched for item in fit.evaluations)
    assert all(item.profile_normalizer_versions == () for item in fit.evaluations)


def test_independent_required_preferred_and_context_coverage():
    profile = tuple(
        MatchingProfileSkill(key, key.title(), ("v2", "v1", "v2"))
        for key in ("python", "docker", "airflow", "kafka")
    )
    signals = (
        _signal("python"), _signal("sql"), _signal("docker"),
        _signal("airflow", kind=SkillSignalKind.PREFERRED),
        _signal("dbt", kind=SkillSignalKind.PREFERRED),
        _signal("kafka", kind=SkillSignalKind.CONTEXT,
                sources=(SkillSignalSource.TECH_STACK,)),
        _signal("spark", kind=SkillSignalKind.CONTEXT,
                sources=(SkillSignalSource.TITLE,)),
    )
    fit = build_skill_fit(
        _input(profile, requirements=tuple(
            _requirement(item.canonical_key, item.canonical_name, item.kind)
            for item in signals
            if item.kind in (SkillSignalKind.REQUIRED, SkillSignalKind.PREFERRED)
        )),
        _signals(signals),
    )
    assert fit.required_coverage == SkillCoverage(2, 3)
    assert fit.required_coverage.ratio == 2 / 3
    assert fit.preferred_coverage == SkillCoverage(1, 2)
    assert fit.context_overlap == SkillCoverage(1, 2)
    context_miss = next(item for item in fit.evaluations if item.canonical_key == "spark")
    assert context_miss.kind is SkillSignalKind.CONTEXT
    assert not context_miss.matched
    matched = next(item for item in fit.evaluations if item.canonical_key == "python")
    assert matched.profile_normalizer_versions == ("v1", "v2")


def test_unknown_and_extracted_empty_remain_distinct():
    unknown = build_skill_fit(
        _input(requirements=None),
        _signals((_signal("sql", kind=SkillSignalKind.CONTEXT,
                          sources=(SkillSignalSource.TITLE,)),),
                 RequirementsState.UNKNOWN, None),
    )
    extracted = build_skill_fit(_input(), _signals())
    assert unknown.requirements_state is RequirementsState.UNKNOWN
    assert extracted.requirements_state is RequirementsState.EXTRACTED
    assert unknown.required_coverage.ratio is extracted.required_coverage.ratio is None
    assert unknown.preferred_coverage.ratio is extracted.preferred_coverage.ratio is None


def test_mismatch_and_duplicate_keys_fail_explicitly():
    with pytest.raises(SkillFitInputError, match="ID mismatch"):
        build_skill_fit(_input(), _signals(opportunity_id=8))
    duplicate_skill = MatchingProfileSkill("python", "Python", ("v1",))
    with pytest.raises(SkillFitInputError, match="duplicate profile skill"):
        build_skill_fit(_input((duplicate_skill, duplicate_skill)), _signals())
    duplicate_signal = _signal("python")
    with pytest.raises(SkillFitInputError, match="duplicate opportunity signal"):
        build_skill_fit(
            _input(requirements=(_requirement("python"),)),
            _signals((duplicate_signal, duplicate_signal)),
        )


@pytest.mark.parametrize("kind", [SkillSignalKind.REQUIRED, SkillSignalKind.PREFERRED])
def test_unknown_rejects_requirement_signals(kind):
    with pytest.raises(SkillFitInputError, match="UNKNOWN"):
        build_skill_fit(
            _input(requirements=None),
            _signals((_signal("python", kind=kind),), RequirementsState.UNKNOWN, None)
        )


@pytest.mark.parametrize(("state", "extractor", "requirements"), [
    (RequirementsState.UNKNOWN, "v1", None),
    (RequirementsState.EXTRACTED, None, ()),
    (RequirementsState.EXTRACTED, "", ()),
])
def test_incoherent_extractor_versions_fail(state, extractor, requirements):
    with pytest.raises(SkillFitInputError, match="extractor version"):
        build_skill_fit(
            _input(requirements=requirements),
            _signals(state=state, extractor=extractor),
        )


def test_upstream_versions_must_be_nonempty():
    with pytest.raises(SkillFitInputError, match="matching_input_version"):
        build_skill_fit(_input(version=""), _signals())
    with pytest.raises(SkillFitInputError, match="skill_signal_version"):
        build_skill_fit(_input(), _signals(version=""))


def test_semantically_unordered_inputs_have_deterministic_evaluations():
    skills = (
        MatchingProfileSkill("sql", "SQL", ("v2", "v1")),
        MatchingProfileSkill("python", "Python", ("v1",)),
    )
    signals = (_signal("sql"), _signal("python"))
    requirements = (_requirement("sql"), _requirement("python"))
    first = build_skill_fit(
        _input(skills, requirements=requirements), _signals(signals)
    )
    second = build_skill_fit(_input(tuple(reversed(skills)),
                                    requirements=tuple(reversed(requirements))),
                             _signals(tuple(reversed(signals))))
    assert first == second
    assert tuple(item.canonical_key for item in first.evaluations) == ("python", "sql")


def test_requirements_state_and_extractor_must_align_with_matching_snapshot():
    with pytest.raises(SkillFitInputError, match="absent matching requirements"):
        build_skill_fit(_input(requirements=None), _signals())
    with pytest.raises(SkillFitInputError, match="extracted matching requirements"):
        build_skill_fit(
            _input(), _signals(state=RequirementsState.UNKNOWN, extractor=None)
        )
    with pytest.raises(SkillFitInputError, match="extractor version mismatch"):
        build_skill_fit(
            _input(extractor="requirements-v3"),
            _signals(extractor="requirements-v2"),
        )


def test_persisted_requirement_and_signal_alignment_accepts_context_sources():
    persisted = (_requirement("python", "Python"),)
    basic = _signal("python", "Python")
    contextual = _signal(
        "python", "Python", sources=(
            SkillSignalSource.REQUIREMENTS,
            SkillSignalSource.TITLE,
            SkillSignalSource.TECH_STACK,
        )
    )
    assert build_skill_fit(
        _input(requirements=persisted), _signals((basic,))
    ).evaluations[0].kind is SkillSignalKind.REQUIRED
    assert build_skill_fit(
        _input(requirements=persisted), _signals((contextual,))
    ).evaluations[0].sources == contextual.sources


@pytest.mark.parametrize("signals", [
    (),
    (_signal("sql", "SQL"),),
    (_signal("python", "Python", SkillSignalKind.PREFERRED),),
    (_signal("python", "Python", sources=(SkillSignalSource.TITLE,)),),
    (_signal("python", "Python 3"),),
])
def test_misaligned_authoritative_requirement_signals_fail(signals):
    with pytest.raises(SkillFitInputError, match="requirement"):
        build_skill_fit(
            _input(requirements=(_requirement("python", "Python"),)),
            _signals(signals),
        )


def test_duplicate_persisted_requirement_key_fails_explicitly():
    duplicate = _requirement("python", "Python")
    with pytest.raises(SkillFitInputError, match="duplicate persisted requirement"):
        build_skill_fit(
            _input(requirements=(duplicate, duplicate)),
            _signals((_signal("python", "Python"),)),
        )
