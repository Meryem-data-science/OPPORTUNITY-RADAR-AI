"""Unit coverage for the conservative CV text normalization."""

from services.digital_twin.cv.normalization import (
    normalize_line,
    normalize_lines,
    normalize_text,
)


def test_every_line_ending_convention_becomes_one_newline() -> None:
    assert normalize_text("a\r\nb\rc d e\fg\vh") == "a\nb\nc\nd\ne\ng\nh"


def test_pdf_space_artefacts_collapse_without_touching_the_words() -> None:
    raw = " Data  \tScience​   Engineer  "

    assert normalize_line(raw) == "Data Science Engineer"


def test_line_separation_is_preserved_and_blank_runs_collapse_to_one() -> None:
    raw = "COMPETENCES\n\n\n\nPython\nSQL\n\n\nLANGUES"

    assert normalize_lines(raw) == (
        "COMPETENCES",
        "",
        "Python",
        "SQL",
        "",
        "LANGUES",
    )


def test_leading_and_trailing_blank_lines_are_dropped() -> None:
    assert normalize_text("\n \n\nProfil\n\n \n") == "Profil"


def test_nothing_is_reworded_translated_or_dropped() -> None:
    raw = "Stage de fin d'etudes - analyste donnees (2024-2025)\nPython, SQL & dbt"

    assert normalize_text(raw) == raw


def test_combining_and_precomposed_accents_become_the_same_string() -> None:
    combining = "EXPÉRIENCE"
    precomposed = "EXPÉRIENCE"

    assert normalize_text(combining) == normalize_text(precomposed) == precomposed


def test_a_block_with_no_text_normalizes_to_nothing() -> None:
    assert normalize_text("  \n\t\n \n") == ""
    assert normalize_lines("   ") == ()


def test_normalization_is_idempotent() -> None:
    raw = "  Jane  Doe \n\n\n PROFIL :\n\tEtudiante  \n"
    once = normalize_text(raw)

    assert normalize_text(once) == once
