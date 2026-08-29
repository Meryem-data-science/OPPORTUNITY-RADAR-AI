"""Unit coverage for the Phase 3.4B1 structuring rules.

Every value below is synthetic: invented roles, invented organizations,
invented project titles. No real CV, no real address and no real personal data
takes part, and nothing here opens a database or a file.

Most of these tests are about what the rules refuse to do. A rule applies or it
does not; there is no partial credit, no scoring and no "probably". Where the
wording is not unambiguous the fragment stays `None` and the reading records
`UNPARSED_V1`, because a `NULL` is worth more than an invented value.
"""

import io
import tokenize
from pathlib import Path

import pytest

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    StructuringRule,
)
from services.digital_twin.structured_profile.structurer import (
    is_explicit_period,
    structure_experience,
    structure_project,
)

STRUCTURER_SOURCE = Path("services/digital_twin/structured_profile/structurer.py")


def code_only(path: Path) -> str:
    """Return a module's executable source, without comments or docstrings."""
    tokens = tokenize.generate_tokens(
        io.StringIO(path.read_text(encoding="utf-8")).readline
    )
    return "".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


# --------------------------------------------------------------------------
# What counts as an explicit period
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment",
    [
        "2024",
        "2022 - 2024",
        "2022-2024",
        "2022 – 2024",
        "2022 — 2024",
        "2023/2024",
        "09/2024",
        "09/2024 - 06/2025",
        "Sept. 2024",
        "septembre 2024 - juin 2025",
        "January 2024",
        "2023 - présent",
        "janvier 2024 — aujourd'hui",
    ],
)
def test_a_closed_explicit_form_is_a_period(fragment: str) -> None:
    assert is_explicit_period(fragment)


@pytest.mark.parametrize(
    "fragment",
    [
        "",
        "Paris",
        "Stage",
        "Data Analyst",
        "depuis 2023",
        "2 ans",
        "6 mois",
        "2022 - 2024 (6 mois)",
        "printemps 2024",
        "septembre",
        "24",
        "l'été 2024",
        "2024 et 2025",
    ],
)
def test_anything_else_is_not_a_period(fragment: str) -> None:
    """A year buried in prose is prose. The rule reads whole fragments only."""
    assert not is_explicit_period(fragment)


# --------------------------------------------------------------------------
# EXPERIENCE: the pipe header rule
# --------------------------------------------------------------------------


def test_an_explicit_pipe_header_is_structured_segment_by_segment() -> None:
    reading = structure_experience("Data Analyst | ACME | 2022 - 2024")

    assert reading.structuring_rule_id is StructuringRule.EXPERIENCE_PIPE_HEADER_V1
    assert reading.role_text == "Data Analyst"
    assert reading.organization_text == "ACME"
    assert reading.period_text == "2022 - 2024"
    assert reading.description_text is None
    assert reading.structurer_version == STRUCTURED_PROFILE_VERSION


def test_the_segments_are_trimmed_and_otherwise_untouched() -> None:
    reading = structure_experience("  Data Analyst  |   ACME SARL |  2024  ")

    assert reading.role_text == "Data Analyst"
    assert reading.organization_text == "ACME SARL"
    assert reading.period_text == "2024"


def test_a_multi_line_description_is_kept_whole_and_in_order() -> None:
    reading = structure_experience(
        "Data Analyst | ACME | 2022 - 2024\n"
        "Première ligne de détail\n"
        "Deuxième ligne de détail"
    )

    assert reading.description_text == (
        "Première ligne de détail\nDeuxième ligne de détail"
    )


def test_a_blank_line_inside_the_description_is_kept_where_it_was() -> None:
    reading = structure_experience(
        "Data Analyst | ACME | 2024\nPremière\n\nDeuxième\n"
    )

    assert reading.description_text == "Première\n\nDeuxième"


def test_a_fourth_segment_the_rule_does_not_name_is_simply_not_projected() -> None:
    """A city is not a role, an organization or a period, so it is not stored.

    The fact keeps the whole line; this slice adds no column for it.
    """
    reading = structure_experience("Data Analyst | ACME | Paris | 2022 - 2024")

    assert reading.structuring_rule_id is StructuringRule.EXPERIENCE_PIPE_HEADER_V1
    assert reading.role_text == "Data Analyst"
    assert reading.organization_text == "ACME"
    assert reading.period_text == "2022 - 2024"


def test_a_header_with_no_explicit_period_is_not_structured() -> None:
    reading = structure_experience("Data Analyst | ACME | Paris")

    assert reading.structuring_rule_id is StructuringRule.UNPARSED_V1
    assert reading.role_text is None
    assert reading.organization_text is None
    assert reading.period_text is None
    assert reading.description_text is None


def test_an_ambiguous_header_with_two_periods_is_not_structured() -> None:
    """Two dates and no rule to choose between them: choosing is inventing."""
    reading = structure_experience("Data Analyst | ACME | 2022 | 2024")

    assert reading.structuring_rule_id is StructuringRule.UNPARSED_V1
    assert reading.period_text is None


def test_a_date_written_where_the_role_belongs_is_not_structured() -> None:
    reading = structure_experience("2022 - 2024 | Data Analyst | ACME")

    assert reading.structuring_rule_id is StructuringRule.UNPARSED_V1


@pytest.mark.parametrize(
    "value",
    [
        "Data Analyst chez ACME depuis 2022",
        "Data Analyst | ACME",
        "Data Analyst |  | 2024",
        "Data Analyst | ACME | ",
        "• Data Analyst | ACME | 2024",
        "",
    ],
)
def test_free_or_incomplete_structure_falls_back_with_every_field_null(
    value: str,
) -> None:
    reading = structure_experience(value)

    assert reading.structuring_rule_id is StructuringRule.UNPARSED_V1
    assert reading.role_text is None
    assert reading.organization_text is None
    assert reading.period_text is None
    assert reading.description_text is None
    assert reading.source_value == value


def test_no_seniority_no_duration_and_no_level_is_ever_derived() -> None:
    """"Stage" stays the word the CV wrote, and nothing is computed from it."""
    reading = structure_experience(
        "Stage Data Analyst | ACME | 2022 - 2024\nPython, SQL, Power BI"
    )

    assert reading.role_text == "Stage Data Analyst"
    assert reading.period_text == "2022 - 2024"
    for forbidden in ("seniority", "level", "duration", "months", "years", "skills"):
        assert not hasattr(reading, forbidden), forbidden


def test_an_employer_is_never_deduced_from_a_sentence() -> None:
    reading = structure_experience(
        "Analyste de données, mission réalisée pour ACME entre 2022 et 2024"
    )

    assert reading.organization_text is None
    assert reading.role_text is None


def test_a_school_year_is_never_converted_into_calendar_dates() -> None:
    reading = structure_experience("Alternante | ACME | 2023/2024")

    assert reading.period_text == "2023/2024"


# --------------------------------------------------------------------------
# PROJECT: the bullet-and-colon rule
# --------------------------------------------------------------------------


def test_a_bullet_and_a_colon_give_a_title_and_a_description() -> None:
    reading = structure_project("• Analyse RH : segmentation des effectifs")

    assert reading.structuring_rule_id is StructuringRule.PROJECT_BULLET_COLON_V1
    assert reading.title_text == "Analyse RH"
    assert reading.description_text == "segmentation des effectifs"
    assert reading.period_text is None


def test_the_colon_rule_works_without_any_bullet() -> None:
    reading = structure_project("Analyse RH : segmentation des effectifs")

    assert reading.title_text == "Analyse RH"
    assert reading.description_text == "segmentation des effectifs"


@pytest.mark.parametrize("marker", ["-", "–", "—", "*", "•", "·", "▪", ">"])
def test_exactly_one_recognised_marker_is_removed(marker: str) -> None:
    reading = structure_project(f"{marker} Analyse RH : segmentation")

    assert reading.title_text == "Analyse RH"


def test_a_second_marker_belongs_to_the_title() -> None:
    """At most one marker is removed; the rest is text the document wrote."""
    reading = structure_project("• • Analyse RH : segmentation")

    assert reading.title_text == "• Analyse RH"


def test_a_final_parenthesised_year_range_becomes_the_period() -> None:
    reading = structure_project("• Analyse RH (2023 - 2024) : segmentation")

    assert reading.title_text == "Analyse RH"
    assert reading.period_text == "2023 - 2024"
    assert reading.description_text == "segmentation"


@pytest.mark.parametrize(
    "title",
    [
        "Analyse RH (2024)",
        "Analyse RH (promotion 2024)",
        "Analyse RH (v2)",
        "Analyse RH (2023 - 2024) suite",
        "Analyse RH 2023 - 2024",
        "Analyse RH (septembre 2024 - juin 2025)",
    ],
)
def test_anything_but_a_closed_final_year_range_stays_in_the_title(
    title: str,
) -> None:
    """A lone year in a title may be a version, an edition or a cohort."""
    reading = structure_project(f"{title} : description")

    assert reading.title_text == title
    assert reading.period_text is None


def test_a_project_description_keeps_its_following_lines() -> None:
    reading = structure_project(
        "• Analyse RH : segmentation des effectifs\nPython et Power BI\nRapport final"
    )

    assert reading.description_text == (
        "segmentation des effectifs\nPython et Power BI\nRapport final"
    )


def test_a_colon_inside_a_scheme_is_not_a_separator() -> None:
    reading = structure_project("• https://example.invalid/rh : dépôt du projet")

    assert reading.title_text == "https://example.invalid/rh"
    assert reading.description_text == "dépôt du projet"


def test_only_the_first_usable_colon_separates() -> None:
    reading = structure_project("Analyse RH : objectif : réduire le turnover")

    assert reading.title_text == "Analyse RH"
    assert reading.description_text == "objectif : réduire le turnover"


@pytest.mark.parametrize(
    "value",
    [
        "Analyse RH des effectifs sur deux ans",
        "• Analyse RH",
        "• : segmentation",
        "Analyse RH :",
        "  :  ",
        "https://example.invalid/rh",
        "",
    ],
)
def test_an_ambiguous_or_empty_side_falls_back_with_every_field_null(
    value: str,
) -> None:
    reading = structure_project(value)

    assert reading.structuring_rule_id is StructuringRule.UNPARSED_V1
    assert reading.title_text is None
    assert reading.period_text is None
    assert reading.description_text is None
    assert reading.source_value == value


def test_no_skill_is_ever_read_out_of_a_project() -> None:
    """A description names technologies; this slice derives no skill from it."""
    reading = structure_project("• Analyse RH : réalisée en Python, SQL et Power BI")

    assert reading.description_text == "réalisée en Python, SQL et Power BI"
    for forbidden in ("skills", "technologies", "level", "score"):
        assert not hasattr(reading, forbidden), forbidden


# --------------------------------------------------------------------------
# Determinism, and the tools the rules refuse to use
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Data Analyst | ACME | 2022 - 2024\nDétail",
        "Prose libre",
        "Alternante | ACME | 2023/2024",
    ],
)
def test_structuring_the_same_value_twice_gives_the_same_reading(value: str) -> None:
    assert structure_experience(value) == structure_experience(value)


def test_the_structurer_needs_no_network_no_model_and_no_clock() -> None:
    lowered = code_only(STRUCTURER_SOURCE).casefold()

    for forbidden in (
        "http",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "openai",
        "anthropic",
        "sqlite3",
        "datetime",
        "time.",
        "random",
        "difflib",
        "levenshtein",
        "fuzz",
        "embedding",
        "sklearn",
        "numpy",
    ):
        assert forbidden not in lowered, forbidden


def test_the_structurer_imports_no_cv_module() -> None:
    """A fact reaches this package through a human decision, never a parser."""
    source = STRUCTURER_SOURCE.read_text(encoding="utf-8")

    assert "digital_twin.cv" not in source
    assert "candidates" not in source
