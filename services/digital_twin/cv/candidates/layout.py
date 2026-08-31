"""Reading one visual block out of the physical layout of the document.

A CV writes an entry as a block of text with a right-hand boundary. When the
entry is longer than that boundary the layout engine breaks it and carries the
rest onto the next baseline, and the text layer of the PDF then holds two lines
where the document shows one entry. Nothing in the *text* of those two lines
says they belong together — the whole point of this module is that nothing has
to. The question it answers is:

    do these two lines sit where a layout engine puts a line and its
    continuation, rather than where it puts two separate entries?

That question is about positions on a page, so the answer is computed from
positions on a page: the baseline distance between the two lines, their left
edges, their font sizes and the page they are on. It reads no word, matches no
keyword, knows no institution, employer, diploma, date, place or level, and
would answer identically if every letter of the document were replaced by
another. A rule that "understood" the words would be Phase 3.4 wearing this
module's name.

Self-calibration, and why it is the safety property
---------------------------------------------------

Absolute distances mean nothing on their own: a CV set at 9 points and one set
at 14 points put their lines at completely different distances, and both are
ordinary. So no distance is compared against a constant. The document is asked
what *its own* spacings are, and continuation is recognised only where the
document itself demonstrates two distinguishable ones — a tight baseline step,
which is what a line and its continuation get, and at least one step clearly
looser than that, which is what separate blocks get. A document whose lines are
all evenly spaced demonstrates no such distinction, and then this module reports
no continuation anywhere: without the distinction, "continuation" and "next
entry" look exactly alike, and guessing between them is the one thing that must
not happen. The same holds for the second condition, the one that asks whether
the first line was *full*: the right-hand boundary it compares against is the
one the document's own lines describe, and where too few lines describe it,
there is no evidence and no continuation.

Widths, and what "width" honestly means here
-------------------------------------------

The second condition needs to know how far a line reaches, and a PDF's text
layer does not say. A real advance width would need the glyph metrics of every
font the line uses — `/Widths` and `/FirstChar` for a simple font, `/W` and
`/DW` for a CID font, the Adobe core metrics for a standard-14 font naming no
widths at all — plus the character codes those widths are indexed by, which are
exactly what decoding the string to Unicode threw away, plus the horizontal
scaling and character and word spacing in force at the time. `pypdf` computes
all of that internally and exposes none of it: its `visitor_text` callback,
the only public route to a line's text state, hands over the matrices, the font
resource dictionary and the size, and no width. Reconstructing the rest would
mean inventing the parts the public API does not state, which is the one thing
this package refuses to do.

So no width here is measured. What is used instead is a proxy, `line_width`:
a line's character count carried into the size that line is set at, in units of
*character-points*. It is not the physical width of the line and is never
described as one. What it is, is the smallest correction that makes the
comparison well-posed: a count of characters is not a length at all, and
comparing one line's count with another's when the two are set at different
sizes compares two different units under one name. Multiplying by the size does
not make the proxy true, but it makes it *dimensionally honest* and monotone in
the two things that actually move a line's reach — more characters reach
further, and the same characters set larger reach further.

What the proxy still cannot see is the shape of the letters: eight narrow
glyphs and eight wide ones are the same eight characters to it, and in a
proportional font they are not the same width. That error is bounded and its
direction is the safe one, for the same reason as before: the boundary is the
*widest observed* line of the column, so a column whose widest line is unusually
narrow-lettered reads as reaching further than it does, and a genuine
continuation then fails this test and is left unmerged. The error that would
matter — reading a column as narrower than it is, and merging two real entries —
needs the *candidate* line to be the narrow-lettered one relative to a column
described by wide-lettered lines, and even then it must survive every other
condition below. This test is one of several, never the whole answer.

The failure mode this is written against is merging two real entries into one,
because that silently deletes an entry a human would have reviewed. Failing to
merge a wrapped entry is visible, correctable in review, and therefore the
direction every threshold here leans.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from services.digital_twin.cv.models import LineLayout, SectionType
from services.digital_twin.cv.sections import SourceLine

#: Two left edges this far apart in PDF points are the same left edge. A layout
#: engine returns a continuation to the very same margin, so the tolerance only
#: has to absorb the rounding a PDF producer writes into its coordinates.
X_EPSILON = 1.0
#: Two font sizes this close in PDF points are the same size.
FONT_SIZE_EPSILON = 0.5
#: Two baseline steps this close in PDF points are the same step.
GAP_EPSILON = 0.5
#: How many comparable baseline steps the document must show before its tightest
#: one is read as its line-to-line leading. Two steps can both be accidents of
#: one block; a leading is something the document repeats.
MIN_GAP_EVIDENCE_PAIRS = 3
#: How much looser than the tightest step another step must be for the document
#: to count as distinguishing "next line" from "next block". Below this the two
#: spacings are the same spacing and no continuation is recognised anywhere.
LOOSE_GAP_FACTOR = 1.25
#: A ceiling on the baseline step of a continuation, as a multiple of the font
#: size. A pair further apart than this is not a line and its continuation
#: whatever the rest of the document does, so this bounds what a pathological
#: calibration can authorise.
MAX_CONTINUATION_LEADING_RATIO = 3.0
#: How many lines must share a left edge and a font size before the widest of
#: them is read as describing that column's right-hand boundary.
MIN_MARGIN_EVIDENCE_LINES = 3

#: The sections whose entries this primitive is allowed to reconstruct.
#:
#: These are the sections a CV writes as blocks of prose-length text — an
#: institution and a diploma, an employer and a role, a project and what it did,
#: a certification and its issuer — which is what makes them long enough to be
#: broken by the right-hand boundary in the first place, and what makes the
#: break visible in the layout when it happens.
#:
#: `LANGUAGES` is deliberately absent. Its entries are short by nature, so they
#: are not wrapped and there is nothing to reconstruct; what the primitive could
#: still do there is merge two languages listed on adjacent baselines into one
#: entry, which is a loss with no matching gain. `PROFILE` and `SKILLS` are
#: absent because neither is read as a list of entries at all.
LAYOUT_CONTINUATION_SECTION_TYPES = frozenset(
    {
        SectionType.EDUCATION,
        SectionType.EXPERIENCE,
        SectionType.PROJECTS,
        SectionType.CERTIFICATIONS,
        SectionType.PROFESSIONAL_DEVELOPMENT,
    }
)

__all__ = [
    "FONT_SIZE_EPSILON",
    "GAP_EPSILON",
    "LAYOUT_CONTINUATION_SECTION_TYPES",
    "LOOSE_GAP_FACTOR",
    "MAX_CONTINUATION_LEADING_RATIO",
    "MIN_GAP_EVIDENCE_PAIRS",
    "MIN_MARGIN_EVIDENCE_LINES",
    "X_EPSILON",
    "DocumentLayoutEvidence",
    "PlacedLine",
    "comparable_gap",
    "continues_previous_line",
    "line_width",
    "read_layout_evidence",
    "reaches_right_boundary",
    "right_boundary",
    "same_column",
]


def line_width(layout: LineLayout, length: int) -> float | None:
    """Return how far `length` characters reach when set at `layout`'s size.

    The unit is *character-points*: a count of characters multiplied by the PDF
    point size the characters are set at. It is a proxy for physical width, not
    a measured one — see the module note on what that means and does not mean —
    and the whole of this module measures every distance in it, so that no two
    quantities are ever compared across units.

    `None` whenever the size the PDF stated cannot carry a width: a size that is
    zero, negative or not a finite number is not a size a line was set at, and
    the honest reading of such a line is that its width is unknown. Nothing is
    substituted for it: a line with no width describes no boundary, and a line
    with no width is never shown to have reached one. A negative `length` is the
    same absence — a caller cannot ask how far minus three characters reach.
    """
    if length < 0:
        return None
    size = layout.font_size
    if not math.isfinite(size) or size <= 0:
        return None
    return length * size


@dataclass(frozen=True)
class PlacedLine:
    """One line of the document that carries a layout, its length and its width."""

    page_number: int
    layout: LineLayout
    #: Number of characters of the normalized line. It is the only thing this
    #: module ever reads off a line's text — a count, never a word.
    length: int
    #: How far the line reaches, in the character-points of `line_width`: the
    #: count above, carried into the size the line is actually set at, so that
    #: it can be compared with the reach of a line set at another size. `None`
    #: when the PDF stated no size a width could be derived from.
    width: float | None


@dataclass(frozen=True)
class DocumentLayoutEvidence:
    """What the document's own layout demonstrates about its spacing.

    `tight_gap` is `None` when the document demonstrates nothing usable, and
    `continues_previous_line` then answers `False` for every pair. That is the
    conservative state and it is reached by every document read from text alone,
    every document with no layout facts, and every evenly spaced document.
    """

    #: The tightest baseline step the document repeats between two comparable
    #: lines, in PDF points; `None` when the document does not distinguish that
    #: step from a looser one.
    tight_gap: float | None
    #: Every line of the document that carries a layout. Used to ask what the
    #: right-hand boundary of a given column is.
    placed_lines: tuple[PlacedLine, ...]

    @property
    def distinguishes_spacings(self) -> bool:
        return self.tight_gap is not None

    def summary(self) -> dict[str, object]:
        """The privacy-safe view: geometry and counts, never a character of text."""
        return {
            "distinguishes_spacings": self.distinguishes_spacings,
            "tight_gap": self.tight_gap,
            "placed_line_count": len(self.placed_lines),
        }


def same_column(left: LineLayout, right: LineLayout) -> bool:
    """True when two lines start at the same left edge, at the same size."""
    return (
        abs(left.x_start - right.x_start) <= X_EPSILON
        and abs(left.font_size - right.font_size) <= FONT_SIZE_EPSILON
    )


def comparable_gap(previous: SourceLine, line: SourceLine) -> float | None:
    """Return the baseline step from `previous` down to `line`, or `None`.

    A step is comparable only between two lines of the same page that start in
    the same column at the same size and run down the page. Anything else — a
    page break, a size change, a different column, a line placed above the one
    before it — is not a step this module can read, and says so.
    """
    if previous.layout is None or line.layout is None:
        return None
    if previous.page_number != line.page_number:
        return None
    if not same_column(previous.layout, line.layout):
        return None
    gap = previous.layout.y - line.layout.y
    return gap if gap > 0 else None


def read_layout_evidence(
    lines: Sequence[SourceLine],
) -> DocumentLayoutEvidence:
    """Ask the whole document what its line spacing and its columns are.

    The document is the right scope: a section holding one wrapped entry shows a
    single baseline step, which on its own says nothing, while the CV around it
    shows that step many times over and shows the looser step that separates its
    blocks. Reading both from the whole document is also what makes the answer
    independent of which section is being cut.
    """
    gaps: list[float] = []
    for previous, line in zip(lines, lines[1:], strict=False):
        gap = comparable_gap(previous, line)
        if gap is not None:
            gaps.append(gap)
    placed = tuple(
        PlacedLine(
            page_number=line.page_number,
            layout=line.layout,
            length=len(line.text),
            width=line_width(line.layout, len(line.text)),
        )
        for line in lines
        if line.text and line.layout is not None
    )
    if len(gaps) < MIN_GAP_EVIDENCE_PAIRS:
        return DocumentLayoutEvidence(tight_gap=None, placed_lines=placed)
    tight = min(gaps)
    if not any(gap > tight * LOOSE_GAP_FACTOR + GAP_EPSILON for gap in gaps):
        return DocumentLayoutEvidence(tight_gap=None, placed_lines=placed)
    return DocumentLayoutEvidence(tight_gap=tight, placed_lines=placed)


def right_boundary(
    evidence: DocumentLayoutEvidence, layout: LineLayout
) -> float | None:
    """Return how far the column of `layout` is seen to reach, or `None`.

    The value is the widest line the document places in that column, in the
    character-points of `line_width`: a boundary the document describes rather
    than one this module assumes. `None` when too few lines describe it.

    Column identity and width are two different questions, and this is where
    they meet. `same_column` answers the first — two lines returning to the same
    left edge at what the document treats as one size are lines of one column —
    and it deliberately tolerates a small size difference, because a producer's
    rounding and a document's own hairline size changes both land inside it.
    That tolerance is right for identity and wrong for capacity: 132 characters
    set a half-point smaller do not reach as far as 132 characters set at the
    column's size, and reading both as "132" would let the smaller line describe
    a boundary the column does not have. So the cohort stays exactly as it was —
    no line is dropped for being set slightly smaller, and a smaller line that
    genuinely reaches furthest still sets the boundary — and what changed is the
    unit each of its members is measured in. A line whose width is unknown
    describes nothing and is left out; if that leaves too few, there is no
    boundary rather than a boundary drawn from the rest.
    """
    widths = [
        placed.width
        for placed in evidence.placed_lines
        if placed.width is not None and same_column(placed.layout, layout)
    ]
    if len(widths) < MIN_MARGIN_EVIDENCE_LINES:
        return None
    return max(widths)


def _first_word_length(text: str) -> int:
    """Length of the first space-separated token, `0` when there is none.

    This looks at where the spaces are and nowhere else. What the token says is
    never read, compared or classified.
    """
    for token in text.split(" "):
        if token:
            return len(token)
    return 0


def reaches_right_boundary(
    evidence: DocumentLayoutEvidence, previous: SourceLine, line: SourceLine
) -> bool:
    """True when the first token of `line` could not have fitted on `previous`.

    This is the reason a layout engine breaks a line: what came next did not
    fit. So a line that was broken by wrapping must reach far enough that the
    next line's first token, plus the space before it, would have run past the
    column's boundary. A line that stops short of that was not broken by
    wrapping — the document ended it there on purpose — and is therefore the end
    of its block, whatever the line below it happens to say.

    All four quantities are in the character-points of `line_width`, and they
    are in it for the same reason: the question is counterfactual — *had* the
    token stayed on `previous`, how far would that line have reached? — so the
    line it would have been set on is the one whose size measures it. `previous`
    text, the separating space and the following token are therefore all carried
    into `previous.layout`'s size in a single call, and compared against a
    boundary carried into each of its own lines' sizes. Nothing here is a raw
    character count, and no two sides of the comparison are in different units.
    """
    if previous.layout is None:
        return False
    boundary = right_boundary(evidence, previous.layout)
    if boundary is None:
        return False
    following = _first_word_length(line.text)
    if not following:
        return False
    required = line_width(previous.layout, len(previous.text) + 1 + following)
    if required is None:
        return False
    return required > boundary


def continues_previous_line(
    evidence: DocumentLayoutEvidence,
    previous: SourceLine,
    line: SourceLine,
) -> bool:
    """True when the layout shows `line` carrying `previous` on, in one block.

    Every condition below must hold, and each one is a statement about the
    physical page:

    1. the document distinguishes a tight baseline step from a looser one, so
       "next line" and "next block" are telling apart at all;
    2. both lines carry text and a layout read off the PDF;
    3. they are on the same page — two baselines on different pages have no
       distance to compare, so a block is never carried across a page break;
    4. they are set at the same font size;
    5. they start at the same left edge — a continuation returns to the margin
       of the line it continues, and a line starting anywhere else is not read
       as one;
    6. the step between their baselines is the document's tightest, and is
       within `MAX_CONTINUATION_LEADING_RATIO` of the font size: the two lines
       are adjacent baselines with no block spacing between them;
    7. the first line reaches its column's right-hand boundary, which is what
       being broken by wrapping means.

    Any condition failing gives `False`, which the caller reads as "keep these
    lines apart" — the same answer it gave before this module existed.
    """
    if not evidence.distinguishes_spacings or evidence.tight_gap is None:
        return False
    if not previous.text or not line.text:
        return False
    if previous.layout is None or line.layout is None:
        return False
    gap = comparable_gap(previous, line)
    if gap is None:
        return False
    if gap > evidence.tight_gap + GAP_EPSILON:
        return False
    if gap > line.layout.font_size * MAX_CONTINUATION_LEADING_RATIO:
        return False
    return reaches_right_boundary(evidence, previous, line)
