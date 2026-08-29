"""Deterministic segmentation of a recognised section into entries.

The sections a CV writes as a list — education, experience, projects,
certifications, languages — are cut into blocks on the separator the section
actually uses, and each block is kept with the text as written and the pages it
covers. That is all this module does.

It states nothing about what a block *means*: no institution, employer, role,
date, duration, diploma level or language level is derived, normalized or
canonicalised here. Those readings belong to Phase 3.4, and inventing them from
a block of text would be exactly the assertion this phase refuses to make.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from services.digital_twin.cv.candidates.models import CandidateType, ExtractionRule
from services.digital_twin.cv.candidates.text import starts_with_bullet
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
    detail — belongs to the block opened by the last boundary above it, in
    order, with its text untouched.
    """
    blocks: list[tuple[SourceLine, ...]] = []
    current: list[SourceLine] = []
    for line in body:
        if not line.text:
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


def segment_entries(
    body: Sequence[SourceLine],
    *,
    section_type: SectionType,
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
    3. otherwise one entry per line, which asserts nothing beyond what the
       document's own line breaks already say.

    Rule 0 exists because a bulleted `EXPERIENCE` section can open its second
    entry on a line that is not a bullet, which rules 1-3 have no way to see:
    that line would join the bullet above it and the block would then hold two
    entries at once. Its preconditions are what keep it from firing anywhere
    else, and a body that does not satisfy both is cut exactly as before.
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
    return ExtractionRule.SECTION_LINE_BLOCK, tuple(_line_blocks(body))


def block_text(block: Sequence[SourceLine]) -> str:
    """Return the block exactly as the CV wrote it, line breaks kept."""
    return "\n".join(line.text for line in block)


def block_pages(block: Sequence[SourceLine]) -> tuple[int, ...]:
    """Return every page the block covers, ascending."""
    return tuple(sorted({line.page_number for line in block}))
