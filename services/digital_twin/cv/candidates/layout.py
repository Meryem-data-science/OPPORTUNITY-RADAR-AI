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

The failure mode this is written against is merging two real entries into one,
because that silently deletes an entry a human would have reviewed. Failing to
merge a wrapped entry is visible, correctable in review, and therefore the
direction every threshold here leans.
"""

from __future__ import annotations

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
#: How many lines must share a left edge and a font size before the longest of
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
    "read_layout_evidence",
    "reaches_right_boundary",
    "right_boundary",
    "same_column",
]


@dataclass(frozen=True)
class PlacedLine:
    """One line of the document that carries a layout, and its length."""

    page_number: int
    layout: LineLayout
    #: Number of characters of the normalized line. It stands in for how far the
    #: line reaches, and it is the only thing this module ever reads off a line's
    #: text — a count, never a word. See `reaches_right_boundary`.
    length: int


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
) -> int | None:
    """Return how far the column of `layout` is seen to reach, or `None`.

    The unit is characters of normalized text, and the value is the longest line
    the document places in that column: a boundary the document describes rather
    than one this module assumes. `None` when too few lines describe it.

    Characters are a stand-in for width, and an imperfect one — a proportional
    font sets a hundred narrow letters in less space than a hundred wide ones —
    which is why the comparison in `reaches_right_boundary` is deliberately
    made against the *widest observed* line and why the whole test is only ever
    one of several conditions. Its error direction is the safe one: a column
    whose longest line is unusually narrow-lettered reads as reaching further
    than it does, and a genuine continuation then fails the test and is left
    unmerged.
    """
    lengths = [
        placed.length
        for placed in evidence.placed_lines
        if same_column(placed.layout, layout)
    ]
    if len(lengths) < MIN_MARGIN_EVIDENCE_LINES:
        return None
    return max(lengths)


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
    """
    if previous.layout is None:
        return False
    boundary = right_boundary(evidence, previous.layout)
    if boundary is None:
        return False
    following = _first_word_length(line.text)
    if not following:
        return False
    return len(previous.text) + 1 + following > boundary


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
