"""Deterministic, lexicon-based detection of CV sections.

A line becomes a section heading only when it satisfies every structural rule
below **and** its folded form is listed verbatim in `SECTION_HEADINGS`. There is
no model, no scoring, no fuzzy match and no substring match, so the reason a
line was or was not treated as a heading is always readable in this file.

The consequence is deliberate: a heading this lexicon does not know is not a
boundary. Its lines stay inside the section that precedes them and no category
is invented for them. The parser would rather under-segment an unusual CV than
claim a section it cannot justify.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from services.digital_twin.cv.models import (
    DetectedSection,
    ExtractedPage,
    ParserWarning,
    SectionType,
    WarningCode,
)

#: A heading is a short label. A line longer than this is prose, whatever it
#: says, and is never promoted to a heading.
MAX_HEADING_LENGTH = 60
#: Decoration a CV puts around a heading label: bullets, rules, and the colon
#: that follows it. It holds no letter and no digit, so a numbered heading such
#: as "1. Formation" keeps its number and is therefore not recognised as a
#: heading. That is intentional: numbering is content, not decoration.
_HEADING_DECORATION = " \t:：.-–—_=~*#>•·▪◦"

_HEADINGS_BY_TYPE: Mapping[SectionType, tuple[str, ...]] = {
    SectionType.PROFILE: (
        "profile", "profil", "profil professionnel", "professional profile",
        "summary", "professional summary", "career summary", "resume",
        "objective", "career objective", "objectif", "objectifs",
        "objectif professionnel", "about", "about me", "a propos",
        "a propos de moi", "presentation", "personal statement",
    ),
    SectionType.EDUCATION: (
        "education", "education and training", "academic background",
        "academic education", "formation", "formations",
        "formation academique", "formations academiques", "parcours academique",
        "etudes", "diplomes", "diplomes et formations", "scolarite",
    ),
    SectionType.EXPERIENCE: (
        "experience", "experiences", "work experience", "work experiences",
        "professional experience", "professional experiences",
        "work history", "employment history", "professional background",
        "experience professionnelle", "experiences professionnelles",
        "parcours professionnel", "internships", "stages",
        "stages et experiences", "experience professionnelle et stages",
    ),
    SectionType.PROJECTS: (
        "project", "projects", "personal projects", "academic projects",
        "selected projects", "side projects", "projet", "projets",
        "projets personnels", "projets academiques", "projets selectionnes",
        "portfolio", "realisations",
    ),
    SectionType.SKILLS: (
        "skills", "key skills", "core skills", "technical skills",
        "technical skills and tools", "hard skills", "soft skills",
        "technologies", "tools", "competence", "competences",
        "competences techniques", "competences cles", "outils",
        "outils et technologies", "savoir faire",
    ),
    SectionType.CERTIFICATIONS: (
        "certification", "certifications", "professional certifications",
        "licenses and certifications", "certifications and licenses",
        "certificat", "certificats", "certifications professionnelles",
    ),
    SectionType.LANGUAGES: (
        "language", "languages", "language skills", "spoken languages",
        "langue", "langues", "langues parlees", "competences linguistiques",
    ),
}


def _build_lexicon() -> Mapping[str, SectionType]:
    """Flatten the lexicon and refuse a folded label claimed by two types.

    The check runs at import: an ambiguous entry is a bug in this file, not a
    condition a CV can trigger, and must never resolve silently by ordering.
    """
    lexicon: dict[str, SectionType] = {}
    for section_type, headings in _HEADINGS_BY_TYPE.items():
        for heading in headings:
            existing = lexicon.get(heading)
            if existing is not None and existing is not section_type:
                raise ValueError(
                    f"heading {heading!r} is claimed by both "
                    f"{existing.value} and {section_type.value}"
                )
            lexicon[heading] = section_type
    return lexicon


#: Folded heading label -> canonical section type. The single source of truth.
SECTION_HEADINGS: Mapping[str, SectionType] = _build_lexicon()


def fold_heading(line: str) -> str:
    """Return the comparison form of a candidate heading line.

    Case, accents, surrounding decoration and repeated spaces are exactly the
    variations a CV applies to the same label — "COMPÉTENCES", "Compétences :",
    "• competences" — so they are folded away before the lookup. Nothing else
    is: the words themselves are compared verbatim.
    """
    decomposed = unicodedata.normalize("NFKD", line)
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    stripped = without_accents.strip(_HEADING_DECORATION)
    lowered = stripped.lower().replace("’", " ").replace("'", " ")
    spelled = lowered.replace("&", " and ")
    return " ".join(spelled.split())


def classify_heading(line: str) -> SectionType | None:
    """Return the canonical type this line introduces, or `None`.

    Rules, in order: a heading is at most `MAX_HEADING_LENGTH` characters, it
    holds no sentence punctuation, and its folded form is listed verbatim in
    `SECTION_HEADINGS`.
    """
    if len(line) > MAX_HEADING_LENGTH:
        return None
    if any(character in line for character in ",;!?"):
        return None
    folded = fold_heading(line)
    if not folded:
        return None
    return SECTION_HEADINGS.get(folded)


@dataclass(frozen=True)
class SourceLine:
    """One normalized line together with the page it was extracted from."""

    text: str
    page_number: int


def document_lines(pages: Iterable[ExtractedPage]) -> tuple[SourceLine, ...]:
    """Flatten pages into one line stream that remembers its provenance."""
    lines: list[SourceLine] = []
    for page in pages:
        if page.is_empty:
            continue
        for text in page.text.split("\n"):
            lines.append(SourceLine(text=text, page_number=page.page_number))
    return tuple(lines)


def _trim(lines: Sequence[SourceLine]) -> tuple[SourceLine, ...]:
    """Drop the blank lines that only separate a run from its neighbours."""
    start, end = 0, len(lines)
    while start < end and not lines[start].text:
        start += 1
    while end > start and not lines[end - 1].text:
        end -= 1
    return tuple(lines[start:end])


def _content(lines: Sequence[SourceLine]) -> str:
    return "\n".join(line.text for line in lines)


def _pages_of(
    lines: Sequence[SourceLine], *, heading_page: int | None
) -> tuple[int, ...]:
    """Return every page the run covers, ascending, heading page included."""
    numbers = {line.page_number for line in lines}
    if heading_page is not None:
        numbers.add(heading_page)
    return tuple(sorted(numbers))


@dataclass(frozen=True)
class SectionSegment:
    """One detected section, with the page of every line it holds.

    This is the segmentation as `segment_document` computes it. A
    `DetectedSection` is this same run of lines joined into one string, which
    loses which page each line came from; a caller that needs that provenance —
    the Phase 3.2B candidate extractor does — reads it here instead of
    re-deriving the boundaries with a second, competing implementation.
    """

    section_type: SectionType
    #: The heading line that opened the section; `None` for the leading
    #: `UNCLASSIFIED` block, which no heading introduced.
    heading_line: SourceLine | None
    body: tuple[SourceLine, ...]

    @property
    def heading_page(self) -> int | None:
        return None if self.heading_line is None else self.heading_line.page_number

    def as_section(self) -> DetectedSection:
        """Return the flattened view this segment stands for."""
        return DetectedSection(
            section_type=self.section_type,
            heading_text=(
                None if self.heading_line is None else self.heading_line.text
            ),
            heading_page=self.heading_page,
            content=_content(self.body),
            page_numbers=_pages_of(self.body, heading_page=self.heading_page),
        )


def detect_sections(
    pages: Sequence[ExtractedPage],
) -> tuple[tuple[DetectedSection, ...], tuple[ParserWarning, ...]]:
    """Return the sections of `pages`, flattened, and the parse warnings.

    The segmentation itself lives in `segment_document`; this is the view the
    `ParsedCv` carries.
    """
    segments, warnings = segment_document(pages)
    return tuple(segment.as_section() for segment in segments), warnings


def segment_document(
    pages: Sequence[ExtractedPage],
) -> tuple[tuple[SectionSegment, ...], tuple[ParserWarning, ...]]:
    """Split the document on recognised headings, in document order.

    Text appearing before the first recognised heading — a CV's name and
    contact block, usually — is kept as one `UNCLASSIFIED` section with a
    warning: the parser holds on to the content without naming it. A heading
    repeated later in the document opens its own section rather than being
    merged into the earlier one, because merging would move CV text away from
    the page it was written on.
    """
    lines = document_lines(pages)
    warnings: list[ParserWarning] = []

    boundaries: list[tuple[int, SectionType]] = []
    for index, line in enumerate(lines):
        section_type = classify_heading(line.text)
        if section_type is not None:
            boundaries.append((index, section_type))
    if not boundaries:
        warnings.append(
            ParserWarning(
                code=WarningCode.NO_SECTION_HEADING_DETECTED,
                message=(
                    "no heading of the French/English lexicon was recognised; "
                    "the document is kept whole and unclassified"
                ),
            )
        )

    segments: list[SectionSegment] = []
    leading_end = boundaries[0][0] if boundaries else len(lines)
    leading = _trim(lines[:leading_end])
    if leading:
        segments.append(
            SectionSegment(
                section_type=SectionType.UNCLASSIFIED,
                heading_line=None,
                body=leading,
            )
        )
        if boundaries:
            warnings.append(
                ParserWarning(
                    code=WarningCode.UNCLASSIFIED_LEADING_CONTENT,
                    message=(
                        "content appears before the first recognised heading "
                        "and is kept without a category"
                    ),
                    page_number=leading[0].page_number,
                    section_type=SectionType.UNCLASSIFIED,
                )
            )

    seen: set[SectionType] = set()
    for position, (index, section_type) in enumerate(boundaries):
        following = position + 1
        end = boundaries[following][0] if following < len(boundaries) else len(lines)
        heading_line = lines[index]
        body = _trim(lines[index + 1 : end])
        segments.append(
            SectionSegment(
                section_type=section_type,
                heading_line=heading_line,
                body=body,
            )
        )
        if section_type in seen:
            warnings.append(
                ParserWarning(
                    code=WarningCode.REPEATED_SECTION_TYPE,
                    message=(
                        "this canonical section type is introduced more than "
                        "once; every occurrence is kept separately"
                    ),
                    page_number=heading_line.page_number,
                    section_type=section_type,
                )
            )
        seen.add(section_type)
        if not body:
            warnings.append(
                ParserWarning(
                    code=WarningCode.EMPTY_SECTION_CONTENT,
                    message="the recognised heading is followed by no content",
                    page_number=heading_line.page_number,
                    section_type=section_type,
                )
            )

    return tuple(segments), tuple(warnings)
