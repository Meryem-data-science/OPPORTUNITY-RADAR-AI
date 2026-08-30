"""The sentences beside the reason codes: complete, deterministic, and quiet.

An explanation is a courtesy for a human reader and the reason code is the
business truth. These tests keep the courtesy honest — every code has a
sentence, every sentence renders, the same inputs give the same text, and no
sentence carries a posting's words or a person's.
"""

from __future__ import annotations

import pytest

from services.eligibility.comparison import (
    CEFR_LEVELS,
    compare_cefr,
    compare_education_levels,
    parse_cefr,
)
from services.eligibility.explanations import (
    EXPLANATION_TEMPLATES,
    MAX_EXPLANATION_LENGTH,
    ExplanationError,
    explain,
)
from services.eligibility.models import ReasonCode


def test_every_reason_code_has_a_sentence():
    assert set(EXPLANATION_TEMPLATES) == set(ReasonCode)


def test_a_missing_detail_fails_loudly_rather_than_rendering_a_hole():
    with pytest.raises(ExplanationError):
        explain(ReasonCode.LANGUAGE_REQUIRED_SATISFIED)


def test_rendering_is_deterministic():
    first = explain(ReasonCode.LANGUAGE_PROFILE_UNKNOWN, language="english", level="B2")
    second = explain(
        ReasonCode.LANGUAGE_PROFILE_UNKNOWN, language="english", level="B2"
    )
    assert first == second
    assert "english" in first and "B2" in first


def test_an_absent_detail_renders_as_words_rather_than_as_none():
    rendered = explain(
        ReasonCode.LANGUAGE_PROFILE_UNKNOWN, language="french", level=None
    )
    assert "None" not in rendered


def test_a_long_detail_is_truncated_rather_than_failing_an_insert():
    """A correct verdict must never be unstorable because a sentence was long."""
    rendered = explain(
        ReasonCode.EDUCATION_PROFILE_UNKNOWN, accepted="X" * 1000
    )
    assert len(rendered) <= MAX_EXPLANATION_LENGTH


# --------------------------------------------------------------------------
# The two comparisons, and their refusals
# --------------------------------------------------------------------------


@pytest.mark.parametrize("level", CEFR_LEVELS)
def test_a_cefr_token_is_read_as_itself(level):
    assert parse_cefr(level) == level
    assert parse_cefr(level.lower()) == level
    assert parse_cefr(f"  {level} ") == level


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "fluent",
        "courant",
        "native",
        "bilingue",
        "avancé",
        "good command",
        "B2/C1",
        "at least B2",
        "B2+",
        "D1",
        "B3",
    ],
)
def test_anything_that_is_not_a_cefr_token_is_refused(text):
    """No mapping from a word to a level exists in this project, by design."""
    assert parse_cefr(text) is None


def test_the_cefr_scale_is_ordered():
    for lower, higher in zip(CEFR_LEVELS, CEFR_LEVELS[1:]):
        assert compare_cefr(higher, lower) is True
        assert compare_cefr(lower, higher) is False
        assert compare_cefr(lower, lower) is True


def test_comparing_against_a_non_cefr_value_answers_nothing():
    assert compare_cefr("fluent", "B2") is None
    assert compare_cefr("C1", "fluent") is None


def test_the_bac_plus_ladder_is_ordered():
    assert compare_education_levels("BAC_PLUS_5", "BAC_PLUS_3", minimum=True) is True
    assert compare_education_levels("BAC_PLUS_2", "BAC_PLUS_3", minimum=True) is False


def test_the_degree_ladder_is_ordered():
    assert compare_education_levels("PHD", "BACHELOR", minimum=True) is True
    assert compare_education_levels("BACHELOR", "MASTER", minimum=True) is False


def test_the_two_ladders_are_never_compared_to_each_other():
    """`0012` says BAC_PLUS_5 and MASTER are separate levels on purpose."""
    assert compare_education_levels("MASTER", "BAC_PLUS_5", minimum=True) is None
    assert compare_education_levels("BAC_PLUS_5", "MASTER", minimum=True) is None
    assert compare_education_levels("BAC_PLUS_5", "MASTER", minimum=False) is None


def test_an_engineering_degree_is_comparable_only_to_itself():
    assert (
        compare_education_levels("ENGINEERING_DEGREE", "ENGINEERING_DEGREE", minimum=True)
        is True
    )
    assert compare_education_levels("ENGINEERING_DEGREE", "MASTER", minimum=True) is None
    assert compare_education_levels("MASTER", "ENGINEERING_DEGREE", minimum=True) is None


def test_an_exact_demand_is_met_by_that_level_and_no_other():
    assert compare_education_levels("MASTER", "MASTER", minimum=False) is True
    assert compare_education_levels("PHD", "MASTER", minimum=False) is False
    assert compare_education_levels("BACHELOR", "MASTER", minimum=False) is False
