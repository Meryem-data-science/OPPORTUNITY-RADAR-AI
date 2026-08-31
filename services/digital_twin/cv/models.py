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
#: The text layer is read by `pypdf`, so its pinned version in `pyproject.toml`
#: is part of those rules: changing that pin requires deciding whether the
#: extracted text can differ and therefore whether this version must move too.
PARSER_VERSION = "cv-parser-v3"


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
class LineLayout:
    """Where one extracted line physically sits on its page.

    Every number is read straight off the PDF's own text state, in PDF points,
    with the page's coordinate system as its frame: `x_start` and `y` are the
    device-space position of the line's first glyph origin, and `font_size` is
    the size that glyph was set at. Nothing is derived, estimated or completed —
    in particular there is no `x_end`, because the width of a line cannot be
    read off the text layer without the font's glyph metrics, and a made-up
    width would be exactly the invented value this package refuses to produce.

    A line only carries this when the PDF states it unambiguously: the text is
    upright and unrotated, and the transformation is a plain translation and
    scale. Anything else leaves `ExtractedLine.layout` at `None`, which reads as
    "the document did not say", never as a default position.
    """

    #: Device-space x of the line's first glyph origin, in PDF points. Larger
    #: means further right. Comparable between lines of the same page only.
    x_start: float
    #: Device-space y of the line's baseline, in PDF points. Larger means
    #: higher on the page, so a following line normally has a smaller `y`.
    y: float
    #: Effective font size of the line's first glyph, in PDF points.
    font_size: float

    def as_dict(self) -> dict[str, object]:
        return {"x_start": self.x_start, "y": self.y, "font_size": self.font_size}


@dataclass(frozen=True)
class ExtractedLine:
    """One line of a page's normalized text, with its layout when the PDF said it.

    `text` is the same string as the matching line of `ExtractedPage.text`; the
    page number is the one the enclosing `ExtractedPage` carries, and is
    deliberately not repeated here, so the two can never disagree.
    """

    text: str
    layout: LineLayout | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "layout": None if self.layout is None else self.layout.as_dict(),
        }


@dataclass(frozen=True)
class ExtractedPage:
    """One page of the PDF, after conservative text normalization.

    `text` is the whole page as a single string and remains the page's primary
    representation: it is what the heading lexicon, the sections and every
    caller written before layout facts existed read. `lines` is the same text
    split on its line breaks: one entry per line of `text`, in that order,
    each carrying where that line physically sat when the PDF stated it. The
    two are consistent by construction — every entry's `text` is the matching
    line of the page's `text`, and the two hold the same number of lines — and
    `lines` is left empty rather than guessed at when that correspondence
    cannot be established, so a caller may always fall back to `text` alone.
    """

    #: 1-based, in document order.
    page_number: int
    text: str
    #: Empty when the page carries no text, and equally when the layout could
    #: not be read: absence here means "no structural evidence", never "no
    #: line".
    lines: tuple[ExtractedLine, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def has_layout(self) -> bool:
        """True when every line of this page carries its layout facts."""
        return bool(self.lines) and all(
            line.layout is not None for line in self.lines if line.text
        )


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
            "layout_page_numbers": [
                page.page_number for page in self.pages if page.has_layout
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
            {
                "page_number": page.page_number,
                "text": page.text,
                "lines": [line.as_dict() for line in page.lines],
            }
            for page in self.pages
        ]
        detailed["sections"] = [section.as_dict() for section in self.sections]
        return detailed
