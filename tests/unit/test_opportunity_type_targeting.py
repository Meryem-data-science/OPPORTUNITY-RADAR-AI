"""Unit coverage for the Phase 7B.1 type target resolver and evaluator.

Both halves are pure: they take values, return values, open no connection and
write nothing. Nothing in this file touches a database — not a temporary one,
and certainly not the operational one — so these tests can say something the
integration suite cannot: the comparison itself needs no profile, no posting
row and no schema to be correct.

Most of what is asserted below is what the two functions must **not** do:
widen a declared set to the types that look adjacent to it, read a target out
of nowhere, name PFE or INTERNSHIP themselves, turn an absence into a refusal,
or quietly accept a value outside the one shared registry.
"""

import inspect
import re
from pathlib import Path

import pytest

from services.digital_twin.preferences.models import (
    ExplicitProfileInputError,
    OpportunityType,
)
from services.targeting.opportunity_type import evaluator as evaluator_module
from services.targeting.opportunity_type import models as models_module
from services.targeting.opportunity_type import profile_target as target_module
from services.targeting.opportunity_type.evaluator import (
    MATCH_RULE,
    NO_TARGET_RULE,
    OUT_OF_TARGET_RULE,
    UNKNOWN_TYPE_RULE,
    evaluate_type_target,
)
from services.targeting.opportunity_type.models import (
    ProfileTypeTarget,
    TargetVerdict,
    TypeTargetingError,
)
from services.targeting.opportunity_type.profile_target import (
    DECLARED_TYPES_RULE,
    NO_DECLARED_TYPE_RULE,
    resolve_declared_type_target,
)

#: The set the profile this product currently serves happens to have declared.
#: It lives in the *tests*, deliberately: the point of Phase 7B.1 is that no
#: module under `services/targeting/` contains it.
TEST_ONLY_TARGET = (OpportunityType.PFE, OpportunityType.INTERNSHIP)

PURE_SOURCES = (
    Path("services/targeting/opportunity_type/evaluator.py"),
    Path("services/targeting/opportunity_type/profile_target.py"),
    Path("services/targeting/opportunity_type/models.py"),
)


def verdict_of(target, opportunity_type) -> TargetVerdict:
    return evaluate_type_target(target, opportunity_type).verdict


# --------------------------------------------------------------------------
# The evaluator: MATCH
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opportunity_type", [OpportunityType.PFE, OpportunityType.INTERNSHIP]
)
def test_a_declared_type_matches(opportunity_type) -> None:
    assessment = evaluate_type_target(TEST_ONLY_TARGET, opportunity_type)
    assert assessment.verdict is TargetVerdict.MATCH
    assert assessment.rule_id == MATCH_RULE
    assert assessment.opportunity_type is opportunity_type
    assert assessment.target_type_count == 2


def test_a_single_declared_type_still_matches() -> None:
    assert (
        verdict_of((OpportunityType.PFE,), OpportunityType.PFE) is TargetVerdict.MATCH
    )


def test_every_declared_type_of_a_wide_target_matches() -> None:
    """Several targeted types all work, and none of them shadows another."""
    target = tuple(OpportunityType)
    for member in OpportunityType:
        assert verdict_of(target, member) is TargetVerdict.MATCH


def test_the_order_of_the_declared_set_changes_nothing() -> None:
    reversed_target = tuple(reversed(TEST_ONLY_TARGET))
    for member in OpportunityType:
        assert verdict_of(TEST_ONLY_TARGET, member) is verdict_of(
            reversed_target, member
        )


# --------------------------------------------------------------------------
# The evaluator: OUT_OF_TARGET. Nothing is implicitly accepted.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "opportunity_type",
    [
        OpportunityType.JUNIOR_ROLE,
        OpportunityType.ALTERNANCE,
        OpportunityType.FIRST_JOB,
        OpportunityType.PFA,
        OpportunityType.SUMMER_INTERNSHIP,
        OpportunityType.PRE_HIRE_INTERNSHIP,
    ],
)
def test_a_known_type_outside_the_target_is_out_of_target(opportunity_type) -> None:
    """A neighbouring type is not a targeted one.

    `JUNIOR_ROLE`, `ALTERNANCE` and `FIRST_JOB` are early-career like a PFE is,
    and `SUMMER_INTERNSHIP`, `PRE_HIRE_INTERNSHIP` and `PFA` are internships
    like `INTERNSHIP` is. None of that makes any of them something this profile
    asked for: the person named a set, and the set is what they named.
    """
    assessment = evaluate_type_target(TEST_ONLY_TARGET, opportunity_type)
    assert assessment.verdict is TargetVerdict.OUT_OF_TARGET
    assert assessment.rule_id == OUT_OF_TARGET_RULE
    assert assessment.opportunity_type is opportunity_type


def test_the_generic_type_does_not_cover_the_specific_ones() -> None:
    """Targeting `INTERNSHIP` is not targeting every kind of internship."""
    target = (OpportunityType.INTERNSHIP,)
    for specific in (
        OpportunityType.PFE,
        OpportunityType.PFA,
        OpportunityType.SUMMER_INTERNSHIP,
        OpportunityType.PRE_HIRE_INTERNSHIP,
    ):
        assert verdict_of(target, specific) is TargetVerdict.OUT_OF_TARGET


def test_a_specific_type_does_not_cover_the_generic_one() -> None:
    """And targeting `PFE` is not targeting internships in general."""
    assert (
        verdict_of((OpportunityType.PFE,), OpportunityType.INTERNSHIP)
        is TargetVerdict.OUT_OF_TARGET
    )


# --------------------------------------------------------------------------
# The evaluator: UNKNOWN, which is never FALSE
# --------------------------------------------------------------------------


def test_an_unknown_opportunity_type_is_unknown_not_out_of_target() -> None:
    """A posting no closed rule could read is not a posting of another kind."""
    assessment = evaluate_type_target(TEST_ONLY_TARGET, None)
    assert assessment.verdict is TargetVerdict.UNKNOWN
    assert assessment.rule_id == UNKNOWN_TYPE_RULE
    assert assessment.opportunity_type is None


@pytest.mark.parametrize("target", [None, (), frozenset(), []])
def test_an_absent_profile_target_is_unknown(target) -> None:
    """A person who said nothing has not said they are looking for nothing."""
    for opportunity_type in (OpportunityType.PFE, OpportunityType.JUNIOR_ROLE, None):
        assessment = evaluate_type_target(target, opportunity_type)
        assert assessment.verdict is TargetVerdict.UNKNOWN
        assert assessment.rule_id == NO_TARGET_RULE
        assert assessment.target_type_count == 0


def test_no_target_is_answered_before_the_posting_is_looked_at() -> None:
    """Two absences at once report the profile's, not the posting's."""
    assert evaluate_type_target(None, None).rule_id == NO_TARGET_RULE


def test_the_three_verdicts_are_the_whole_vocabulary() -> None:
    assert {member.value for member in TargetVerdict} == {
        "MATCH",
        "OUT_OF_TARGET",
        "UNKNOWN",
    }


# --------------------------------------------------------------------------
# The evaluator refuses what it cannot compare
# --------------------------------------------------------------------------


def test_a_string_opportunity_type_is_refused_not_compared() -> None:
    with pytest.raises(TypeTargetingError):
        evaluate_type_target(TEST_ONLY_TARGET, "PFE")


def test_a_string_target_entry_is_refused_not_compared() -> None:
    with pytest.raises(TypeTargetingError):
        evaluate_type_target(("PFE",), OpportunityType.PFE)


def test_a_value_outside_the_registry_is_refused() -> None:
    with pytest.raises(TypeTargetingError):
        evaluate_type_target(TEST_ONLY_TARGET, "STAGE")


# --------------------------------------------------------------------------
# The evaluator is pure, and knows nothing about this product
# --------------------------------------------------------------------------


def test_the_evaluator_takes_the_target_set_as_an_argument() -> None:
    """The profile is the source of truth, so the target arrives as a value."""
    parameters = list(
        inspect.signature(evaluate_type_target).parameters
    )
    assert parameters == ["target_types", "opportunity_type"]


def test_the_evaluator_returns_a_new_value_and_mutates_no_input() -> None:
    target = [OpportunityType.PFE, OpportunityType.INTERNSHIP]
    before = list(target)
    evaluate_type_target(target, OpportunityType.JUNIOR_ROLE)
    assert target == before


@pytest.mark.parametrize("source", PURE_SOURCES)
def test_the_pure_modules_hardcode_no_product_decision(source) -> None:
    """No target set, no country and no posting id lives in this code.

    The docstrings do discuss `PFE`, `INTERNSHIP` and Morocco — explaining what
    the code refuses to assume is the point of them — so only executable lines
    are read here.
    """
    executable = "\n".join(
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "*"))
    )
    body = "\n".join(
        segment
        for index, segment in enumerate(executable.split('"""'))
        if index % 2 == 0
    )
    for forbidden in (
        "PFE",
        "INTERNSHIP",
        "JUNIOR_ROLE",
        "ALTERNANCE",
        "FIRST_JOB",
        # No country either: whether a posting is in Morocco is Phase 7A.1's
        # question, and no posting id is special to this one.
        "MA",
        "Maroc",
        "Morocco",
        "232",
    ):
        # Whole words: `MATCH` is a verdict, not the country code `MA`.
        assert re.search(rf"\b{forbidden}\b", body) is None, forbidden


@pytest.mark.parametrize("source", PURE_SOURCES)
def test_the_pure_modules_write_nothing_and_open_nothing(source) -> None:
    text = source.read_text(encoding="utf-8")
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "CREATE TABLE", "connect("):
        assert forbidden not in text, forbidden


def test_the_evaluator_module_imports_no_database_module() -> None:
    """A pure comparison needs neither sqlite3 nor a repository."""
    text = Path(evaluator_module.__file__).read_text(encoding="utf-8")
    assert "import sqlite3" not in text
    assert "repository" not in text


def test_the_type_vocabulary_is_the_shared_registry_not_a_new_one() -> None:
    """One registry, declared once, in the Digital Twin's preferences."""
    assert models_module.OpportunityType is OpportunityType
    assert {member.value for member in OpportunityType} == {
        "PFA",
        "PFE",
        "SUMMER_INTERNSHIP",
        "PRE_HIRE_INTERNSHIP",
        "ALTERNANCE",
        "INTERNSHIP",
        "FIRST_JOB",
        "JUNIOR_ROLE",
    }


# --------------------------------------------------------------------------
# The profile target resolver, over declared values
# --------------------------------------------------------------------------


def test_a_declared_set_becomes_the_target() -> None:
    target = resolve_declared_type_target(7, list(TEST_ONLY_TARGET))
    assert target.known is True
    assert target.opportunity_types == (
        OpportunityType.PFE,
        OpportunityType.INTERNSHIP,
    )
    assert target.count == 2
    assert target.rule_id == DECLARED_TYPES_RULE
    assert target.profile_id == 7


def test_a_declared_set_is_ordered_canonically_and_deduplicated() -> None:
    """The registry's own declaration order, so one set has one shape."""
    target = resolve_declared_type_target(
        7, ["INTERNSHIP", "PFE", "PFE", OpportunityType.INTERNSHIP]
    )
    assert target.opportunity_types == (
        OpportunityType.PFE,
        OpportunityType.INTERNSHIP,
    )


@pytest.mark.parametrize("declared", [None, (), []])
def test_a_declared_set_naming_nothing_is_an_unknown_target(declared) -> None:
    target = resolve_declared_type_target(7, declared)
    assert target.known is False
    assert target.opportunity_types == ()
    assert target.count == 0
    assert target.rule_id == NO_DECLARED_TYPE_RULE


def test_a_declared_value_outside_the_registry_is_refused_not_dropped() -> None:
    """Dropping it would narrow a target the person wrote wider."""
    with pytest.raises(ExplicitProfileInputError):
        resolve_declared_type_target(7, ["PFE", "STAGE"])


def test_the_resolver_reads_only_what_it_was_handed() -> None:
    """No CV, no skill, no career objective, no availability, no mobility."""
    text = Path(target_module.__file__).read_text(encoding="utf-8")
    body = "\n".join(
        segment for index, segment in enumerate(text.split('"""')) if index % 2 == 0
    )
    for forbidden in (
        "get_profile_mobility",
        "get_profile_availability",
        "get_profile_career_objectives",
        "profile_skills",
        "cv",
    ):
        assert forbidden not in body, forbidden


# --------------------------------------------------------------------------
# The target value object
# --------------------------------------------------------------------------


def test_a_target_holds_registry_members_only() -> None:
    with pytest.raises(TypeTargetingError):
        ProfileTypeTarget(profile_id=1, opportunity_types=("PFE",), rule_id="r-v1")


def test_a_target_names_each_type_once() -> None:
    with pytest.raises(TypeTargetingError):
        ProfileTypeTarget(
            profile_id=1,
            opportunity_types=(OpportunityType.PFE, OpportunityType.PFE),
            rule_id="r-v1",
        )


def test_a_target_names_one_trimmed_rule() -> None:
    with pytest.raises(TypeTargetingError):
        ProfileTypeTarget(profile_id=1, opportunity_types=(), rule_id=" ")


def test_an_assessment_renders_an_unknown_type_as_unknown() -> None:
    rendered = evaluate_type_target(TEST_ONLY_TARGET, None).as_dict()
    assert rendered["opportunity_type"] == "UNKNOWN"
    assert rendered["verdict"] == "UNKNOWN"
