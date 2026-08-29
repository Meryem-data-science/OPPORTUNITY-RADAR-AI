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


def _line_blocks(body: Sequence[SourceLine]) -> list[tuple[SourceLine, ...]]:
    return [(line,) for line in body if line.text]


def segment_entries(
    body: Sequence[SourceLine],
) -> tuple[ExtractionRule, tuple[tuple[SourceLine, ...], ...]]:
    """Cut a section body into entries, and name the rule that cut it.

    One separator is chosen for the whole section, in a fixed order, so the
    same body is always cut the same way:

    1. a blank line, whenever the body holds one — the coarsest separator a CV
       writes, and the one that keeps a multi-line entry together;
    2. otherwise a list marker opening a line, whenever the body holds one;
    3. otherwise one entry per line, which asserts nothing beyond what the
       document's own line breaks already say.
    """
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
