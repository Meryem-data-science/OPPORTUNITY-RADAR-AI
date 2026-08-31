"""Unit coverage for reconstructing a wrapped entry from the page layout.

Every document below is invented and built in memory from `PlacedText` values
that state where each line physically sits. TEST ONLY: no real CV, person,
institution, employer, diploma or contact detail appears here, in the fixtures
or in the git history, and no PDF is read from disk.

The fixtures describe *layouts*, not meanings. That is the whole point: the rule
under test is allowed to know that two lines sit on adjacent baselines in one
column, and is not allowed to know what the words on them are. Several tests
below exist only to hold it to that.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from services.digital_twin.cv.candidates import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    ExtractionRule,
    StructuredCvExtraction,
    extract_candidates,
)
from services.digital_twin.cv.candidates.layout import (
    MIN_GAP_EVIDENCE_PAIRS,
    read_layout_evidence,
)
from services.digital_twin.cv.models import PARSER_VERSION, SectionType
from services.digital_twin.cv.parser import parse_cv_bytes
from services.digital_twin.cv.sections import document_lines
from tests.unit.conftest import PlacedText, build_synthetic_pdf

#: The geometry every fixture is written in, in PDF points. A body line and its
#: continuation sit `TIGHT_LEADING` apart; two separate blocks sit
#: `BLOCK_SPACING` apart. Headings are set larger, which is what keeps a
#: heading-to-body step from being compared with a body-to-body one.
LEFT_MARGIN = 72.0
BODY_SIZE = 11.0
HEADING_SIZE = 13.0
TIGHT_LEADING = 14.0
BLOCK_SPACING = 26.0
TOP = 760.0

# TEST ONLY content. Every line below is invented and describes nobody.
HEADING_PROFILE = "PROFIL"
HEADING_EDUCATION = "FORMATION"
HEADING_EXPERIENCE = "EXPERIENCE PROFESSIONNELLE"
HEADING_PROJECTS = "PROJETS"


def laid_out(
    rows: Sequence[tuple[str, float, float]],
    *,
    top: float = TOP,
    x: float = LEFT_MARGIN,
) -> list[PlacedText]:
    """Turn `(text, font size, step down from the line above)` into a page.

    The first row's step is ignored: it sits at `top`. Everything else is placed
    by walking down the page, which is how the tests describe a layout without
    ever writing an absolute coordinate.
    """
    placed: list[PlacedText] = []
    y = top
    for index, (text, size, step) in enumerate(rows):
        if index:
            y -= step
        placed.append(PlacedText(text=text, x=x, y=y, font_size=size))
    return placed


#: The part every fixture opens with: a header and a profile paragraph, set in
#: the body column. It gives the document the repeated tight step and the
#: longest line of its column, so the document describes its own spacing and its
#: own right-hand boundary rather than a constant standing in for either.
PREAMBLE: list[tuple[str, float, float]] = [
    ("Prenom Exemple", HEADING_SIZE, 0.0),
    ("Data Analyst", BODY_SIZE, 20.0),
    ("contact@example.invalid", BODY_SIZE, TIGHT_LEADING),
    (HEADING_PROFILE, HEADING_SIZE, BLOCK_SPACING),
    ("Example profile sentence written to reach the right", BODY_SIZE, 18.0),
    ("edge of this invented column.", BODY_SIZE, TIGHT_LEADING),
]

#: One EDUCATION entry the layout engine broke across two baselines: the first
#: line runs to the column's boundary, the second carries the rest.
WRAPPED_EDUCATION: list[tuple[str, float, float]] = [
    (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
    ("Example Institute | Example Degree in Distributed", BODY_SIZE, 18.0),
    ("Systems and Analytics | Expected 2099", BODY_SIZE, TIGHT_LEADING),
]

#: Two EDUCATION entries, each short of the boundary and separated by the
#: spacing the document puts between blocks. No blank line and no list marker:
#: exactly the shape that used to become one entry per line.
TWO_EDUCATION_ENTRIES: list[tuple[str, float, float]] = [
    (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
    ("Example Institute | First Example Degree | 2098", BODY_SIZE, 18.0),
    ("Other Institute | Second Example Degree | 2099", BODY_SIZE, BLOCK_SPACING),
]

#: What the document needs so that its own spacings are distinguishable: a step
#: clearly looser than the tightest one, between two lines of the same column.
TRAILING_BLOCK_SPACED_PAIR: list[tuple[str, float, float]] = [
    (HEADING_PROJECTS, HEADING_SIZE, BLOCK_SPACING),
    ("Example Project One | Invented | 2097", BODY_SIZE, 18.0),
    ("Example Project Two | Invented | 2096", BODY_SIZE, BLOCK_SPACING),
]


def extract_of(*pages: Sequence[PlacedText]) -> StructuredCvExtraction:
    """Build, parse and extract one synthetic document."""
    return extract_candidates(parse_cv_bytes(build_synthetic_pdf(list(pages))))


def entries(
    result: StructuredCvExtraction, candidate_type: CandidateType
) -> list[str]:
    return [candidate.raw_text for candidate in result.of_type(candidate_type)]


def rules_of(
    result: StructuredCvExtraction, candidate_type: CandidateType
) -> set[str]:
    return {
        candidate.rule_id.value for candidate in result.of_type(candidate_type)
    }


# --------------------------------------------------------------------------
# A. A real visual continuation becomes one entry
# --------------------------------------------------------------------------


def test_a_wrapped_education_entry_is_one_entry_with_its_line_break_kept() -> None:
    result = extract_of(
        laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    )

    education = entries(result, CandidateType.EDUCATION_ENTRY)

    assert education == [
        "Example Institute | Example Degree in Distributed\n"
        "Systems and Analytics | Expected 2099"
    ]
    assert rules_of(result, CandidateType.EDUCATION_ENTRY) == {
        ExtractionRule.SECTION_LAYOUT_CONTINUATION_BLOCK.value
    }


def test_the_wrapped_entry_keeps_the_source_text_of_both_lines_verbatim() -> None:
    result = extract_of(
        laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    )

    (entry,) = result.of_type(CandidateType.EDUCATION_ENTRY)

    # Both lines survive exactly as the document wrote them, and the break the
    # layout engine inserted is still visible in the text a human will review.
    assert entry.raw_text.split("\n") == [
        "Example Institute | Example Degree in Distributed",
        "Systems and Analytics | Expected 2099",
    ]
    assert entry.normalized_value is None


# --------------------------------------------------------------------------
# B. Two real entries stay two
# --------------------------------------------------------------------------


def test_two_education_entries_without_a_blank_line_stay_two_entries() -> None:
    result = extract_of(
        laid_out(PREAMBLE + TWO_EDUCATION_ENTRIES + TRAILING_BLOCK_SPACED_PAIR)
    )

    education = entries(result, CandidateType.EDUCATION_ENTRY)

    assert education == [
        "Example Institute | First Example Degree | 2098",
        "Other Institute | Second Example Degree | 2099",
    ]
    assert rules_of(result, CandidateType.EDUCATION_ENTRY) == {
        ExtractionRule.SECTION_LINE_BLOCK.value
    }


def test_two_entries_on_adjacent_baselines_stay_two_when_neither_is_full() -> None:
    """The tight step alone never merges: the first line must also be full.

    Here the two entries sit exactly `TIGHT_LEADING` apart — the step a
    continuation gets — and both stop well short of the column's boundary. A
    rule reading the step alone would join them; reading whether the first line
    was broken by wrapping keeps them apart.
    """
    compact = [
        (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
        ("Example Institute | 2098", BODY_SIZE, 18.0),
        ("Other Institute | 2099", BODY_SIZE, TIGHT_LEADING),
    ]

    result = extract_of(laid_out(PREAMBLE + compact + TRAILING_BLOCK_SPACED_PAIR))

    assert entries(result, CandidateType.EDUCATION_ENTRY) == [
        "Example Institute | 2098",
        "Other Institute | 2099",
    ]


# --------------------------------------------------------------------------
# C. Ambiguous layout falls back instead of guessing
# --------------------------------------------------------------------------


def test_an_evenly_spaced_document_proves_nothing_and_nothing_is_merged() -> None:
    """Every step identical: "next line" and "next entry" look alike, so no merge.

    This is also the shape of every document built from evenly spaced lines,
    which is what the suites written before this rule use.
    """
    even = [
        (HEADING_PROFILE, BODY_SIZE, 0.0),
        (
            "Example profile sentence written to reach the right",
            BODY_SIZE,
            TIGHT_LEADING,
        ),
        ("edge of this invented column.", BODY_SIZE, TIGHT_LEADING),
        (HEADING_EDUCATION, BODY_SIZE, TIGHT_LEADING),
        (
            "Example Institute | Example Degree in Distributed",
            BODY_SIZE,
            TIGHT_LEADING,
        ),
        ("Systems and Analytics | Expected 2099", BODY_SIZE, TIGHT_LEADING),
    ]

    result = extract_of(laid_out(even))

    assert entries(result, CandidateType.EDUCATION_ENTRY) == [
        "Example Institute | Example Degree in Distributed",
        "Systems and Analytics | Expected 2099",
    ]
    assert rules_of(result, CandidateType.EDUCATION_ENTRY) == {
        ExtractionRule.SECTION_LINE_BLOCK.value
    }


def test_a_document_with_too_few_comparable_steps_proves_nothing() -> None:
    short = [
        (HEADING_EDUCATION, HEADING_SIZE, 0.0),
        ("Example Institute | Example Degree in Distributed", BODY_SIZE, 18.0),
        ("Systems and Analytics | Expected 2099", BODY_SIZE, TIGHT_LEADING),
    ]
    lines = document_lines(parse_cv_bytes(build_synthetic_pdf([laid_out(short)])).pages)

    evidence = read_layout_evidence(lines)

    assert not evidence.distinguishes_spacings
    assert evidence.tight_gap is None
    result = extract_of(laid_out(short))
    assert len(entries(result, CandidateType.EDUCATION_ENTRY)) == 2


def test_a_parsed_cv_carrying_no_layout_at_all_falls_back_conservatively() -> None:
    """A page built from text alone has no geometry, so nothing is merged."""
    from services.digital_twin.cv.models import ExtractedPage, ParsedCv
    from services.digital_twin.cv.sections import detect_sections

    text = (
        f"{HEADING_EDUCATION}\n"
        "Example Institute | Example Degree in Distributed\n"
        "Systems and Analytics | Expected 2099"
    )
    pages = (ExtractedPage(page_number=1, text=text),)
    sections, warnings = detect_sections(pages)
    parsed = ParsedCv(
        parser_version=PARSER_VERSION,
        content_sha256="0" * 64,
        page_count=1,
        pages=pages,
        sections=sections,
        warnings=warnings,
    )

    result = extract_candidates(parsed)

    assert not pages[0].has_layout
    assert len(entries(result, CandidateType.EDUCATION_ENTRY)) == 2
    assert rules_of(result, CandidateType.EDUCATION_ENTRY) == {
        ExtractionRule.SECTION_LINE_BLOCK.value
    }


def test_a_line_starting_at_another_left_edge_is_not_read_as_a_continuation() -> None:
    """An indented line is not merged: this rule only knows the plain margin."""
    page = laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    indented = [
        PlacedText(
            text=line.text,
            x=line.x + 12.0 if line.text.startswith("Systems and") else line.x,
            y=line.y,
            font_size=line.font_size,
        )
        for line in page
    ]

    result = extract_of(indented)

    assert len(entries(result, CandidateType.EDUCATION_ENTRY)) == 2


# --------------------------------------------------------------------------
# D. More than one continuation line
# --------------------------------------------------------------------------


def test_an_entry_wrapped_over_three_lines_stays_one_block() -> None:
    three = [
        (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
        ("Example Institute | Example Degree in Distributed", BODY_SIZE, 18.0),
        (
            "Systems and Analytics with an Invented Extra Title",
            BODY_SIZE,
            TIGHT_LEADING,
        ),
        ("Mention | Expected 2099", BODY_SIZE, TIGHT_LEADING),
    ]

    result = extract_of(laid_out(PREAMBLE + three + TRAILING_BLOCK_SPACED_PAIR))

    assert entries(result, CandidateType.EDUCATION_ENTRY) == [
        "Example Institute | Example Degree in Distributed\n"
        "Systems and Analytics with an Invented Extra Title\n"
        "Mention | Expected 2099"
    ]


# --------------------------------------------------------------------------
# E. A page break
# --------------------------------------------------------------------------


def test_an_entry_split_by_a_page_break_is_never_joined_across_the_pages() -> None:
    """Two baselines on different pages have no distance, so nothing is merged.

    The pages stay the pages the document wrote, and each candidate carries the
    one it came from.
    """
    first = laid_out(PREAMBLE + [
        (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
        ("Example Institute | Example Degree in Distributed", BODY_SIZE, 18.0),
    ])
    second = laid_out(
        [
            ("Systems and Analytics | Expected 2099", BODY_SIZE, 0.0),
            (
                "Other Institute | Second Example Degree | 2097",
                BODY_SIZE,
                BLOCK_SPACING,
            ),
            (
                "Third Institute | Third Example Degree | 2096",
                BODY_SIZE,
                TIGHT_LEADING,
            ),
        ],
        top=TOP,
    )

    result = extract_of(first, second)

    education = result.of_type(CandidateType.EDUCATION_ENTRY)
    assert [candidate.page_numbers for candidate in education][:2] == [(1,), (2,)]
    assert education[0].raw_text == "Example Institute | Example Degree in Distributed"


# --------------------------------------------------------------------------
# F. The historical rules keep answering as they did
# --------------------------------------------------------------------------


def test_a_blank_line_separated_body_is_still_cut_on_its_blank_lines() -> None:
    from services.digital_twin.cv.models import ExtractedPage, ParsedCv
    from services.digital_twin.cv.sections import detect_sections

    text = (
        f"{HEADING_EDUCATION}\n"
        "Example Institute | Example Degree\n"
        "Expected 2099\n"
        "\n"
        "Other Institute | Second Example Degree\n"
        "2097"
    )
    pages = (ExtractedPage(page_number=1, text=text),)
    sections, warnings = detect_sections(pages)
    result = extract_candidates(
        ParsedCv(
            parser_version=PARSER_VERSION,
            content_sha256="0" * 64,
            page_count=1,
            pages=pages,
            sections=sections,
            warnings=warnings,
        )
    )

    assert rules_of(result, CandidateType.EDUCATION_ENTRY) == {
        ExtractionRule.SECTION_BLANK_LINE_BLOCK.value
    }
    assert len(entries(result, CandidateType.EDUCATION_ENTRY)) == 2


def test_a_bulleted_body_is_still_cut_on_its_list_markers() -> None:
    bulleted = [
        (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
        (
            "- Example Institute | Example Degree in Distributed",
            BODY_SIZE,
            18.0,
        ),
        ("Systems and Analytics | Expected 2099", BODY_SIZE, TIGHT_LEADING),
        (
            "- Other Institute | Second Example Degree | 2097",
            BODY_SIZE,
            TIGHT_LEADING,
        ),
    ]

    result = extract_of(laid_out(PREAMBLE + bulleted + TRAILING_BLOCK_SPACED_PAIR))

    assert rules_of(result, CandidateType.EDUCATION_ENTRY) == {
        ExtractionRule.SECTION_BULLET_BLOCK.value
    }
    assert len(entries(result, CandidateType.EDUCATION_ENTRY)) == 2


def test_a_pipe_delimited_experience_body_is_still_cut_on_its_pipe_lines() -> None:
    experience = [
        (HEADING_EXPERIENCE, HEADING_SIZE, BLOCK_SPACING),
        ("Example Office | Junior Analyst | 2098", BODY_SIZE, 18.0),
        (
            "Wrote invented reports about invented numbers.",
            BODY_SIZE,
            TIGHT_LEADING,
        ),
        ("Other Office | Assistant | 2097", BODY_SIZE, TIGHT_LEADING),
    ]

    result = extract_of(laid_out(PREAMBLE + experience + TRAILING_BLOCK_SPACED_PAIR))

    assert rules_of(result, CandidateType.EXPERIENCE_ENTRY) == {
        ExtractionRule.EXPERIENCE_PIPE_DELIMITED_BLOCK.value
    }
    assert entries(result, CandidateType.EXPERIENCE_ENTRY) == [
        "Example Office | Junior Analyst | 2098\n"
        "Wrote invented reports about invented numbers.",
        "Other Office | Assistant | 2097",
    ]


def test_the_sections_outside_the_allowed_list_are_never_reconstructed() -> None:
    """`LANGUAGES` keeps one entry per line whatever its layout looks like."""
    languages = [
        ("LANGUES", HEADING_SIZE, BLOCK_SPACING),
        (
            "Langue Inventee A parlee couramment dans ce document",
            BODY_SIZE,
            18.0,
        ),
        ("Langue Inventee B lue et ecrite", BODY_SIZE, TIGHT_LEADING),
    ]

    result = extract_of(laid_out(PREAMBLE + languages + TRAILING_BLOCK_SPACED_PAIR))

    assert rules_of(result, CandidateType.LANGUAGE_ENTRY) == {
        ExtractionRule.SECTION_LINE_BLOCK.value
    }
    assert len(entries(result, CandidateType.LANGUAGE_ENTRY)) == 2


# --------------------------------------------------------------------------
# G. Determinism
# --------------------------------------------------------------------------


def test_the_same_document_always_yields_the_same_result() -> None:
    page = laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    content = build_synthetic_pdf([page])

    first = extract_candidates(parse_cv_bytes(content))
    second = extract_candidates(parse_cv_bytes(content))

    assert first == second
    assert parse_cv_bytes(content) == parse_cv_bytes(content)
    assert first.parser_version == PARSER_VERSION == "cv-parser-v4"
    assert first.extractor_version == CANDIDATE_EXTRACTOR_VERSION == "cv-candidates-v5"


# --------------------------------------------------------------------------
# H. Provenance survives a merged block
# --------------------------------------------------------------------------


def test_a_merged_block_keeps_its_section_its_pages_and_its_rule() -> None:
    result = extract_of(
        laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    )

    (entry,) = result.of_type(CandidateType.EDUCATION_ENTRY)

    assert entry.section_type is SectionType.EDUCATION
    assert entry.page_numbers == (1,)
    assert entry.rule_id is ExtractionRule.SECTION_LAYOUT_CONTINUATION_BLOCK
    assert entry.provenance()["rule_id"] == "SECTION_LAYOUT_CONTINUATION_BLOCK"
    assert entry.cv_sha256 and entry.fingerprint


# --------------------------------------------------------------------------
# I. Confidentiality
# --------------------------------------------------------------------------


def test_no_routine_view_of_the_result_quotes_the_document() -> None:
    result = extract_of(
        laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    )
    parsed = parse_cv_bytes(
        build_synthetic_pdf(
            [laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)]
        )
    )
    lines = document_lines(parsed.pages)

    rendered = (
        repr(result.summary())
        + repr(parsed.summary())
        + repr(read_layout_evidence(lines).summary())
    )

    for secret in (
        "Example Institute",
        "Distributed",
        "Prenom",
        "contact@example.invalid",
        HEADING_EDUCATION,
    ):
        assert secret not in rendered
    assert "SECTION_LAYOUT_CONTINUATION_BLOCK" in repr(result.summary())


# --------------------------------------------------------------------------
# J. No hidden semantics
# --------------------------------------------------------------------------


def test_text_that_reads_as_one_sentence_is_not_merged_without_layout_proof() -> None:
    """Two halves of one obvious phrase, laid out as two blocks: still two.

    Nothing but the geometry decides. The words here would make any reader — and
    any rule that read them — join the lines; the document says they are two
    blocks, and the document is what is read.
    """
    tempting = [
        (HEADING_EDUCATION, HEADING_SIZE, BLOCK_SPACING),
        ("Example Institute | Example Degree in Distributed", BODY_SIZE, 18.0),
        ("Systems and Analytics | Expected 2099", BODY_SIZE, BLOCK_SPACING),
    ]

    result = extract_of(laid_out(PREAMBLE + tempting + TRAILING_BLOCK_SPACED_PAIR))

    assert entries(result, CandidateType.EDUCATION_ENTRY) == [
        "Example Institute | Example Degree in Distributed",
        "Systems and Analytics | Expected 2099",
    ]


def test_the_same_layout_merges_whatever_the_words_are() -> None:
    """Replace every word and the answer is identical: no lexicon is consulted."""
    original = laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    # The headings stay headings — without them the document has no sections
    # left to segment — and every body line becomes meaningless characters of
    # exactly the same count, so the layout is untouched and the words are gone.
    scrambled = [
        line
        if line.font_size == HEADING_SIZE
        else PlacedText(
            text="".join(
                "x" if character.isalnum() else character for character in line.text
            ),
            x=line.x,
            y=line.y,
            font_size=line.font_size,
        )
        for line in original
    ]

    result = extract_of(scrambled)

    education = entries(result, CandidateType.EDUCATION_ENTRY)
    assert len(education) == 1
    assert "\n" in education[0]
    assert set(education[0]) <= set("x |\n")


# --------------------------------------------------------------------------
# The evidence primitive itself
# --------------------------------------------------------------------------


def test_the_evidence_reads_the_tightest_repeated_step_of_the_document() -> None:
    page = laid_out(PREAMBLE + WRAPPED_EDUCATION + TRAILING_BLOCK_SPACED_PAIR)
    lines = document_lines(parse_cv_bytes(build_synthetic_pdf([page])).pages)

    evidence = read_layout_evidence(lines)

    assert evidence.distinguishes_spacings
    assert evidence.tight_gap == pytest.approx(TIGHT_LEADING)
    assert len(evidence.placed_lines) == len(page)


def test_the_evidence_needs_more_than_a_couple_of_comparable_steps() -> None:
    assert MIN_GAP_EVIDENCE_PAIRS >= 3
