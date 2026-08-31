"""Local PDF text extraction for the CV parser.

`pypdf` reads the text layer the PDF already carries; nothing is uploaded, no
remote service is called and no model is asked to guess. Every failure mode is
raised as its own explicit error: a caller learns which one it hit, and no
unreadable document is ever silently turned into an empty CV.

There is no OCR in this slice. A scanned CV — a page image with no text layer —
raises `EmptyPdfTextError` instead of producing invented content.

The text layer is read once, through `pypdf`'s `visitor_text` callback. That
callback is what `pypdf` calls each time it flushes a line of output, and it
hands over the text state that produced it — the current transformation matrix,
the text matrix and the font size — so the extraction gets both the string
`extract_text()` returns and where each of its lines physically sat, from a
single pass over one document. Nothing is inferred from the geometry here: this
module records it and stops.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError

from services.digital_twin.cv.models import (
    ExtractedLine,
    ExtractedPage,
    LineLayout,
)
from services.digital_twin.cv.normalization import (
    kept_line_indexes,
    normalize_line,
    normalize_text,
    split_raw_lines,
)


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
    `cv-parser-v3`, so the parser reports the absence instead of inventing one.
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


#: A matrix component this close to zero is zero. PDF producers write exact
#: zeros for an upright, unrotated line; anything larger is a real rotation or
#: skew, and a rotated line's x/y are not comparable with an upright one's.
_MATRIX_EPSILON = 1e-9


@dataclass(frozen=True)
class _TextChunk:
    """One `visitor_text` call: the text `pypdf` flushed, and where it started.

    `layout` is `None` when the text state was rotated, skewed or mirrored. That
    is not a failure — it is the honest reading of a line whose position cannot
    be compared with an upright one's — and it propagates as an absent layout
    rather than as a guessed position.
    """

    text: str
    layout: LineLayout | None


def _layout_of(
    cm: Sequence[float], tm: Sequence[float], font_size: float
) -> LineLayout | None:
    """Return the device-space position of a text-space origin, or `None`.

    The two matrices are composed, and the result is kept only when it is a
    plain translation and scale: a rotated or skewed line has no single "left
    edge" or "baseline" that could be compared with the lines around it, and a
    mirrored or degenerate scale has no reliable reading order, so both give
    `None`. What comes back is therefore always directly comparable with the
    other lines of the same page, or absent.
    """
    if len(cm) != 6 or len(tm) != 6:
        return None
    if any(abs(value) > _MATRIX_EPSILON for value in (cm[1], cm[2], tm[1], tm[2])):
        return None
    horizontal_scale, vertical_scale = cm[0] * tm[0], cm[3] * tm[3]
    if horizontal_scale <= 0 or vertical_scale <= 0:
        return None
    size = font_size * vertical_scale
    if size <= 0:
        return None
    return LineLayout(
        x_start=cm[0] * tm[4] + cm[4],
        y=cm[3] * tm[5] + cm[5],
        font_size=size,
    )


def _collector(record: Callable[[_TextChunk], None]) -> Callable[..., None]:
    """Return the `visitor_text` callback that records one chunk per call.

    `pypdf` calls it with the text it is flushing and the text state that
    produced it; the concatenation of those strings is what `extract_text()`
    returns. The callback therefore records the stream without interpreting it,
    and `_aligned_lines` is the only place that turns it into lines.
    """

    def visit(
        text: object,
        cm: object,
        tm: object,
        font_dictionary: object,
        font_size: object,
    ) -> None:
        if not isinstance(text, str) or not text:
            return
        layout: LineLayout | None = None
        if isinstance(cm, Sequence) and isinstance(tm, Sequence):
            try:
                layout = _layout_of(
                    [float(value) for value in cm],
                    [float(value) for value in tm],
                    float(font_size),  # type: ignore[arg-type]
                )
            except (TypeError, ValueError):
                # A text state this callback cannot read as numbers is a text
                # state with no position, which is exactly what `None` says.
                layout = None
        record(_TextChunk(text=text, layout=layout))

    return visit


def _aligned_lines(
    chunks: Sequence[_TextChunk], text: str
) -> tuple[ExtractedLine, ...]:
    """Return one `ExtractedLine` per line of `text`, or nothing at all.

    The chunks are cut on their line breaks and normalized through exactly the
    steps `normalize_text` applies, so the sequence that comes out should be the
    lines of `text`, position for position. That is then *checked*, line by
    line, and a single mismatch drops the whole page's layout: absent evidence
    is safe, and evidence attached to the wrong line is not. The check is what
    lets every reader downstream trust the correspondence without re-deriving
    it.

    A line's layout is the layout of the first chunk that contributed text to
    it, which is where the line starts. A chunk `pypdf` could not place leaves
    the lines it opened without layout.
    """
    if not text:
        return ()
    raw_texts: list[str] = []
    raw_layouts: list[LineLayout | None] = []
    pending = ""
    pending_layout: LineLayout | None = None
    started = False
    for chunk in chunks:
        pieces = split_raw_lines(chunk.text)
        for position, piece in enumerate(pieces):
            if position:
                raw_texts.append(pending)
                raw_layouts.append(pending_layout)
                pending, pending_layout, started = "", None, False
            if piece:
                pending += piece
                if not started:
                    pending_layout, started = chunk.layout, True
    raw_texts.append(pending)
    raw_layouts.append(pending_layout)

    normalized = [normalize_line(raw) for raw in raw_texts]
    kept = kept_line_indexes(normalized)
    expected = text.split("\n")
    if len(kept) != len(expected):
        return ()
    lines: list[ExtractedLine] = []
    for index, line_text in zip(kept, expected, strict=True):
        if normalized[index] != line_text:
            return ()
        lines.append(
            ExtractedLine(
                text=line_text,
                layout=raw_layouts[index] if line_text else None,
            )
        )
    return tuple(lines)


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
        chunks: list[_TextChunk] = []
        try:
            visitor = _collector(chunks.append)
            raw_text = page.extract_text(visitor_text=visitor) or ""
        except (PyPdfError, DependencyError, ValueError) as error:
            raise InvalidPdfError(
                f"text extraction failed on page {page_number}: "
                f"{type(error).__name__}"
            ) from error
        text = normalize_text(raw_text)
        pages.append(
            ExtractedPage(
                page_number=page_number,
                text=text,
                lines=_aligned_lines(chunks, text),
            )
        )

    if all(page.is_empty for page in pages):
        raise EmptyPdfTextError(
            "the PDF carries no extractable text; a scanned CV needs OCR, "
            "which cv-parser-v3 does not perform"
        )
    return tuple(pages)
