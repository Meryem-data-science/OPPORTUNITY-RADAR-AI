"""The Phase 3.2A entry point: one local PDF in, one deterministic result out.

The parse is a fixed pipeline — read the bytes, hash them, extract each page's
text layer, normalize it conservatively, split on recognised headings — and
every step is a pure function of the file and of `PARSER_VERSION`. Nothing is
read from the clock, the environment or the network, so the same PDF parsed
twice yields identical `ParsedCv` values.

What comes out is a description of the document, not knowledge about the
person: no fact is validated, nothing is written to the Digital Twin, and an
ambiguity stays an ambiguity, reported as a warning.
"""

from __future__ import annotations

from pathlib import Path

from services.collector.logging_config import get_logger
from services.digital_twin.cv.models import (
    PARSER_VERSION,
    ParsedCv,
    ParserWarning,
    WarningCode,
)
from services.digital_twin.cv.pdf import content_sha256, extract_pages, read_pdf_bytes
from services.digital_twin.cv.sections import detect_sections

LOGGER_NAME = "services.collector.digital_twin.cv.parser"


def parse_cv_bytes(content: bytes) -> ParsedCv:
    """Parse an in-memory PDF. Raises a `PdfExtractionError` subclass on failure."""
    pages = extract_pages(content)
    sections, section_warnings = detect_sections(pages)
    empty_page_warnings = tuple(
        ParserWarning(
            code=WarningCode.EMPTY_PAGE,
            message="the page carries no extractable text",
            page_number=page.page_number,
        )
        for page in pages
        if page.is_empty
    )
    return ParsedCv(
        parser_version=PARSER_VERSION,
        content_sha256=content_sha256(content),
        page_count=len(pages),
        pages=pages,
        sections=sections,
        warnings=empty_page_warnings + section_warnings,
    )


def parse_cv_pdf(path: str | Path) -> ParsedCv:
    """Parse a local CV PDF at `path`.

    The structured event this emits carries counts, canonical section types and
    warning codes only. The CV text, the headings as written, the file path and
    any contact detail stay out of the logs.
    """
    logger = get_logger(LOGGER_NAME)
    result = parse_cv_bytes(read_pdf_bytes(Path(path)))
    logger.info(
        "Parsed a local CV PDF.",
        extra={
            "event": "cv_pdf_parsed",
            "parser_version": result.parser_version,
            "cv_sha256": result.content_sha256,
            "page_count": result.page_count,
            "section_types": [
                section_type.value for section_type in result.section_types
            ],
            "warning_codes": [warning.code.value for warning in result.warnings],
        },
    )
    return result
