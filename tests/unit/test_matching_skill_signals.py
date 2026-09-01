from dataclasses import FrozenInstanceError

import pytest

from services.collector.matching.models import (
    MatchingOpportunityInput,
    MatchingRequiredSkill,
    MatchingRequirements,
)
from services.collector.matching.skill_signals import (
    SKILL_SIGNAL_VERSION,
    RequirementsState,
    SkillSignalKind,
    SkillSignalSource,
    build_opportunity_skill_signals,
)


def opportunity(title="Data role", description=None, skills=None, extracted=True):
    requirements = None
    if extracted:
        requirements = MatchingRequirements("requirements-v-test", tuple(skills or ()))
    return MatchingOpportunityInput(7, title, description, None, requirements=requirements)


def by_key(result):
    return {signal.canonical_key: signal for signal in result.signals}


def test_version_and_frozen_results():
    result = build_opportunity_skill_signals(opportunity())
    assert result.skill_signal_version == SKILL_SIGNAL_VERSION == "skill-signals-v1"
    with pytest.raises(FrozenInstanceError):
        result.opportunity_id = 9


def test_persisted_required_and_preferred_are_authoritative():
    result = build_opportunity_skill_signals(opportunity(skills=(
        MatchingRequiredSkill("python", "Python", "REQUIRED"),
        MatchingRequiredSkill("sql", "SQL", "PREFERRED"),
    )))
    assert by_key(result)["python"].kind is SkillSignalKind.REQUIRED
    assert by_key(result)["sql"].kind is SkillSignalKind.PREFERRED
    assert all(s.sources == (SkillSignalSource.REQUIREMENTS,) for s in result.signals)


def test_unknown_allows_title_but_not_description():
    result = build_opportunity_skill_signals(
        opportunity("SQL engineer", "Tech Stack\nPython", extracted=False)
    )
    assert result.requirements_state is RequirementsState.UNKNOWN
    assert result.requirements_extractor_version is None
    assert tuple(by_key(result)) == ("sql",)


def test_extracted_empty_allows_closed_context_sections():
    result = build_opportunity_skill_signals(opportunity(description=(
        "About the role\nWe build with Python.\n"
        "Responsibilities\nOperate Kafka.\nTech Stack:\nSQL and Airflow."
    )))
    assert result.requirements_state is RequirementsState.EXTRACTED
    assert {s.kind for s in result.signals} == {SkillSignalKind.CONTEXT}
    assert {s.sources[0] for s in result.signals} == {
        SkillSignalSource.ROLE_DESCRIPTION,
        SkillSignalSource.RESPONSIBILITIES,
        SkillSignalSource.TECH_STACK,
    }


@pytest.mark.parametrize("description", [
    "About the company\nWe use Python.\nBenefits\nSQL training.",
    "We use Python and SQL every day.",
    "Required Qualifications\nPython\nPreferred Skills\nSQL",
])
def test_unapproved_or_requirement_sections_do_not_create_context(description):
    assert build_opportunity_skill_signals(opportunity(description=description)).signals == ()


@pytest.mark.parametrize("sentence", [
    "No prior Python experience is required.",
    "Training in Python will be provided.",
    "You will learn Python.",
])
def test_cancellations_do_not_create_context(sentence):
    result = build_opportunity_skill_signals(opportunity(description=f"Responsibilities\n{sentence}"))
    assert "python" not in by_key(result)


def test_clause_cancellation_does_not_hide_another_skill():
    result = build_opportunity_skill_signals(opportunity(
        description="Responsibilities\nNo Python experience required, but SQL is used daily."
    ))
    assert tuple(by_key(result)) == ("sql",)


def test_priority_multiple_sources_and_deterministic_order():
    result = build_opportunity_skill_signals(opportunity(
        "Python SQL engineer", "Tech Stack\nPython and Airflow.",
        (MatchingRequiredSkill("python", "Python", "REQUIRED"),
         MatchingRequiredSkill("sql", "SQL", "PREFERRED")),
    ))
    signals = by_key(result)
    assert signals["python"].kind is SkillSignalKind.REQUIRED
    assert signals["python"].sources == (
        SkillSignalSource.REQUIREMENTS, SkillSignalSource.TITLE, SkillSignalSource.TECH_STACK
    )
    assert signals["sql"].kind is SkillSignalKind.PREFERRED
    assert tuple(s.canonical_key for s in result.signals) == tuple(sorted(signals))


def test_semantically_unordered_requirements_produce_same_result():
    skills = (
        MatchingRequiredSkill("sql", "SQL", "PREFERRED"),
        MatchingRequiredSkill("python", "Python", "REQUIRED"),
    )
    assert build_opportunity_skill_signals(opportunity(skills=skills)) == build_opportunity_skill_signals(
        opportunity(skills=tuple(reversed(skills)))
    )


def test_existing_matcher_guards_and_contextual_alternatives():
    result = build_opportunity_skill_signals(opportunity(
        description="Tech Stack\nGoogle, PySpark and Régression. TensorFlow or PyTorch."
    ))
    assert set(by_key(result)) == {"pyspark", "tensorflow", "pytorch"}
    assert {"go", "apache spark", "r"}.isdisjoint(by_key(result))


def test_unknown_persisted_requirement_fails_explicitly():
    with pytest.raises(ValueError, match="MAYBE"):
        build_opportunity_skill_signals(opportunity(skills=(
            MatchingRequiredSkill("python", "Python", "MAYBE"),
        )))
