"""Typed, immutable result vocabulary of the CV PDF parser.

Every value here is either read straight out of the PDF or derived from it by a
rule written in this package. Nothing is inferred, completed or guessed, and no
value depends on the current time: the same file parsed twice with the same
`PARSER_VERSION` produces the same result, field for field.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Bumped whenever extraction, normalization or heading rules change, so a
#: stored result can always be compared against the rules that produced it.
PARSER_VERSION = "cv-parser-v1"


class SectionType(StrEnum):
    """The canonical categories the heading lexicon is allowed to assign.

    `UNCLASSIFIED` is not a category: it marks content the parser kept but
    refused to name, which is the only honest answer for text that appears
    before any heading it recognises.
    """

    PROFILE = "PROFILE"
    EDUCATION = "EDUCATION"
    EXPERIENCE = "EXPERIENCE"
    PROJECTS = "PROJECTS"
    SKILLS = "SKILLS"
    CERTIFICATIONS = "CERTIFICATIONS"
    LANGUAGES = "LANGUAGES"
    UNCLASSIFIED = "UNCLASSIFIED"


class WarningCode(StrEnum):
    """Deterministic diagnostics. A warning states a fact about the parse."""

    #: A page of the PDF carried no extractable text once normalized.
    EMPTY_PAGE = "EMPTY_PAGE"
    #: No heading of the lexicon matched anywhere in the document.
    NO_SECTION_HEADING_DETECTED = "NO_SECTION_HEADING_DETECTED"
    #: Text was found before the first recognised heading and was kept as
    #: `UNCLASSIFIED` rather than attached to a category.
    UNCLASSIFIED_LEADING_CONTENT = "UNCLASSIFIED_LEADING_CONTENT"
    #: The same canonical section type was introduced by more than one heading.
    #: Both occurrences are kept separately, in document order.
    REPEATED_SECTION_TYPE = "REPEATED_SECTION_TYPE"
    #: A recognised heading was followed by no content before the next heading.
    EMPTY_SECTION_CONTENT = "EMPTY_SECTION_CONTENT"


@dataclass(frozen=True)
class ExtractedPage:
    """One page of the PDF, after conservative text normalization."""

    #: 1-based, in document order.
    page_number: int
    text: str

    @property
    def is_empty(self) -> bool:
        return not self.text


@dataclass(frozen=True)
class ParserWarning:
    """One diagnostic. Its `message` never quotes CV content."""

    code: WarningCode
    message: str
    page_number: int | None = None
    section_type: SectionType | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "page_number": self.page_number,
            "section_type": (
                None if self.section_type is None else self.section_type.value
            ),
        }


@dataclass(frozen=True)
class DetectedSection:
    """A run of lines introduced by one recognised heading.

    `heading_text` is the heading exactly as the CV wrote it, kept so a reader
    can check the classification against the original. It is `None` for the
    `UNCLASSIFIED` block, which no heading introduced.
    """

    section_type: SectionType
    heading_text: str | None
    #: Page carrying the heading; `None` for the `UNCLASSIFIED` block.
    heading_page: int | None
    content: str
    #: Every page this section spans, ascending, heading included. Enough to go
    #: back to the PDF and read the section in its original context.
    page_numbers: tuple[int, ...]

    @property
    def is_empty(self) -> bool:
        return not self.content

    def as_dict(self) -> dict[str, object]:
        """The full section, CV text included. Never printed by default."""
        return {
            "section_type": self.section_type.value,
            "heading_text": self.heading_text,
            "heading_page": self.heading_page,
            "content": self.content,
            "page_numbers": list(self.page_numbers),
        }


@dataclass(frozen=True)
class ParsedCv:
    """The whole deterministic result of parsing one PDF."""

    parser_version: str
    #: SHA-256 of the PDF bytes, so a result can be tied to the exact file.
    content_sha256: str
    page_count: int
    pages: tuple[ExtractedPage, ...]
    sections: tuple[DetectedSection, ...]
    warnings: tuple[ParserWarning, ...]

    @property
    def short_sha256(self) -> str:
        return self.content_sha256[:12]

    @property
    def section_types(self) -> tuple[SectionType, ...]:
        """Canonical types in document order, repetitions kept."""
        return tuple(section.section_type for section in self.sections)

    def summary(self) -> dict[str, object]:
        """The privacy-safe view: shape of the document, never its content.

        No page text, no section content, no heading as written, and therefore
        no name, address, email or phone number.
        """
        return {
            "parser_version": self.parser_version,
            "sha256": self.content_sha256,
            "short_sha256": self.short_sha256,
            "page_count": self.page_count,
            "empty_page_numbers": [
                page.page_number for page in self.pages if page.is_empty
            ],
            "section_types": [
                section_type.value for section_type in self.section_types
            ],
            "warnings": [warning.as_dict() for warning in self.warnings],
        }

    def as_dict(self) -> dict[str, object]:
        """The detailed view, including the CV text.

        Only ever written where the operator explicitly asked for it.
        """
        detailed = self.summary()
        detailed["pages"] = [
            {"page_number": page.page_number, "text": page.text} for page in self.pages
        ]
        detailed["sections"] = [section.as_dict() for section in self.sections]
        return detailed
