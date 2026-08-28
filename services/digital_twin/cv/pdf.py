"""Local PDF text extraction for the CV parser.

`pypdf` reads the text layer the PDF already carries; nothing is uploaded, no
remote service is called and no model is asked to guess. Every failure mode is
raised as its own explicit error: a caller learns which one it hit, and no
unreadable document is ever silently turned into an empty CV.

There is no OCR in this slice. A scanned CV — a page image with no text layer —
raises `EmptyPdfTextError` instead of producing invented content.
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from services.digital_twin.cv.models import ExtractedPage
from services.digital_twin.cv.normalization import normalize_text


class PdfExtractionError(RuntimeError):
    """Base error for a PDF this slice cannot read into pages of text."""


class PdfFileNotFoundError(PdfExtractionError):
    """Raised when the given path does not exist."""


class PdfNotAFileError(PdfExtractionError):
    """Raised when the given path exists but is a directory or a device."""


class InvalidPdfError(PdfExtractionError):
    """Raised when the bytes are not a PDF, or are a structurally broken one."""


class EncryptedPdfError(PdfExtractionError):
    """Raised when the PDF is encrypted.

    The parser does not try passwords and does not attempt an empty-password
    decryption: an encrypted CV is refused explicitly rather than half-read.
    """


class EmptyPdfTextError(PdfExtractionError):
    """Raised when the PDF is readable but carries no extractable text.

    This is what a scanned CV looks like from here. OCR is out of scope for
    `cv-parser-v1`, so the parser reports the absence instead of inventing one.
    """


def read_pdf_bytes(path: Path) -> bytes:
    """Return the raw bytes of a local PDF file.

    The path is never interpolated into an error message: a CV filename usually
    carries the person's name, and this package keeps personal data out of
    messages, logs and tracebacks.
    """
    if not path.exists():
        raise PdfFileNotFoundError("the given CV path does not exist")
    if not path.is_file():
        raise PdfNotAFileError("the given CV path is not a regular file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise PdfExtractionError(
            f"the CV file could not be read: {type(error).__name__}"
        ) from error
    if not content:
        raise InvalidPdfError("the CV file is empty")
    return content


def content_sha256(content: bytes) -> str:
    """Return the SHA-256 of the PDF bytes, the identity of the parsed file."""
    return hashlib.sha256(content).hexdigest()


def extract_pages(content: bytes) -> tuple[ExtractedPage, ...]:
    """Return one normalized `ExtractedPage` per page, in document order.

    A page whose text layer is empty yields an empty page rather than being
    dropped, so page numbers stay the real page numbers of the PDF. A document
    where *every* page is empty raises `EmptyPdfTextError`.
    """
    try:
        reader = PdfReader(BytesIO(content))
    except (PyPdfError, ValueError) as error:
        raise InvalidPdfError(
            f"the file is not a readable PDF: {type(error).__name__}"
        ) from error
    if reader.is_encrypted:
        raise EncryptedPdfError(
            "the PDF is encrypted; decrypt it locally before parsing it"
        )
    try:
        page_objects = list(reader.pages)
    except (PyPdfError, ValueError) as error:
        raise InvalidPdfError(
            f"the PDF page tree could not be read: {type(error).__name__}"
        ) from error
    if not page_objects:
        raise InvalidPdfError("the PDF contains no page")

    pages: list[ExtractedPage] = []
    for page_number, page in enumerate(page_objects, start=1):
        try:
            raw_text = page.extract_text() or ""
        except (PyPdfError, DependencyError, ValueError) as error:
            raise InvalidPdfError(
                f"text extraction failed on page {page_number}: "
                f"{type(error).__name__}"
            ) from error
        pages.append(
            ExtractedPage(page_number=page_number, text=normalize_text(raw_text))
        )

    if all(page.is_empty for page in pages):
        raise EmptyPdfTextError(
            "the PDF carries no extractable text; a scanned CV needs OCR, "
            "which cv-parser-v1 does not perform"
        )
    return tuple(pages)
