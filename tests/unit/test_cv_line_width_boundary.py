"""Unit coverage for how far a line is read to reach, and what that is measured in.

Every document below is invented and built in memory from `PlacedText` values
that state where each line physically sits, how big it is and which of the
page's fonts it is set in. TEST ONLY: no real CV, person, institution,
employer, diploma, date or contact detail appears here, in the fixtures or in
the git history, and no PDF is read from disk. Every line of text is nonsense
chosen for its *length*, which is the only thing the code under test reads off
it.

What this module pins
---------------------

`reaches_right_boundary` asks whether the next line's first token could have
fitted on the line above, and answers it by comparing that line's reach with
the reach of the widest line of its column. Both sides used to be raw counts of
characters, and a count of characters is not a length: two lines set at 10 and
at 9.5 points are one column — the size tolerance that makes a column a column
is half a point wide — but 132 characters at 9.5 points do not reach as far as
132 characters at 10 points, and reading both as "132" let the smaller line
describe a boundary the column does not have. The tests below hold every side
of that comparison to one unit, `line_width`'s character-points, and hold the
whole rule to refusing everything it refused before.

The geometry these fixtures are written in is deliberately the one `layout.py`
cannot read — the wrap step and the entry step differ by four tenths of a point
— so that the section is cut by the typography rule and the boundary condition
is the only thing left varying between a merge and a refusal.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from services.digital_twin.cv.candidates import (
    CandidateType,
    ExtractionRule,
    StructuredCvExtraction,
    extract_candidates,
)
from services.digital_twin.cv.candidates.layout import (
    FONT_SIZE_EPSILON,
    MIN_MARGIN_EVIDENCE_LINES,
    DocumentLayoutEvidence,
    PlacedLine,
    line_width,
    read_layout_evidence,
    reaches_right_boundary,
    right_boundary,
    same_column,
)
from services.digital_twin.cv.models import LineLayout
from services.digital_twin.cv.parser import parse_cv_bytes
from services.digital_twin.cv.sections import SourceLine, document_lines
from tests.unit.conftest import PlacedText, build_synthetic_pdf

# --------------------------------------------------------------------------
# The geometry, in PDF points
# --------------------------------------------------------------------------

LEFT_MARGIN = 72.0
#: The body column's size, and the size a *few* of its lines are set at instead.
#: They differ by exactly `FONT_SIZE_EPSILON`, which is what makes them one
#: column and what makes their character counts incomparable.
BODY_SIZE = 10.0
SMALL_SIZE = 9.5
HEADING_SIZE = 13.0
#: The step a wrapped line gets, and the step between two entries. They differ
#: by four tenths of a point, so the document demonstrates one spacing and the
#: spacing rule of `layout.py` reads nothing here.
WRAP_STEP = 12.2
ENTRY_STEP = 12.6
HEADING_STEP = 18.0
BLOCK_STEP = 26.0
TOP = 760.0

#: Two `/BaseFont` names and nothing more. No test asserts which is heavier, and
#: the rule under test is never told.
OPENER = "F2"
CONTINUATION = "F1"
#: A font resource the builder declares with no `/BaseFont` at all.
UNNAMED = "F4"

# --------------------------------------------------------------------------
# TEST ONLY content. Every string below is nonsense, chosen for its length.
# --------------------------------------------------------------------------

HEADING_PROFILE = "PROFIL"
HEADING_EDUCATION = "FORMATION"

#: 121 characters at `BODY_SIZE`: the line the layout engine broke.
WRAPPED_OPENING = (
    "Fanquist Grunfel Thillicrup Quondrix Snerkulon Gravendil "
    "Trundlequist Kliptonoodle Verbolix Vaskiple Crandiff Vandersplix"
)
#: The continuation. Its first token is 7 characters long, and that is the only
#: property of it the rule reads.
WRAPPED_REMAINDER = "Wubbolt Rikkabudil Vandersplix Bramwold Wibblethorp Snerkulon"
#: The same continuation opening on a 3-character token instead, which *would*
#: have fitted on the line above.
NEAR_MISS_REMAINDER = "Zib Rikkabudil Vandersplix Bramwold Wibblethorp Snerkulon"
#: The next entry, opened in the column's opening face.
SECOND_ENTRY = (
    "Vandersplix Bramwold Wibblethorp Muskaloop Fassomere Zaramunt "
    "Plimmert Grommelt Yolbrast Wubbolt"
)
#: 132 characters at `SMALL_SIZE`: more characters than any line of the column,
#: and less reach than the 121-character line set at the column's own size.
SMALL_LONG_LINE = (
    "Vandersplix Bramwold Wibblethorp Muskaloop Fassomere Zaramunt Plimmert "
    "Grommelt Yolbrast Ferndrop Hoskalut Jondrak Lumbaxil Zorpling"
)
#: 140 characters at `SMALL_SIZE`: enough that the smaller line genuinely does
#: reach furthest, even measured properly.
SMALL_WIDER_LINE = (
    "Ombrelly Quellorn Ferndrop Hoskalut Jondrak Lumbaxil Mirquall Crandiff "
    "Verbolix Muskaloop Wibblethorp Bramwold Plimmert Quondrix Vandersplix"
)
#: 20 characters at `SMALL_SIZE`: the same line, describing nothing.
SMALL_SHORT_LINE = "Ombrelly Wibblethorp"
#: 40 characters: a line the document plainly ended on purpose.
SHORT_OPENER = "Zorpling Wubbolt Rikkabudil Trundlequist"

#: The same three education lines with every word replaced by a different,
#: equally meaningless word of the same length.
ALT_OPENING = (
    "Ombrelly Quellorn Ferndrop Hoskalut Jondrak Lumbaxil Mirquall "
    "Crandiff Verbolix Muskaloop Wibblethorp Bramwold Rikkabudil"
)
ALT_REMAINDER = "Kwipzel Ombrelly Quellorn Ferndrop Ipplewer Kressimo Brindolt"
ALT_SECOND = (
    "Ferndrop Hoskalut Jondrak Lumbaxil Mirquall Crandiff Verbolix "
    "Muskaloop Wibblethorp Trundlequist"
)

LEAD_ONE = "Ombrelly Quellorn Ferndrop Pardusk"
LEAD_TWO = "Ombrelly Quellorn Ferndrop Hoskalut Rumbiset"
PROFILE_A_ONE = (
    "Ombrelly Quellorn Ferndrop Hoskalut Jondrak Lumbaxil Mirquall "
    "Crandiff Verbolix Rumbiset"
)
PROFILE_B_ONE = (
    "Ombrelly Quellorn Ferndrop Hoskalut Jondrak Lumbaxil Mirquall Trundlequist"
)
PROFILE_A_TWO = (
    "Ombrelly Quellorn Ferndrop Hoskalut Jondrak Lumbaxil Mirquall "
    "Crandiff Wibblethorp"
)

Row = tuple[str, str, float, float]


def placed(
    rows: Sequence[Row], *, top: float = TOP, x: float = LEFT_MARGIN
) -> list[PlacedText]:
    """Turn `(text, font, font size, step down from the line above)` into a page."""
    laid_out: list[PlacedText] = []
    y = top
    for index, (text, font, size, step) in enumerate(rows):
        if index:
            y -= step
        laid_out.append(PlacedText(text=text, x=x, y=y, font_size=size, font=font))
    return laid_out


def preamble(*, small_line: str = SMALL_LONG_LINE) -> list[Row]:
    """The lines that teach the body column its opener and describe its width.

    Its profile block opens on one face, carries on in another and opens again,
    which is the repetition `typography.py` needs before it will read anything.
    Its last line is the one that matters here: a line of the same column, set
    half a point smaller, holding more characters than any other line of the
    document.
    """
    return [
        ("Prenom Exemple", OPENER, HEADING_SIZE, 0.0),
        (LEAD_ONE, CONTINUATION, BODY_SIZE, 20.0),
        (LEAD_TWO, CONTINUATION, BODY_SIZE, ENTRY_STEP),
        (HEADING_PROFILE, OPENER, HEADING_SIZE, BLOCK_STEP),
        (PROFILE_A_ONE, OPENER, BODY_SIZE, HEADING_STEP),
        (PROFILE_B_ONE, CONTINUATION, BODY_SIZE, WRAP_STEP),
        (PROFILE_A_TWO, OPENER, BODY_SIZE, ENTRY_STEP),
        (small_line, CONTINUATION, SMALL_SIZE, ENTRY_STEP),
    ]


def education(
    *,
    opening: str = WRAPPED_OPENING,
    remainder: str = WRAPPED_REMAINDER,
    second: str = SECOND_ENTRY,
    opening_font: str = OPENER,
    remainder_font: str = CONTINUATION,
    second_font: str = OPENER,
    wrap_step: float = WRAP_STEP,
) -> list[Row]:
    """The three education lines: an opener, its continuation, the next entry."""
    return [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (opening, opening_font, BODY_SIZE, HEADING_STEP),
        (remainder, remainder_font, BODY_SIZE, wrap_step),
        (second, second_font, BODY_SIZE, ENTRY_STEP),
    ]


def extract_of(*pages: Sequence[PlacedText]) -> StructuredCvExtraction:
    """Build, parse and extract one synthetic document."""
    return extract_candidates(parse_cv_bytes(build_synthetic_pdf(list(pages))))


def education_of(result: StructuredCvExtraction) -> list[str]:
    return [
        candidate.raw_text
        for candidate in result.of_type(CandidateType.EDUCATION_ENTRY)
    ]


def rules_of(result: StructuredCvExtraction) -> set[str]:
    return {
        candidate.rule_id.value
        for candidate in result.of_type(CandidateType.EDUCATION_ENTRY)
    }


def layout_of(rows: Sequence[Row]) -> DocumentLayoutEvidence:
    parsed = parse_cv_bytes(build_synthetic_pdf([placed(rows)]))
    return read_layout_evidence(document_lines(parsed.pages))


# --------------------------------------------------------------------------
# A. The lengths this whole module is about
# --------------------------------------------------------------------------


def test_the_fixture_lengths_are_the_ones_the_failure_was_reported_with() -> None:
    """The premise, asserted rather than described.

    121 characters at 10 points, 7 characters of next token, and a line of the
    same column holding 132 characters at 9.5 points.
    """
    assert len(WRAPPED_OPENING) == 121
    assert len(WRAPPED_REMAINDER.split(" ")[0]) == 7
    assert len(SMALL_LONG_LINE) == 132
    assert len(SMALL_WIDER_LINE) == 140
    assert BODY_SIZE - SMALL_SIZE == FONT_SIZE_EPSILON


def test_the_two_sizes_are_one_column_and_two_capacities() -> None:
    """Same left edge, one column — and not the same reach for one character."""
    body = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)
    small = LineLayout(x_start=LEFT_MARGIN, y=688.0, font_size=SMALL_SIZE)

    assert same_column(body, small)
    assert line_width(small, 132) < line_width(body, 121 + 1 + 7)


def test_a_raw_character_count_would_have_read_the_column_the_other_way() -> None:
    """Why the old reading failed, stated in the arithmetic that failed.

    Counted as characters, the smaller line is the longest line of the column
    and the wrapped line falls two characters short of it. Measured as reach,
    the wrapped line runs past it. Nothing about the document changed between
    those two readings — only the unit did.
    """
    raw_boundary = max(len(SMALL_LONG_LINE), len(WRAPPED_OPENING))
    raw_required = len(WRAPPED_OPENING) + 1 + len(WRAPPED_REMAINDER.split(" ")[0])
    assert raw_required <= raw_boundary

    body = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)
    small = LineLayout(x_start=LEFT_MARGIN, y=688.0, font_size=SMALL_SIZE)
    measured_boundary = max(
        line_width(small, len(SMALL_LONG_LINE)),
        line_width(body, len(WRAPPED_OPENING)),
    )
    assert line_width(body, raw_required) > measured_boundary


# --------------------------------------------------------------------------
# B. The reproduction: the document that v4 refused now reads as two entries
# --------------------------------------------------------------------------


def test_the_document_does_not_distinguish_a_wrap_step_from_an_entry_step() -> None:
    """The precondition: the spacing rule reads nothing in these fixtures."""
    evidence = layout_of(preamble() + education())

    assert not evidence.distinguishes_spacings
    assert evidence.tight_gap is None
    assert evidence.placed_lines


def test_the_wrapped_entry_survives_a_smaller_longer_line_in_its_column() -> None:
    result = extract_of(placed(preamble() + education()))

    assert education_of(result) == [
        f"{WRAPPED_OPENING}\n{WRAPPED_REMAINDER}",
        SECOND_ENTRY,
    ]


def test_the_first_block_is_cut_by_the_typography_rule_by_name() -> None:
    result = extract_of(placed(preamble() + education()))

    assert rules_of(result) == {
        ExtractionRule.SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK.value
    }


def test_the_columns_boundary_is_the_widest_reach_not_the_longest_count() -> None:
    """The boundary in character-points, computed from the document itself."""
    evidence = layout_of(preamble() + education())
    body = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)

    boundary = right_boundary(evidence, body)

    # The smaller line still describes the column's edge — it is the widest
    # thing in it — but it describes it in points, not in characters.
    assert boundary == pytest.approx(len(SMALL_LONG_LINE) * SMALL_SIZE)
    assert boundary != max(
        placed.length
        for placed in evidence.placed_lines
        if same_column(placed.layout, body)
    )


# --------------------------------------------------------------------------
# C. The inverse: a smaller line is never ignored, and can still forbid a merge
# --------------------------------------------------------------------------


def test_a_smaller_line_that_genuinely_reaches_furthest_still_sets_the_edge() -> None:
    """140 characters at 9.5 points really do run past 129 at 10, and are read so.

    This is the other half of the fix. The rule is not "disregard lines set at
    another size" — that would throw away the document's own evidence — it is
    "measure every line in the same unit". Measured properly, this smaller line
    is the widest of its column, so the wrapped line has *not* reached the edge
    and the two lines stay apart.
    """
    result = extract_of(placed(preamble(small_line=SMALL_WIDER_LINE) + education()))

    assert education_of(result) == [
        WRAPPED_OPENING,
        WRAPPED_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_that_same_document_merges_once_the_smaller_line_is_short() -> None:
    """The control: only the smaller line's length differs between the two."""
    result = extract_of(placed(preamble(small_line=SMALL_SHORT_LINE) + education()))

    assert education_of(result) == [
        f"{WRAPPED_OPENING}\n{WRAPPED_REMAINDER}",
        SECOND_ENTRY,
    ]


# --------------------------------------------------------------------------
# D. The false-merge battery: everything that must stay apart, stays apart
# --------------------------------------------------------------------------


def test_a_two_entries_both_opening_on_the_signature_stay_apart() -> None:
    """A -> A. Both lines open the way this column opens an entry."""
    result = extract_of(
        placed(preamble() + education(remainder_font=OPENER))
    )

    assert education_of(result) == [
        WRAPPED_OPENING,
        WRAPPED_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_b_a_visibly_short_first_line_never_carries_the_line_below_it() -> None:
    """40 characters at 10 points is not a line a layout engine broke."""
    result = extract_of(placed(preamble() + education(opening=SHORT_OPENER)))

    assert education_of(result) == [
        SHORT_OPENER,
        WRAPPED_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_c_a_next_token_that_would_have_fitted_refuses_the_merge() -> None:
    """The same page, the same faces, a shorter first token on the line below.

    121 + 1 + 3 characters at 10 points stop just inside the column's edge, so
    the layout engine had room and did not need to break anything. Only the
    length of that token differs from the merging document.
    """
    result = extract_of(
        placed(preamble() + education(remainder=NEAR_MISS_REMAINDER))
    )

    assert education_of(result) == [
        WRAPPED_OPENING,
        NEAR_MISS_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_d_a_continuation_at_another_left_edge_is_not_merged() -> None:
    """A line starting six points further in is not this column's line."""
    rows = preamble() + education()
    page = placed(rows)
    moved = [
        (
            PlacedText(
                text=line.text,
                x=LEFT_MARGIN + 6.0,
                y=line.y,
                font_size=line.font_size,
                font=line.font,
            )
            if line.text == WRAPPED_REMAINDER
            else line
        )
        for line in page
    ]

    result = extract_of(moved)

    assert education_of(result) == [
        WRAPPED_OPENING,
        WRAPPED_REMAINDER,
        SECOND_ENTRY,
    ]


def test_e_a_continuation_far_below_its_opener_is_not_merged() -> None:
    """A step of more than three times the font size is not two adjacent lines."""
    result = extract_of(
        placed(preamble() + education(wrap_step=BODY_SIZE * 3.5))
    )

    assert education_of(result) == [
        WRAPPED_OPENING,
        WRAPPED_REMAINDER,
        SECOND_ENTRY,
    ]


def test_f_a_document_stating_no_typeface_merges_nothing() -> None:
    """Every line set in a font resource the PDF gives no `/BaseFont`."""
    rows = [
        (text, UNNAMED, size, step)
        for text, _, size, step in preamble() + education()
    ]

    result = extract_of(placed(rows))

    assert education_of(result) == [
        WRAPPED_OPENING,
        WRAPPED_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_g_replacing_every_word_changes_no_decision() -> None:
    """The same geometry with different nonsense of the same lengths."""
    assert len(ALT_OPENING) == len(WRAPPED_OPENING)
    assert len(ALT_REMAINDER) == len(WRAPPED_REMAINDER)
    assert len(ALT_SECOND) == len(SECOND_ENTRY)
    assert len(ALT_REMAINDER.split(" ")[0]) == len(WRAPPED_REMAINDER.split(" ")[0])
    assert {ALT_OPENING, ALT_REMAINDER, ALT_SECOND}.isdisjoint(
        {WRAPPED_OPENING, WRAPPED_REMAINDER, SECOND_ENTRY}
    )

    result = extract_of(
        placed(
            preamble()
            + education(
                opening=ALT_OPENING, remainder=ALT_REMAINDER, second=ALT_SECOND
            )
        )
    )

    assert education_of(result) == [f"{ALT_OPENING}\n{ALT_REMAINDER}", ALT_SECOND]
    assert rules_of(result) == {
        ExtractionRule.SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK.value
    }


def test_h_the_decision_no_longer_follows_the_longest_raw_count() -> None:
    """Both documents below have the same longest *count* and opposite answers.

    One column holds a 132-character line, the other a 140-character one, and in
    both the longest count belongs to the smaller size while the wrapped line
    counts 121. A rule reading `max(len(text))` cannot tell these two documents
    apart — it refuses both. Measured as reach, they differ, and the answers
    differ with them.
    """
    merged = extract_of(placed(preamble(small_line=SMALL_LONG_LINE) + education()))
    refused = extract_of(placed(preamble(small_line=SMALL_WIDER_LINE) + education()))

    raw_required = len(WRAPPED_OPENING) + 1 + len(WRAPPED_REMAINDER.split(" ")[0])
    assert raw_required <= len(SMALL_LONG_LINE) < len(SMALL_WIDER_LINE)

    assert len(education_of(merged)) == 2
    assert len(education_of(refused)) == 3


# --------------------------------------------------------------------------
# E. Dimensional consistency: one unit, on every side of the comparison
# --------------------------------------------------------------------------


def line_of(text: str, size: float, *, y: float = 700.0) -> SourceLine:
    return SourceLine(
        text=text,
        page_number=1,
        layout=LineLayout(x_start=LEFT_MARGIN, y=y, font_size=size),
    )


def evidence_of(*lines: SourceLine) -> DocumentLayoutEvidence:
    return DocumentLayoutEvidence(
        tight_gap=None,
        placed_lines=tuple(
            PlacedLine(
                page_number=line.page_number,
                layout=line.layout,
                length=len(line.text),
                width=line_width(line.layout, len(line.text)),
            )
            for line in lines
        ),
    )


def test_a_width_is_characters_carried_into_the_size_they_are_set_at() -> None:
    layout = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)

    assert line_width(layout, 0) == 0.0
    assert line_width(layout, 121) == pytest.approx(121 * BODY_SIZE)
    assert line_width(layout, 1) == pytest.approx(BODY_SIZE)


def test_every_term_of_the_boundary_test_is_in_the_same_unit() -> None:
    """Scaling every size of the document scales the boundary by the same factor.

    A quantity in character-points is homogeneous of degree one in the sizes; a
    raw character count is homogeneous of degree zero. Comparing one against the
    other is the mixed-unit comparison this module exists to remove, and this
    test is what a reintroduction of it would fail.
    """
    # A hairline size difference inside the cohort, so that the column stays one
    # column at every factor and the two sizes still differ: what is being
    # measured here is the unit, not `same_column`.
    other = BODY_SIZE - 0.1
    for factor in (0.5, 2.0, 3.0):
        plain = evidence_of(
            line_of("a" * 40, BODY_SIZE),
            line_of("a" * 60, BODY_SIZE),
            line_of("a" * 132, other),
        )
        scaled = evidence_of(
            line_of("a" * 40, BODY_SIZE * factor),
            line_of("a" * 60, BODY_SIZE * factor),
            line_of("a" * 132, other * factor),
        )
        asked = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)
        asked_scaled = LineLayout(
            x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE * factor
        )

        assert right_boundary(plain, asked) == pytest.approx(132 * other)
        assert right_boundary(scaled, asked_scaled) == pytest.approx(
            right_boundary(plain, asked) * factor
        )


def test_scaling_a_whole_document_changes_no_decision() -> None:
    """The same document typeset half as large decides identically.

    Every term of the comparison carries the same factor, so the factor cancels.
    That is the property a mixed-unit comparison does not have.
    """
    for factor in (0.5, 2.0):
        rows = [
            (text, font, size * factor, step * factor)
            for text, font, size, step in preamble() + education()
        ]

        result = extract_of(placed(rows))

        assert education_of(result) == [
            f"{WRAPPED_OPENING}\n{WRAPPED_REMAINDER}",
            SECOND_ENTRY,
        ]


def test_a_column_set_at_one_size_decides_exactly_as_a_raw_count_would() -> None:
    """The non-regression property, stated as an equivalence.

    Where every line of a column is set at the same size, the size is a common
    positive factor of both sides of the comparison and cancels. Every document
    the rule already read — which is every document whose column is one size —
    is therefore read exactly as before, and this test walks the boundary case
    from clearly short to clearly full to prove it.
    """
    widest = 100
    lines = [
        line_of("a" * 40, BODY_SIZE),
        line_of("a" * 70, BODY_SIZE),
        line_of("a" * widest, BODY_SIZE),
    ]
    evidence = evidence_of(*lines)
    for length in range(90, 100):
        previous = line_of("a" * length, BODY_SIZE)
        following = line_of("bbbbb rest", BODY_SIZE)

        raw = length + 1 + 5 > widest

        assert reaches_right_boundary(evidence, previous, following) is raw


# --------------------------------------------------------------------------
# F. A size the PDF states no width can be derived from
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size", [0.0, -1.0, -0.001, float("nan"), float("inf"), float("-inf")]
)
def test_a_line_with_an_unusable_size_has_no_width(size: float) -> None:
    """No default is invented for a size that is not a size."""
    layout = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=size)

    assert line_width(layout, 121) is None


def test_a_negative_length_has_no_width_either() -> None:
    layout = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)

    assert line_width(layout, -1) is None


def test_a_line_with_no_width_describes_no_boundary() -> None:
    """It is left out of the column's evidence rather than counted as zero."""
    evidence = evidence_of(
        line_of("a" * 40, BODY_SIZE),
        line_of("a" * 70, BODY_SIZE),
        line_of("a" * 200, float("nan")),
        line_of("a" * 100, BODY_SIZE),
    )
    asked = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)

    assert right_boundary(evidence, asked) == pytest.approx(100 * BODY_SIZE)


def test_too_few_measurable_lines_leave_the_column_with_no_boundary() -> None:
    """A column described by fewer than the minimum says nothing at all."""
    evidence = evidence_of(
        line_of("a" * 40, BODY_SIZE),
        line_of("a" * 70, BODY_SIZE),
        *[line_of("a" * 200, 0.0) for _ in range(MIN_MARGIN_EVIDENCE_LINES)],
    )
    asked = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)

    assert right_boundary(evidence, asked) is None
    assert (
        reaches_right_boundary(
            evidence, line_of("a" * 200, BODY_SIZE), line_of("bbb rest", BODY_SIZE)
        )
        is False
    )


def test_a_line_whose_own_size_is_unusable_never_reaches_the_boundary() -> None:
    """The column has a boundary; the line asking about it has no width at all.

    The sizes here are tiny on purpose: they are what puts a line set at zero
    points in the same column as lines that do have a width, so the boundary is
    genuinely established and the only thing missing is the asking line's own
    reach. Nothing is substituted for it and the answer is a refusal.
    """
    evidence = evidence_of(
        line_of("a" * 40, 0.4),
        line_of("a" * 70, 0.4),
        line_of("a" * 100, 0.4),
    )
    asked = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=0.0)
    assert right_boundary(evidence, asked) == pytest.approx(100 * 0.4)

    previous = line_of("a" * 200, 0.0)

    assert line_width(previous.layout, len(previous.text)) is None
    assert reaches_right_boundary(evidence, previous, line_of("bbb x", 0.4)) is False


# --------------------------------------------------------------------------
# G. What the proxy cannot see, said plainly
# --------------------------------------------------------------------------


def test_the_width_is_a_proxy_and_does_not_know_glyph_shapes() -> None:
    """Eight narrow characters and eight wide ones measure the same here.

    In a proportional font they are not the same width, and this is the
    documented limit of the representation: it is a proxy, never a measured
    advance. `pypdf`'s public API states no glyph metrics for the text it
    extracts, and inventing them is the guess this package refuses.
    """
    layout = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)

    assert line_width(layout, len("iiiiiiii")) == line_width(
        layout, len("WWWWWWWW")
    )


def test_the_direction_of_that_error_is_the_conservative_one() -> None:
    """A column read as wider than it is refuses a merge; it never invents one.

    The boundary is the widest line the column shows. Every line the column adds
    can only push that boundary out, never pull it in, so an unmeasured glyph
    shape can only make the rule ask *more* of a line before calling it full.
    """
    base = evidence_of(
        line_of("a" * 40, BODY_SIZE),
        line_of("a" * 70, BODY_SIZE),
        line_of("a" * 100, BODY_SIZE),
    )
    wider = evidence_of(
        line_of("a" * 40, BODY_SIZE),
        line_of("a" * 70, BODY_SIZE),
        line_of("a" * 100, BODY_SIZE),
        line_of("a" * 130, BODY_SIZE),
    )
    previous = line_of("a" * 110, BODY_SIZE)
    following = line_of("bbbbb rest", BODY_SIZE)

    assert reaches_right_boundary(base, previous, following) is True
    assert reaches_right_boundary(wider, previous, following) is False


def test_the_boundary_never_reads_a_column_narrower_than_its_widest_line() -> None:
    """Whatever the sizes in the cohort, no member is measured away."""
    evidence = layout_of(preamble() + education())
    asked = LineLayout(x_start=LEFT_MARGIN, y=700.0, font_size=BODY_SIZE)
    boundary = right_boundary(evidence, asked)

    for placed_line in evidence.placed_lines:
        if placed_line.width is None or not same_column(placed_line.layout, asked):
            continue
        assert placed_line.width <= boundary
        assert math.isfinite(placed_line.width)
