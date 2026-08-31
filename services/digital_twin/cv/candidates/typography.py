"""Reading an entry boundary from the typeface a column opens its entries in.

`layout.py` recognises a wrapped entry by its *spacing*: it asks the document
for its own tight line-to-line step and its own looser block step, and joins two
lines only where the step between them is the tight one. That works whenever the
document puts visibly more room between two entries than inside one.

Some documents do not. A CV can set its entries one under the other at a step so
close to its line-to-line leading that the two are not distinguishable — the
block step and the wrap step differ by a fraction of a point — and then
`layout.py` correctly refuses to conclude anything, because from spacing alone
"next line" and "next entry" genuinely look alike. Every such line becomes its
own entry, and a wrapped entry is silently read as two.

There is a second physical signal in the PDF for exactly that case. A document
that lays its entries out this way usually *opens* each one in one typeface and
sets the wrapped remainder in another, and the PDF states which font rendered
each run of glyphs. So this module asks a different question:

    does this column open its entries in one particular typeface, repeatedly,
    and is this line set in a different one?

What "one particular typeface" is, is never assumed. It is read off the document,
by looking for the repetition that demonstrates it — see `_demonstrated_opener`.
Nothing here decides that a signature means bold, heading, emphasis, title,
regular or body: a signature is an opaque token that came out of the PDF, and the
only operation performed on two of them is `==`. A document that opens its
entries in its *lighter* face and wraps into its heavier one is read exactly as
correctly as the other way round, because neither case is written down anywhere.

Not a word is read. As in `layout.py`, the one thing taken off a line's text is
how many characters it holds, which stands in for how far it reaches; replacing
every word of the document with another word of the same length changes no
decision this module makes.

Why the typeface alone is never enough
--------------------------------------

"The font changed, so this is a continuation" would be far too permissive: a CV
sets a date, a grade or a place in a different face all the time, on lines that
are perfectly separate entries. So the typeface is one condition among several,
and every other one is the physical evidence `layout.py` already established:
the two lines are on the same page, in the same column, at the same size, on
baselines close enough to be adjacent, and — the condition that does most of the
work — the upper line reaches its column's right-hand boundary, which is what
being broken by wrapping means. A line that stopped short of that boundary was
ended by the document on purpose, and no typeface changes that.

The failure mode written against is the same one, and so is the direction every
rule leans: merging two real entries silently deletes one, while failing to
merge a wrapped entry stays visible in review. Where the evidence is short,
incomplete, contradictory or simply absent, this module reports no continuation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.digital_twin.cv.candidates.layout import (
    MAX_CONTINUATION_LEADING_RATIO,
    DocumentLayoutEvidence,
    comparable_gap,
    reaches_right_boundary,
    same_column,
)
from services.digital_twin.cv.models import LineLayout, SectionType
from services.digital_twin.cv.sections import SourceLine

#: How many times a column must be seen opening on a signature before that
#: signature is read as the one it opens its entries in. Two is the smallest
#: number that can be a repetition rather than a single occurrence, and it is
#: not on its own enough — see `_demonstrated_opener` for the second half of
#: the condition, which is what rules out reading `A B` as "A opens".
MIN_OPENER_OCCURRENCES = 2

#: The sections whose entries this primitive is allowed to reconstruct.
#:
#: `EDUCATION` only, and deliberately so. It is the section whose entries are
#: reliably written as one self-contained block per diploma — an institution,
#: what was studied, when — which is what makes "this column opens an entry
#: here" a meaningful thing for a typeface to mark, and what makes a wrap the
#: only reason for a second line to sit tight under the first.
#:
#: The other sections of `LAYOUT_CONTINUATION_SECTION_TYPES` are left out until
#: tests justify them, because each has a shape that could turn this rule into a
#: false merge rather than a recovered one:
#:
#: * `EXPERIENCE` and `PROJECTS` routinely spread one entry over several
#:   deliberate lines — a role, then the employer, then the dates, then the
#:   detail — set in alternating faces on purpose. Those lines are separate
#:   lines the document meant to write, and telling them apart from a wrap needs
#:   evidence this module does not have. Both sections also already have earlier
#:   rules that fire first on the layouts they normally use: the pipe-delimited
#:   rule for `EXPERIENCE`, and the bullet rule for the bulleted detail both use.
#: * `CERTIFICATIONS` writes short entries that do not reach a right-hand
#:   boundary and therefore do not wrap; what this rule could still do there is
#:   merge a certification with the issuer line under it, which loses an entry
#:   and recovers none.
#: * `LANGUAGES` is absent from the spacing rule too, for the same reason: its
#:   entries are short, and a language and its level are very often set in two
#:   faces on two lines.
STYLE_CONTINUATION_SECTION_TYPES = frozenset({SectionType.EDUCATION})

__all__ = [
    "MIN_OPENER_OCCURRENCES",
    "STYLE_CONTINUATION_SECTION_TYPES",
    "ColumnOpenerSignature",
    "DocumentStyleEvidence",
    "continues_previous_line_in_style",
    "read_style_evidence",
]


@dataclass(frozen=True)
class ColumnOpenerSignature:
    """One column, and the typographic signature it opens its entries in."""

    #: A line of that column, kept so another line can be tested against it
    #: with `same_column` rather than against a rounded key.
    layout: LineLayout
    #: The signature the column's runs were seen to open on, verbatim from the
    #: PDF. Never `None`: a column that demonstrated nothing gets no record.
    signature: str


@dataclass(frozen=True)
class DocumentStyleEvidence:
    """Which of its typefaces each column of the document opens entries in.

    `openers` is empty whenever the document demonstrates nothing usable, and
    `continues_previous_line_in_style` then answers `False` for every pair. That
    is the conservative state, and it is where every document read from text
    alone lands, every document whose PDF states no `/BaseFont`, and every
    document that never repeats an opening typeface.
    """

    openers: tuple[ColumnOpenerSignature, ...]

    @property
    def demonstrates_openers(self) -> bool:
        return bool(self.openers)

    def opener_for(self, layout: LineLayout) -> str | None:
        """Return the signature the column of `layout` opens entries in.

        `None` when no column matched, and equally when more than one matched
        with signatures that disagree: two columns close enough to be the same
        column, saying different things, demonstrate nothing between them.
        """
        matched = {
            opener.signature
            for opener in self.openers
            if same_column(opener.layout, layout)
        }
        if len(matched) != 1:
            return None
        return next(iter(matched))

    def summary(self) -> dict[str, object]:
        """The privacy-safe view: counts and geometry, never a line of text.

        The signatures themselves are left out. A `/BaseFont` name is technical
        and carries no personal data, but it is still something the document
        wrote, and nothing here needs it to describe the shape of the evidence.
        """
        return {
            "demonstrates_openers": self.demonstrates_openers,
            "opener_column_count": len(self.openers),
        }


def _in_same_run(previous: SourceLine, line: SourceLine) -> bool:
    """True when `line` sits directly under `previous`, in the same column.

    This is what makes two lines part of one physical run: the same page — a
    run never crosses a page break, because two baselines on different pages
    have no distance between them — the same left edge and size, a step running
    down the page, and a step short enough to be adjacent. "Adjacent" is bounded
    by `MAX_CONTINUATION_LEADING_RATIO`, the same ceiling `layout.py` puts on a
    continuation: a pair further apart than a few times its own font size is not
    two lines of one block whatever else is true of them.
    """
    if previous.layout is None or line.layout is None:
        return False
    gap = comparable_gap(previous, line)
    if gap is None:
        return False
    return gap <= line.layout.font_size * MAX_CONTINUATION_LEADING_RATIO


def _column_runs(lines: Sequence[SourceLine]) -> list[tuple[SourceLine, ...]]:
    """Cut the document's line stream into maximal runs of one column.

    A run is a purely physical object: consecutive lines carrying text and a
    layout, each sitting directly under the one before it in the same column of
    the same page. Everything else — a blank line, a line with no layout, a size
    change, a column change, a page break, a step too large to be adjacent —
    ends the run and opens the next.
    """
    runs: list[tuple[SourceLine, ...]] = []
    current: list[SourceLine] = []
    for line in lines:
        if not line.text or line.layout is None:
            if current:
                runs.append(tuple(current))
                current = []
            continue
        if current and _in_same_run(current[-1], line):
            current.append(line)
            continue
        if current:
            runs.append(tuple(current))
        current = [line]
    if current:
        runs.append(tuple(current))
    return runs


def _demonstrated_opener(run: Sequence[SourceLine]) -> str | None:
    """Return the signature this run demonstrates it opens entries in.

    The candidate is the signature of the run's **first** line, and that choice
    is structural rather than a preference: a line the layout engine wrapped
    always has the line it continues above it, in the same run, so the first
    line of a run can never itself be a continuation. Whatever a run opens on is
    therefore an opening signature, and the only question left is whether the
    run *demonstrates* that it is the one this column uses for every entry.

    It demonstrates it when both of these hold:

    * the signature appears at least `MIN_OPENER_OCCURRENCES` times in the run,
      so it is something the column repeats rather than something it did once;
    * some line with a *different* signature sits strictly between two of those
      occurrences, so the run actually shows the alternation an opener and its
      continuation produce.

    Both halves are needed, and the second is what makes the rule safe:

        A B      one A. Two lines, two typefaces, and no way to tell a wrapped
                 entry from two entries set differently. Nothing demonstrated.
        A B C    one A again, and now three faces with no repetition at all.
        A A      two A's, but nothing between them: a column that opens twice on
                 the same face and never wraps. Nothing to conclude, and
                 nothing that needs concluding.
        B B      the same, whatever the face happens to be called.
        A B A    two A's with a B between them. This is the smallest structure
                 that shows a column opening, carrying on in another face, and
                 opening again — and it is the only one of these that is read.

    A run whose first line has no signature, or that holds any line whose
    signature the PDF did not state, demonstrates nothing: a sequence with a
    hole in it cannot show an alternation. That is what makes the whole
    mechanism inert on a document whose typography the PDF does not state.
    """
    signatures = [line.layout.start_font_signature for line in run if line.layout]
    if len(signatures) != len(run) or any(
        signature is None for signature in signatures
    ):
        return None
    opener = signatures[0]
    positions = [
        index for index, signature in enumerate(signatures) if signature == opener
    ]
    if len(positions) < MIN_OPENER_OCCURRENCES:
        return None
    inner = range(positions[0] + 1, positions[-1])
    if not any(signatures[index] != opener for index in inner):
        return None
    return opener


def read_style_evidence(lines: Sequence[SourceLine]) -> DocumentStyleEvidence:
    """Ask the whole document which typeface each of its columns opens on.

    The document is the right scope, for the same reason it is in `layout.py`:
    one section holding one wrapped entry shows too little to demonstrate
    anything, while the CV around it shows the same column opening entry after
    entry. Reading it once for the whole document also keeps the answer
    independent of where the section boundaries happened to fall.

    Runs are per page and per column; their conclusions are then pooled per
    column across the document, and a column is only recorded when every run
    that demonstrated something demonstrated the *same* signature. Two runs of
    one column disagreeing is a document this rule cannot read, and it is
    recorded as no evidence rather than resolved by counting.
    """
    demonstrated: dict[int, set[str]] = {}
    columns: list[LineLayout] = []
    for run in _column_runs(lines):
        opener = _demonstrated_opener(run)
        if opener is None:
            continue
        layout = run[0].layout
        if layout is None:  # pragma: no cover - a run's lines all carry layout
            continue
        for index, known in enumerate(columns):
            if same_column(known, layout):
                demonstrated[index].add(opener)
                break
        else:
            columns.append(layout)
            demonstrated[len(columns) - 1] = {opener}
    openers = tuple(
        ColumnOpenerSignature(layout=columns[index], signature=next(iter(signatures)))
        for index, signatures in sorted(demonstrated.items())
        if len(signatures) == 1
    )
    return DocumentStyleEvidence(openers=openers)


def continues_previous_line_in_style(
    style_evidence: DocumentStyleEvidence,
    layout_evidence: DocumentLayoutEvidence,
    block_opener: SourceLine,
    previous: SourceLine,
    line: SourceLine,
) -> bool:
    """True when the typography shows `line` carrying its block on.

    `block_opener` is the first line of the block `line` would join, `previous`
    the last line already in it. Every condition below must hold, and each one
    is a statement about the physical page:

    1. all three lines carry text and a layout read off the PDF;
    2. `previous` and `line` are on the same page, in the same column, at the
       same size, on baselines close enough to be adjacent — the run test, so
       a page break, a column change, a size change or a large vertical space
       each end the block;
    3. the column demonstrates, from its own repetition, which typeface it
       opens entries in;
    4. `block_opener` is set in that typeface: the block being carried on was
       itself opened by an opener, so a run that starts on anything else never
       accumulates lines;
    5. `line` is set in a *different* typeface, so the document is not showing
       the start of the next entry;
    6. `previous` reaches its column's right-hand boundary, which is what being
       broken by wrapping means, and is what keeps a short line the document
       ended on purpose from swallowing the entry below it.

    Any condition failing gives `False`, which the caller reads as "keep these
    lines apart" — the same answer it gave before this module existed.
    """
    if not style_evidence.demonstrates_openers:
        return False
    if not block_opener.text or not previous.text or not line.text:
        return False
    if (
        block_opener.layout is None
        or previous.layout is None
        or line.layout is None
    ):
        return False
    if not _in_same_run(previous, line):
        return False
    opener = style_evidence.opener_for(line.layout)
    if opener is None:
        return False
    if block_opener.layout.start_font_signature != opener:
        return False
    signature = line.layout.start_font_signature
    if signature is None or signature == opener:
        return False
    return reaches_right_boundary(layout_evidence, previous, line)
