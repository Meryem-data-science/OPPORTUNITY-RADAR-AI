"""Synthetic PDF builder shared by the CV parser unit tests.

Every PDF the tests read is built here, in memory, from invented text. No real
CV, no real person and no real contact detail enters the test suite or the git
history.

A page is given either as plain strings — evenly spaced lines at one size, the
shape every test written before layout facts existed uses — or as `PlacedText`
values, which state where each line sits and how big it is. The plain form emits
exactly the content stream it always did, byte for byte, so the PDFs those tests
hash and parse are unchanged.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

_PAGE_HEADER = (
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    "/Resources << /Font << /F1 {font} 0 R >> >> /Contents {contents} 0 R >>"
)


def _escape(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("cp1252", errors="replace")


@dataclass(frozen=True)
class PlacedText:
    """One line of a synthetic page, placed where the test says it goes.

    The coordinates are the PDF's own: `x` grows to the right, `y` grows
    upwards, both in points, and `font_size` is the size the line is set at. A
    test that cares about the physical layout of a document writes it here
    rather than describing it in prose.
    """

    text: str
    x: float = 72.0
    y: float = 760.0
    font_size: float = 12.0
    #: The `a b c d` of the line's text matrix, defaulting to the identity. A
    #: test needing a rotated, skewed or mirrored line writes it here; nothing
    #: else has any reason to.
    matrix: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 1.0)


def _number(value: float) -> bytes:
    """Render a coordinate the way a PDF producer would, without exponents."""
    return f"{value:.4f}".rstrip("0").rstrip(".").encode("ascii")


def _placed_content_stream(lines: Sequence[PlacedText]) -> bytes:
    parts = [b"BT"]
    for line in lines:
        parts.append(b"/F1 " + _number(line.font_size) + b" Tf")
        operands = [_number(value) for value in line.matrix]
        operands += [_number(line.x), _number(line.y)]
        parts.append(b" ".join(operands) + b" Tm")
        parts.append(b"(" + _escape(line.text) + b") Tj")
    parts.append(b"ET")
    return b"\n".join(parts)


def _content_stream(lines: Sequence[str] | Sequence[PlacedText]) -> bytes:
    if not lines:
        return b""
    if all(isinstance(line, PlacedText) for line in lines):
        return _placed_content_stream(lines)
    parts = [b"BT", b"/F1 12 Tf", b"72 760 Td", b"16 TL"]
    for line in lines:
        parts.append(b"(" + _escape(line) + b") Tj")
        parts.append(b"T*")
    parts.append(b"ET")
    return b"\n".join(parts)


def build_synthetic_pdf(
    pages: Sequence[Sequence[str]] | Sequence[Sequence[PlacedText]],
) -> bytes:
    """Return a minimal uncompressed PDF, one text line per given entry.

    A page given as an empty sequence gets an empty content stream, which is
    how a page with no text layer reads to any extractor. A page given as
    `PlacedText` values is laid out exactly where they say.
    """
    count = len(pages)
    font_number = 3 + 2 * count
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(count))
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii"),
        font_number: (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"
        ),
    }
    for index, lines in enumerate(pages):
        page_number = 3 + 2 * index
        contents_number = page_number + 1
        bodies[page_number] = _PAGE_HEADER.format(
            font=font_number, contents=contents_number
        ).encode("ascii")
        stream = _content_stream(lines)
        bodies[contents_number] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    document = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(bodies):
        offsets[number] = len(document)
        document += f"{number} 0 obj\n".encode("ascii") + bodies[number] + b"\nendobj\n"
    xref_offset = len(document)
    size = len(bodies) + 1
    document += f"xref\n0 {size}\n".encode("ascii") + b"0000000000 65535 f \n"
    for number in sorted(bodies):
        document += f"{offsets[number]:010d} 00000 n \n".encode("ascii")
    document += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(document)


@pytest.fixture()
def synthetic_pdf():
    """Return the builder above, so a test can describe its own PDF inline."""
    return build_synthetic_pdf
