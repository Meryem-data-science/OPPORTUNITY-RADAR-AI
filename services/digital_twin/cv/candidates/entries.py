"""Deterministic segmentation of a recognised section into entries.

The sections a CV writes as a list — education, experience, projects,
certifications, languages — are cut into blocks on the separator the section
actually uses, and each block is kept with the text as written and the pages it
covers. That is all this module does.

Two of those separators are the physical layout of the page, read through
`layout.py` and `typography.py`: where the document shows that a line break was
the layout engine running out of room rather than the document ending an entry,
the two lines stay in one block. `layout.py` reads that from the document's own
spacing; `typography.py` reads it from the typeface the document's own columns
are seen to open their entries in, for the documents whose spacing cannot tell a
wrap from a boundary. Both are statements about the shape of the document and
not about its meaning — see those modules for why, and for what each refuses to
do when the page proves nothing.

It states nothing about what a block *means*: no institution, employer, role,
date, duration, diploma level or language level is derived, normalized or
canonicalised here. Those readings belong to Phase 3.4, and inventing them from
a block of text would be exactly the assertion this phase refuses to make.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from services.digital_twin.cv.candidates.layout import (
    LAYOUT_CONTINUATION_SECTION_TYPES,
    DocumentLayoutEvidence,
    continues_previous_line,
)
from services.digital_twin.cv.candidates.models import CandidateType, ExtractionRule
from services.digital_twin.cv.candidates.text import starts_with_bullet
from services.digital_twin.cv.candidates.typography import (
    STYLE_CONTINUATION_SECTION_TYPES,
    DocumentStyleEvidence,
    continues_previous_line_in_style,
)
from services.digital_twin.cv.models import SectionType
from services.digital_twin.cv.sections import SourceLine

#: The sections whose body is read as a list of entries, and the candidate type
#: each one produces. A section outside this table yields no entry candidate:
#: `PROFILE` is prose about the person and `SKILLS` has its own rule.
ENTRY_CANDIDATE_TYPES: Mapping[SectionType, CandidateType] = {
    SectionType.EDUCATION: CandidateType.EDUCATION_ENTRY,
    SectionType.EXPERIENCE: CandidateType.EXPERIENCE_ENTRY,
    SectionType.PROJECTS: CandidateType.PROJECT_ENTRY,
    SectionType.CERTIFICATIONS: CandidateType.CERTIFICATION_ENTRY,
    SectionType.LANGUAGES: CandidateType.LANGUAGE_ENTRY,
}

#: The literal character this module reads as an explicit part separator. It is
#: compared as itself: no other vertical bar, box-drawing character or dash is
#: folded into it.
PIPE_SEPARATOR = "|"
#: How many non-empty parts a line must hold to count as an explicit boundary.
#: Two parts are how an ordinary sentence happens to use the character; three
#: written parts are a structure the document put there on purpose.
MIN_PIPE_BOUNDARY_SEGMENTS = 3
#: How many boundary lines a body must hold before the boundaries are read as
#: the separator of the whole section. One such line separates nothing.
MIN_PIPE_BOUNDARY_LINES = 2


def is_pipe_boundary_line(text: str) -> bool:
    """Say whether the line is written as an explicit pipe-delimited line.

    This is a statement about the line's punctuation and nothing else: it is
    not blank, it does not open with a list marker, and splitting it on
    `PIPE_SEPARATOR` yields at least `MIN_PIPE_BOUNDARY_SEGMENTS` parts that
    all hold text. What those parts *are* is not read here — no employer, role,
    date, place, duration or seniority is derived from them by this rule or by
    anything downstream of it.
    """
    if not text or starts_with_bullet(text):
        return False
    segments = text.split(PIPE_SEPARATOR)
    if len(segments) < MIN_PIPE_BOUNDARY_SEGMENTS:
        return False
    return all(segment.strip() for segment in segments)


def _opens_on_pipe_boundaries(body: Sequence[SourceLine]) -> bool:
    """Say whether the body is written as a list of pipe-delimited entries.

    Both conditions are required, and deliberately narrow: the very first line
    of the body must already be a boundary — so a section whose entries are
    introduced some other way is never re-cut on a pipe line appearing inside
    one of them — and the body must hold at least `MIN_PIPE_BOUNDARY_LINES` of
    them, since a single one separates nothing from nothing.
    """
    written = [line for line in body if line.text]
    if not written or not is_pipe_boundary_line(written[0].text):
        return False
    boundaries = sum(1 for line in written if is_pipe_boundary_line(line.text))
    return boundaries >= MIN_PIPE_BOUNDARY_LINES


def _blank_line_blocks(body: Sequence[SourceLine]) -> list[tuple[SourceLine, ...]]:
    blocks: list[tuple[SourceLine, ...]] = []
    current: list[SourceLine] = []
    for line in body:
        if line.text:
            current.append(line)
        elif current:
            blocks.append(tuple(current))
            current = []
    if current:
        blocks.append(tuple(current))
    return blocks


def _bullet_blocks(body: Sequence[SourceLine]) -> list[tuple[SourceLine, ...]]:
    blocks: list[tuple[SourceLine, ...]] = []
    current: list[SourceLine] = []
    for line in body:
        if not line.text:
            continue
        if starts_with_bullet(line.text) and current:
            blocks.append(tuple(current))
            current = []
        current.append(line)
    if current:
        blocks.append(tuple(current))
    return blocks


def _pipe_delimited_blocks(
    body: Sequence[SourceLine],
) -> list[tuple[SourceLine, ...]]:
    """Cut the body on its boundary lines, everything else following its own.

    A line that is not a boundary — a bullet, a continuation line, an indented
    detail, a blank line the CV left between two of them — belongs to the block
    opened by the last boundary above it, in order, with its text untouched. A
    blank line is not a boundary and is not a separator here, so it is kept
    where the document wrote it instead of being dropped: only a boundary line
    ever opens a block. Blank lines before the first boundary are the exception
    and are skipped, since there is no block yet for them to belong to.
    """
    blocks: list[tuple[SourceLine, ...]] = []
    current: list[SourceLine] = []
    for line in body:
        if not current and not line.text:
            continue
        if is_pipe_boundary_line(line.text) and current:
            blocks.append(tuple(current))
            current = []
        current.append(line)
    if current:
        blocks.append(tuple(current))
    return blocks


def _line_blocks(body: Sequence[SourceLine]) -> list[tuple[SourceLine, ...]]:
    return [(line,) for line in body if line.text]


def _layout_continuation_blocks(
    body: Sequence[SourceLine], evidence: DocumentLayoutEvidence
) -> list[tuple[SourceLine, ...]]:
    """One block per line, except where the layout carries a line on.

    This is `_line_blocks` with one change: a line the document physically
    places as the continuation of the line above it joins that line's block
    instead of opening its own. The decision is `continues_previous_line`'s
    alone, and it is taken pair by pair, so an entry the document wraps over
    three or more lines stays one block for as long as each step is a
    continuation of the one before it.
    """
    blocks: list[list[SourceLine]] = []
    for line in body:
        if not line.text:
            continue
        if blocks and continues_previous_line(evidence, blocks[-1][-1], line):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return [tuple(block) for block in blocks]


def _style_continuation_blocks(
    body: Sequence[SourceLine],
    layout_evidence: DocumentLayoutEvidence,
    style_evidence: DocumentStyleEvidence,
) -> list[tuple[SourceLine, ...]]:
    """One block per line, except where the typography carries a line on.

    This is `_line_blocks` with one change, and it mirrors
    `_layout_continuation_blocks` exactly: a line the document's typography
    places as the continuation of the block above it joins that block instead of
    opening its own. The decision is `continues_previous_line_in_style`'s alone.

    The block's *first* line is passed along with its last, because the question
    that rule asks is whether the block being carried on was itself opened in
    the column's opening typeface. That is what lets an entry wrapped over three
    or more lines stay one block — each further line is another line set in a
    non-opening face under a full one — while a run that never opened on an
    opener never accumulates anything.
    """
    blocks: list[list[SourceLine]] = []
    for line in body:
        if not line.text:
            continue
        if blocks and continues_previous_line_in_style(
            style_evidence, layout_evidence, blocks[-1][0], blocks[-1][-1], line
        ):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return [tuple(block) for block in blocks]


def _has_style_continuation(
    body: Sequence[SourceLine],
    layout_evidence: DocumentLayoutEvidence,
    style_evidence: DocumentStyleEvidence,
) -> bool:
    """Say whether the typography carries any line of this body on.

    Asked before the rule is named, and computed by cutting the body rather
    than by scanning pairs: whether a line continues its block depends on the
    block it would join, so the only honest answer is the one the cut produces.
    A body the typography says nothing about keeps the rule — and the name — it
    had before this rule existed.
    """
    return any(
        len(block) > 1
        for block in _style_continuation_blocks(body, layout_evidence, style_evidence)
    )


def _has_layout_continuation(
    body: Sequence[SourceLine], evidence: DocumentLayoutEvidence
) -> bool:
    """Say whether the layout carries any line of this body on.

    Asked before the rule is named, so a body the layout says nothing about
    keeps the rule — and the name — it had before this rule existed.
    """
    written = [line for line in body if line.text]
    return any(
        continues_previous_line(evidence, previous, line)
        for previous, line in zip(written, written[1:], strict=False)
    )


def segment_entries(
    body: Sequence[SourceLine],
    *,
    section_type: SectionType,
    layout_evidence: DocumentLayoutEvidence | None = None,
    style_evidence: DocumentStyleEvidence | None = None,
) -> tuple[ExtractionRule, tuple[tuple[SourceLine, ...], ...]]:
    """Cut a section body into entries, and name the rule that cut it.

    One separator is chosen for the whole section, in a fixed order, so the
    same body is always cut the same way:

    0. in an `EXPERIENCE` section only, and only where the body both opens on
       an explicit pipe-delimited line and holds at least
       `MIN_PIPE_BOUNDARY_LINES` of them, those lines — a structure the
       document itself wrote — are the separator, and each one opens a block
       that runs until the next;
    1. otherwise a blank line, whenever the body holds one — the coarsest
       separator a CV writes, and the one that keeps a multi-line entry
       together;
    2. otherwise a list marker opening a line, whenever the body holds one;
    2.5 otherwise, in the sections of `LAYOUT_CONTINUATION_SECTION_TYPES` and
       only where the document's own spacing proves that a line is the physical
       continuation of the line above it, that line joins the block above
       instead of opening its own;
    2.6 otherwise, in the sections of `STYLE_CONTINUATION_SECTION_TYPES` and
       only where the document's own typography proves the same thing, that
       line joins the block above instead of opening its own;
    3. otherwise one entry per line, which asserts nothing beyond what the
       document's own line breaks already say.

    Rule 0 exists because a bulleted `EXPERIENCE` section can open its second
    entry on a line that is not a bullet, which rules 1-3 have no way to see:
    that line would join the bullet above it and the block would then hold two
    entries at once. Its preconditions are what keep it from firing anywhere
    else, and a body that does not satisfy both is cut exactly as before.

    Rules 2.5 and 2.6 are where the physical layout of the document is read, and
    they are placed there on purpose. A body separated by blank lines or by list
    markers already keeps a wrapped entry together, so rules 1 and 2 need no
    help and are left untouched; the body that needs help is the one with
    neither, whose only remaining separator was the line break itself. Where the
    page proves that a line break was the layout engine's and not the
    document's, the two lines stay in one block; where it proves nothing, rule 3
    answers exactly as it always did. Either evidence left at `None` — a caller
    that has not read the document's layout, a `ParsedCv` built from text alone
    — is that same "proves nothing", so the matching rule simply never fires.

    The two are separate rules, in that order, because they rest on different
    evidence and a reader of a candidate's provenance has to be able to tell
    which one merged its lines. Rule 2.5 concludes from the spacing the document
    demonstrates and is tried first: it is the older, better-established reading,
    and it is the one that applies wherever a document does put visibly more
    room between entries than inside one. Rule 2.6 exists for the documents
    where that reading is impossible — where the block step and the wrap step
    are the same step — and concludes instead from the typeface a column is seen
    to open its entries in. Where 2.5 already fires, nothing changes; a body is
    only ever cut by one of them.
    """
    if section_type is SectionType.EXPERIENCE and _opens_on_pipe_boundaries(body):
        return (
            ExtractionRule.EXPERIENCE_PIPE_DELIMITED_BLOCK,
            tuple(_pipe_delimited_blocks(body)),
        )
    if any(not line.text for line in body):
        return ExtractionRule.SECTION_BLANK_LINE_BLOCK, tuple(_blank_line_blocks(body))
    if any(starts_with_bullet(line.text) for line in body):
        return ExtractionRule.SECTION_BULLET_BLOCK, tuple(_bullet_blocks(body))
    if (
        layout_evidence is not None
        and section_type in LAYOUT_CONTINUATION_SECTION_TYPES
        and _has_layout_continuation(body, layout_evidence)
    ):
        return (
            ExtractionRule.SECTION_LAYOUT_CONTINUATION_BLOCK,
            tuple(_layout_continuation_blocks(body, layout_evidence)),
        )
    if (
        layout_evidence is not None
        and style_evidence is not None
        and section_type in STYLE_CONTINUATION_SECTION_TYPES
        and _has_style_continuation(body, layout_evidence, style_evidence)
    ):
        return (
            ExtractionRule.SECTION_LAYOUT_STYLE_CONTINUATION_BLOCK,
            tuple(_style_continuation_blocks(body, layout_evidence, style_evidence)),
        )
    return ExtractionRule.SECTION_LINE_BLOCK, tuple(_line_blocks(body))


def block_text(block: Sequence[SourceLine]) -> str:
    """Return the block exactly as the CV wrote it, line breaks kept."""
    return "\n".join(line.text for line in block)


def block_pages(block: Sequence[SourceLine]) -> tuple[int, ...]:
    """Return every page the block covers, ascending."""
    return tuple(sorted({line.page_number for line in block}))
