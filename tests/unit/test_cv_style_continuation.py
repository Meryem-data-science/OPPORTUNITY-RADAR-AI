"""Unit coverage for reconstructing a wrapped entry from the page typography.

Every document below is invented and built in memory from `PlacedText` values
that state where each line physically sits and which of the page's fonts it is
set in. TEST ONLY: no real CV, person, institution, employer, diploma or contact
detail appears here, in the fixtures or in the git history, and no PDF is read
from disk.

The geometry these fixtures are written in is the one `layout.py` cannot read.
Its rule needs the document to demonstrate two distinguishable baseline steps —
a tight one inside a block, a looser one between blocks — and here the step
between two entries and the step inside a wrapped one differ by four tenths of a
point. That is not a defect of the fixtures: it is the shape of document this
whole module exists for, and the first test below pins it, so that a future
change loosening `layout.py` into answering these documents would fail loudly
rather than quietly taking this rule's place.

The fixtures describe *layouts and typefaces*, never meanings. Which typeface
opens an entry is something each document demonstrates for itself; nothing here
or in the code under test knows that one of them is bold and the other is not,
and several tests below exist only to hold it to that.
"""

from __future__ import annotations

from collections.abc import Sequence

from services.digital_twin.cv.candidates import (
    CandidateType,
    ExtractionRule,
    StructuredCvExtraction,
    extract_candidates,
)
from services.digital_twin.cv.candidates.layout import (
    LAYOUT_CONTINUATION_SECTION_TYPES,
    read_layout_evidence,
)
from services.digital_twin.cv.candidates.models import CANDIDATE_EXTRACTOR_VERSION
from services.digital_twin.cv.candidates.typography import (
    STYLE_CONTINUATION_SECTION_TYPES,
    read_style_evidence,
)
from services.digital_twin.cv.models import PARSER_VERSION, ExtractedPage, SectionType
from services.digital_twin.cv.parser import parse_cv_bytes
from services.digital_twin.cv.sections import SourceLine, document_lines
from tests.unit.conftest import PlacedText, build_synthetic_pdf

#: The geometry every fixture is written in, in PDF points.
#:
#: `WRAP_STEP` and `ENTRY_STEP` are the whole point: the step the layout engine
#: uses when it carries a line on, and the step this document puts between two
#: entries. They differ by 0.4 points — far below anything that could be read as
#: two different spacings — so the document demonstrates one spacing, not two.
LEFT_MARGIN = 72.0
BODY_SIZE = 11.0
HEADING_SIZE = 13.0
WRAP_STEP = 12.2
ENTRY_STEP = 12.6
#: The step below a heading. Headings are set larger, so a heading-to-body step
#: is never compared with a body-to-body one.
HEADING_STEP = 18.0
BLOCK_STEP = 26.0
TOP = 760.0

#: The page font resources the fixtures name. `OPENER` and `CONTINUATION` are
#: two different `/BaseFont` names and nothing more: no test asserts which of
#: them is heavier, and the rule under test is never told.
OPENER = "F2"
CONTINUATION = "F1"
THIRD_FACE = "F3"
#: A font resource the builder declares with no `/BaseFont` at all: the document
#: that states no typography.
UNNAMED = "F4"

# TEST ONLY content. Every line below is invented and describes nobody.
HEADING_PROFILE = "PROFIL"
HEADING_EDUCATION = "FORMATION"

#: The three lines the whole mission is about, as a reader sees them:
#:
#:     Example Institute | Example Degree in Distributed
#:     Systems and Analytics | Expected 2099
#:     Example Secondary Diploma | Highest Distinction
#:
#: The first two are one entry the layout engine broke; the third opens the
#: second entry. Nothing in the *words* says so.
FIRST_ENTRY_OPENING = "Example Institute | Example Degree in Distributed"
FIRST_ENTRY_REMAINDER = "Systems and Analytics | Expected 2099"
SECOND_ENTRY = "Example Secondary Diploma | Highest Distinction"


def placed(
    rows: Sequence[tuple[str, str, float, float]],
    *,
    top: float = TOP,
    x: float = LEFT_MARGIN,
) -> list[PlacedText]:
    """Turn `(text, font, font size, step down from the line above)` into a page.

    The first row's step is ignored: it sits at `top`. Everything else is placed
    by walking down the page, which is how these tests describe a layout without
    ever writing an absolute coordinate.
    """
    laid_out: list[PlacedText] = []
    y = top
    for index, (text, font, size, step) in enumerate(rows):
        if index:
            y -= step
        laid_out.append(
            PlacedText(text=text, x=x, y=y, font_size=size, font=font)
        )
    return laid_out


#: What every fixture opens with. It gives the document enough lines in the body
#: column for that column's right-hand boundary to be described by the document
#: rather than by a constant, and it is written in the same near-uniform spacing
#: as the rest, so it never accidentally supplies the looser step `layout.py`
#: would need.
PREAMBLE: list[tuple[str, str, float, float]] = [
    ("Prenom Exemple", OPENER, HEADING_SIZE, 0.0),
    ("Data Analyst", CONTINUATION, BODY_SIZE, 20.0),
    ("contact@example.invalid", CONTINUATION, BODY_SIZE, ENTRY_STEP),
    (HEADING_PROFILE, OPENER, HEADING_SIZE, BLOCK_STEP),
    ("Example profile sentence written to reach the right", CONTINUATION,
     BODY_SIZE, HEADING_STEP),
    ("edge of this invented column.", CONTINUATION, BODY_SIZE, ENTRY_STEP),
]

#: The reproduction: opener, continuation, opener, at near-identical steps.
EDUCATION_A_B_A: list[tuple[str, str, float, float]] = [
    (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
    (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
    (FIRST_ENTRY_REMAINDER, CONTINUATION, BODY_SIZE, WRAP_STEP),
    (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
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


# --------------------------------------------------------------------------
# A. The document these fixtures describe is one the spacing rule cannot read
# --------------------------------------------------------------------------


def test_the_document_does_not_distinguish_a_wrap_step_from_an_entry_step() -> None:
    """The precondition of everything below, asserted rather than assumed.

    If this ever fails, the fixtures stopped describing the problem: the spacing
    rule would be answering these documents, and the tests that follow would be
    passing for a reason that has nothing to do with typography.
    """
    parsed = parse_cv_bytes(
        build_synthetic_pdf([placed(PREAMBLE + EDUCATION_A_B_A)])
    )

    evidence = read_layout_evidence(document_lines(parsed.pages))

    assert not evidence.distinguishes_spacings
    assert evidence.tight_gap is None
    # And the lines really are placed, so the refusal is about the spacing and
    # not about missing layout facts.
    assert evidence.placed_lines


# --------------------------------------------------------------------------
# B. The reproduction: A -> B -> A becomes two entries, not three
# --------------------------------------------------------------------------


def test_an_opener_continuation_opener_run_becomes_two_education_entries() -> None:
    result = extract_of(placed(PREAMBLE + EDUCATION_A_B_A))

    education = education_of(result)

    assert len(education) == 2
    assert education == [
        f"{FIRST_ENTRY_OPENING}\n{FIRST_ENTRY_REMAINDER}",
        SECOND_ENTRY,
    ]


def test_the_first_entry_holds_both_wrapped_lines_verbatim() -> None:
    result = extract_of(placed(PREAMBLE + EDUCATION_A_B_A))

    first, _ = result.of_type(CandidateType.EDUCATION_ENTRY)

    # Both lines survive exactly as the document wrote them, and the break the
    # layout engine inserted is still visible to the human who reviews it.
    assert first.raw_text.split("\n") == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
    ]
    assert first.normalized_value is None


def test_the_second_entry_holds_only_the_third_line() -> None:
    result = extract_of(placed(PREAMBLE + EDUCATION_A_B_A))

    _, second = result.of_type(CandidateType.EDUCATION_ENTRY)

    assert second.raw_text == SECOND_ENTRY


def test_the_merge_is_attributed_to_the_typography_rule_by_name() -> None:
    """A reader of the provenance can tell *which* evidence grouped the lines."""
    result = extract_of(placed(PREAMBLE + EDUCATION_A_B_A))

    assert rules_of(result) == {
        ExtractionRule.SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK.value
    }


def test_the_opener_signature_is_learned_from_the_document_not_hardcoded() -> None:
    """The same geometry with the two typefaces exchanged decides the same way.

    Here the entries open in the face the other fixtures use for continuations,
    and wrap into the one they use for openers. If anything in the rule believed
    that one particular typeface "is" an opener, this document would be cut the
    other way round — or not at all.
    """
    swapped = [
        (HEADING_EDUCATION, CONTINUATION, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, CONTINUATION, BODY_SIZE, HEADING_STEP),
        (FIRST_ENTRY_REMAINDER, OPENER, BODY_SIZE, WRAP_STEP),
        (SECOND_ENTRY, CONTINUATION, BODY_SIZE, ENTRY_STEP),
    ]
    preamble = [
        (text, OPENER if font == CONTINUATION else CONTINUATION, size, step)
        for text, font, size, step in PREAMBLE
    ]

    result = extract_of(placed(preamble + swapped))

    assert education_of(result) == [
        f"{FIRST_ENTRY_OPENING}\n{FIRST_ENTRY_REMAINDER}",
        SECOND_ENTRY,
    ]


# --------------------------------------------------------------------------
# C. TEST J: the decision is about the page, never about the words
# --------------------------------------------------------------------------

#: The same three lines with every word replaced by a different, equally
#: meaningless word of the same length. The lengths are held because how far a
#: line reaches is the one thing the rule reads off a line's text — a character
#: count standing in for a width, never a word — so keeping them is what makes
#: this a test of *meaning* rather than of geometry.
NONSENSE_OPENING = "Wubbolt Fanquist X Zorpling Grunfel op Rikkabudil"
NONSENSE_REMAINDER = "Plimmert und Vaskiple X Quondrix 8823"
NONSENSE_SECOND = "Bramwold Thillicrup X Grommeltifexy Vandersplix"


def test_replacing_every_word_changes_no_decision() -> None:
    assert len(NONSENSE_OPENING) == len(FIRST_ENTRY_OPENING)
    assert len(NONSENSE_REMAINDER) == len(FIRST_ENTRY_REMAINDER)
    assert len(NONSENSE_SECOND) == len(SECOND_ENTRY)

    nonsense = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (NONSENSE_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (NONSENSE_REMAINDER, CONTINUATION, BODY_SIZE, WRAP_STEP),
        (NONSENSE_SECOND, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(PREAMBLE + nonsense))

    assert education_of(result) == [
        f"{NONSENSE_OPENING}\n{NONSENSE_REMAINDER}",
        NONSENSE_SECOND,
    ]
    assert rules_of(result) == {
        ExtractionRule.SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK.value
    }


# --------------------------------------------------------------------------
# D. A document that has *demonstrated* its opening typeface
# --------------------------------------------------------------------------

#: A preamble whose profile paragraph already shows the body column opening on
#: one typeface, carrying on in another and opening again. It teaches the column
#: its opening signature outside any section that is cut into entries, so the
#: tests below can vary one property of an EDUCATION run at a time and know that
#: a refusal to merge is about *that* property and not about missing evidence.
TEACHING_PREAMBLE: list[tuple[str, str, float, float]] = [
    ("Prenom Exemple", OPENER, HEADING_SIZE, 0.0),
    ("Data Analyst", CONTINUATION, BODY_SIZE, 20.0),
    ("contact@example.invalid", CONTINUATION, BODY_SIZE, ENTRY_STEP),
    (HEADING_PROFILE, OPENER, HEADING_SIZE, BLOCK_STEP),
    ("Example profile sentence written to reach the right", OPENER,
     BODY_SIZE, HEADING_STEP),
    ("edge of this invented column.", CONTINUATION, BODY_SIZE, WRAP_STEP),
    ("Example second profile sentence, also invented.", OPENER,
     BODY_SIZE, ENTRY_STEP),
]


def test_the_teaching_preamble_alone_demonstrates_the_columns_opener() -> None:
    """The control for section E: this document really has learned an opener."""
    parsed = parse_cv_bytes(
        build_synthetic_pdf([placed(TEACHING_PREAMBLE + EDUCATION_A_B_A)])
    )

    evidence = read_style_evidence(document_lines(parsed.pages))

    assert evidence.demonstrates_openers
    # One column of the document — the body column — demonstrated one signature.
    assert len(evidence.openers) == 1
    assert evidence.summary() == {
        "demonstrates_openers": True,
        "opener_column_count": 1,
    }


def test_the_taught_document_still_merges_the_wrapped_entry() -> None:
    """The control every negative test below is varied from."""
    result = extract_of(placed(TEACHING_PREAMBLE + EDUCATION_A_B_A))

    assert education_of(result) == [
        f"{FIRST_ENTRY_OPENING}\n{FIRST_ENTRY_REMAINDER}",
        SECOND_ENTRY,
    ]


# --------------------------------------------------------------------------
# E. The false-positive battery: everything that must stay two, or three
# --------------------------------------------------------------------------


def test_b_two_entries_both_opening_on_the_signature_stay_apart() -> None:
    """TEST B. `A -> A`: two real entries, and the opener *is* demonstrated.

    This is the merge that must never happen. Both lines open on the typeface
    the column uses for entries, so neither is a continuation of the other, and
    the near-identical spacing gives the rule no excuse to think otherwise.
    """
    both_openers = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(TEACHING_PREAMBLE + both_openers))

    assert education_of(result) == [FIRST_ENTRY_OPENING, SECOND_ENTRY]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_c_two_lines_neither_opening_on_the_signature_stay_apart() -> None:
    """TEST C. `B -> B`: no line here was opened by the column's opener.

    The block the second line would join was never opened by an entry opener,
    so there is no entry for it to be the continuation *of*.
    """
    neither_opener = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, CONTINUATION, BODY_SIZE, HEADING_STEP),
        (SECOND_ENTRY, CONTINUATION, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(TEACHING_PREAMBLE + neither_opener))

    assert education_of(result) == [FIRST_ENTRY_OPENING, SECOND_ENTRY]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_d_one_pair_of_typefaces_with_no_repetition_is_not_guessed_at() -> None:
    """TEST D. `A -> B` and nothing else: two lines, two faces, no evidence.

    A document showing this once has shown a wrapped entry and two entries set
    differently in exactly equal measure. Nothing decides between them, so
    nothing is decided.
    """
    unrepeated = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (SECOND_ENTRY, CONTINUATION, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(PREAMBLE + unrepeated))

    assert education_of(result) == [FIRST_ENTRY_OPENING, SECOND_ENTRY]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_e_three_different_typefaces_with_no_repetition_are_not_guessed_at() -> None:
    """TEST E. `A -> B -> C`: three faces, no signature repeated at all."""
    all_different = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (FIRST_ENTRY_REMAINDER, CONTINUATION, BODY_SIZE, WRAP_STEP),
        (SECOND_ENTRY, THIRD_FACE, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(PREAMBLE + all_different))

    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_f_a_continuation_at_another_left_edge_is_not_merged() -> None:
    """TEST F. `A -> B -> A`, but B starts in another column."""
    page = placed(TEACHING_PREAMBLE + EDUCATION_A_B_A)
    indented = [
        line
        if line.text != FIRST_ENTRY_REMAINDER
        else PlacedText(
            text=line.text,
            x=LEFT_MARGIN + 24.0,
            y=line.y,
            font_size=line.font_size,
            font=line.font,
        )
        for line in page
    ]

    result = extract_of(indented)

    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_g_a_continuation_at_another_font_size_is_not_merged() -> None:
    """TEST G. `A -> B -> A`, but B is set at a size the column does not use."""
    page = placed(TEACHING_PREAMBLE + EDUCATION_A_B_A)
    resized = [
        line
        if line.text != FIRST_ENTRY_REMAINDER
        else PlacedText(
            text=line.text,
            x=line.x,
            y=line.y,
            font_size=BODY_SIZE - 3.0,
            font=line.font,
        )
        for line in page
    ]

    result = extract_of(resized)

    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_h_a_continuation_far_below_its_opener_is_not_merged() -> None:
    """TEST H. `A -> B -> A`, but a whole block of space sits between A and B.

    Everything else about the run is unchanged. What stops the merge here is the
    ceiling on how far apart two baselines of one block can be: a line several
    times its own font size below the one above it is not that line carried on,
    whatever typeface it is set in.
    """
    far_apart = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (FIRST_ENTRY_REMAINDER, CONTINUATION, BODY_SIZE, BODY_SIZE * 4.0),
        (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(TEACHING_PREAMBLE + far_apart))

    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_i_a_continuation_on_the_next_page_is_not_merged() -> None:
    """TEST I. A block is never carried across a page break.

    Page one ends on a line set in the opening typeface and long enough to have
    been wrapped; page two opens on a line set in the other one. Every condition
    but the page holds, and the two stay apart — two baselines on different
    pages have no distance between them to read.
    """
    first_page = placed(
        TEACHING_PREAMBLE
        + EDUCATION_A_B_A
        + [(FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, ENTRY_STEP)]
    )
    second_page = placed(
        [(FIRST_ENTRY_REMAINDER, CONTINUATION, BODY_SIZE, 0.0)], top=TOP
    )

    result = extract_of(first_page, second_page)

    # The wrapped pair on page one is still one entry, so the run really did
    # keep its evidence; the pair split by the page break stays two.
    assert education_of(result) == [
        f"{FIRST_ENTRY_OPENING}\n{FIRST_ENTRY_REMAINDER}",
        SECOND_ENTRY,
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
    ]


def test_a_line_that_stops_short_of_the_boundary_never_carries_on() -> None:
    """The condition doing most of the work, on its own.

    The typography is exactly the merging document's — opener, other face,
    opener — and the opener line is short. A layout engine does not break a line
    that had room left, so this one was ended by the document and the line under
    it is the next entry, whatever face it is set in.
    """
    short_opener = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        ("Example Institute", OPENER, BODY_SIZE, HEADING_STEP),
        (FIRST_ENTRY_REMAINDER, CONTINUATION, BODY_SIZE, WRAP_STEP),
        (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(TEACHING_PREAMBLE + short_opener))

    assert education_of(result) == [
        "Example Institute",
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


# --------------------------------------------------------------------------
# F. A line whose typeface changes part-way through
# --------------------------------------------------------------------------


def test_a_line_is_read_by_the_typeface_it_opens_in_not_the_one_it_ends_in() -> None:
    """An entry may be set in several faces on one line, and usually is.

    Here the first line opens in the column's opening typeface and switches to
    the other one part-way through, which is how a CV sets an institution and
    then what was studied. The line under it is set entirely in that second
    face. The rule must read the first line as *opened* by the opener — if it
    read the face the line ended in, the two lines would look identical and it
    would see `B -> B` where the document wrote `A -> B`.
    """
    head, tail = "Example Institute ", "| Example Degree in Distributed"
    assert head + tail == FIRST_ENTRY_OPENING

    page = placed(TEACHING_PREAMBLE + EDUCATION_A_B_A)
    mixed = [
        line
        if line.text != FIRST_ENTRY_OPENING
        else PlacedText(
            text=head,
            x=line.x,
            y=line.y,
            font_size=line.font_size,
            font=OPENER,
            runs=((tail, CONTINUATION),),
        )
        for line in page
    ]

    result = extract_of(mixed)

    assert education_of(result) == [
        f"{FIRST_ENTRY_OPENING}\n{FIRST_ENTRY_REMAINDER}",
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {
        ExtractionRule.SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK.value
    }


def test_the_signature_recorded_for_a_mixed_line_is_the_one_it_opened_in() -> None:
    """The same fact, read straight off the parse rather than off the cut."""
    head, tail = "Example Institute ", "| Example Degree in Distributed"
    parsed = parse_cv_bytes(
        build_synthetic_pdf(
            [
                [
                    PlacedText(
                        text=head,
                        y=TOP,
                        font_size=BODY_SIZE,
                        font=OPENER,
                        runs=((tail, CONTINUATION),),
                    ),
                    PlacedText(
                        text=FIRST_ENTRY_REMAINDER,
                        y=TOP - WRAP_STEP,
                        font_size=BODY_SIZE,
                        font=CONTINUATION,
                    ),
                ]
            ]
        )
    )

    first, second = parsed.pages[0].lines

    assert first.text == FIRST_ENTRY_OPENING
    assert first.layout is not None and second.layout is not None
    # Two different `/BaseFont` names, and the mixed line carries the first.
    assert first.layout.start_font_signature != second.layout.start_font_signature
    assert first.layout.start_font_signature == "/Helvetica-Bold"
    assert second.layout.start_font_signature == "/Helvetica"


# --------------------------------------------------------------------------
# G. A document whose typography the PDF does not state
# --------------------------------------------------------------------------


def test_a_document_stating_no_basefont_carries_no_signature() -> None:
    parsed = parse_cv_bytes(
        build_synthetic_pdf(
            [
                [
                    PlacedText(text=FIRST_ENTRY_OPENING, y=TOP,
                               font_size=BODY_SIZE, font=UNNAMED),
                    PlacedText(text=FIRST_ENTRY_REMAINDER, y=TOP - WRAP_STEP,
                               font_size=BODY_SIZE, font=UNNAMED),
                ]
            ]
        )
    )

    # The line is placed — position and size were stated — and only the
    # typeface is absent. Absence, not a default.
    for line in parsed.pages[0].lines:
        assert line.layout is not None
        assert line.layout.start_font_signature is None


def test_a_document_stating_no_basefont_merges_nothing_and_does_not_crash() -> None:
    """The feature is additive: without the signal, v3's answer is unchanged."""
    unnamed_everywhere = [
        (text, UNNAMED, size, step)
        for text, _, size, step in TEACHING_PREAMBLE + EDUCATION_A_B_A
    ]

    result = extract_of(placed(unnamed_everywhere))

    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


def test_a_hole_in_the_signatures_of_a_run_demonstrates_nothing() -> None:
    """One unnamed face inside an otherwise perfect run is enough to refuse.

    A sequence with a hole in it cannot show an alternation, so the run is read
    as demonstrating nothing rather than as demonstrating what its stated parts
    happen to look like.
    """
    holed = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (FIRST_ENTRY_REMAINDER, UNNAMED, BODY_SIZE, WRAP_STEP),
        (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(PREAMBLE + holed))

    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        FIRST_ENTRY_REMAINDER,
        SECOND_ENTRY,
    ]
    assert rules_of(result) == {ExtractionRule.SECTION_LINE_BLOCK.value}


# --------------------------------------------------------------------------
# H. Documents with no layout at all
# --------------------------------------------------------------------------


def test_a_document_read_from_text_alone_demonstrates_no_opener() -> None:
    """A `ParsedCv` built without layout gets no invented typography.

    This is the shape every caller written before layout facts existed uses, and
    it must stay exactly as conservative as it was.
    """
    pages = (
        ExtractedPage(
            page_number=1,
            text="\n".join(
                [HEADING_EDUCATION, FIRST_ENTRY_OPENING, FIRST_ENTRY_REMAINDER,
                 SECOND_ENTRY]
            ),
        ),
    )

    evidence = read_style_evidence(document_lines(pages))

    assert not evidence.demonstrates_openers
    assert evidence.openers == ()
    assert evidence.summary() == {
        "demonstrates_openers": False,
        "opener_column_count": 0,
    }


def test_a_layout_free_line_carries_no_signature_of_its_own() -> None:
    line = SourceLine(text=FIRST_ENTRY_OPENING, page_number=1)

    assert line.layout is None


# --------------------------------------------------------------------------
# I. Scope: the rule reaches EDUCATION and stops there
# --------------------------------------------------------------------------


def test_only_education_is_reconstructed_from_typography_for_now() -> None:
    assert STYLE_CONTINUATION_SECTION_TYPES == frozenset({SectionType.EDUCATION})
    # And it is a subset of what the spacing rule already covers, so v4 never
    # reaches a section v3 had ruled out.
    assert STYLE_CONTINUATION_SECTION_TYPES <= LAYOUT_CONTINUATION_SECTION_TYPES


def test_the_same_geometry_in_a_projects_section_is_left_alone() -> None:
    """`PROJECTS` is inside the spacing rule's scope and outside this one.

    The run is written exactly like the merging EDUCATION one; only the heading
    differs. It stays one entry per line, which is what makes the scope a real
    restriction rather than a comment.
    """
    projects = [
        ("PROJETS", OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (FIRST_ENTRY_REMAINDER, CONTINUATION, BODY_SIZE, WRAP_STEP),
        (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(TEACHING_PREAMBLE + projects))

    assert [
        candidate.raw_text
        for candidate in result.of_type(CandidateType.PROJECT_ENTRY)
    ] == [FIRST_ENTRY_OPENING, FIRST_ENTRY_REMAINDER, SECOND_ENTRY]


# --------------------------------------------------------------------------
# J. The rule is deterministic, and it never displaces the ones above it
# --------------------------------------------------------------------------


def test_the_same_document_always_yields_the_same_cut() -> None:
    document = build_synthetic_pdf([placed(PREAMBLE + EDUCATION_A_B_A)])

    first = extract_candidates(parse_cv_bytes(document))
    second = extract_candidates(parse_cv_bytes(document))

    assert first == second
    assert first.parser_version == PARSER_VERSION == "cv-parser-v4"
    assert first.extractor_version == CANDIDATE_EXTRACTOR_VERSION == "cv-candidates-v5"


def test_a_list_marker_still_wins_over_the_typography() -> None:
    """Rule 2 comes first: an explicit separator is never second-guessed.

    The typography here says the second line continues the first — the run is
    the merging one, face for face and baseline for baseline. The document says
    otherwise by opening that line with a list marker, and the document wins:
    the body is cut by the bullet rule, and its name says so.
    """
    bulleted = [
        (HEADING_EDUCATION, OPENER, HEADING_SIZE, BLOCK_STEP),
        (FIRST_ENTRY_OPENING, OPENER, BODY_SIZE, HEADING_STEP),
        (f"- {FIRST_ENTRY_REMAINDER}", CONTINUATION, BODY_SIZE, WRAP_STEP),
        (SECOND_ENTRY, OPENER, BODY_SIZE, ENTRY_STEP),
    ]

    result = extract_of(placed(TEACHING_PREAMBLE + bulleted))

    assert rules_of(result) == {ExtractionRule.SECTION_BULLET_BLOCK.value}
    # The first line keeps its own block, and the marked line opens the next.
    assert education_of(result) == [
        FIRST_ENTRY_OPENING,
        f"- {FIRST_ENTRY_REMAINDER}\n{SECOND_ENTRY}",
    ]
