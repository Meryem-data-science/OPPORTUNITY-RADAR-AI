"""Synthetic PDF builder shared by the CV parser unit tests.

Every PDF the tests read is built here, in memory, from invented text. No real
CV, no real person and no real contact detail enters the test suite or the git
history.
"""

from collections.abc import Sequence

import pytest

_PAGE_HEADER = (
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    "/Resources << /Font << /F1 {font} 0 R >> >> /Contents {contents} 0 R >>"
)


def _escape(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return escaped.encode("cp1252", errors="replace")


def _content_stream(lines: Sequence[str]) -> bytes:
    if not lines:
        return b""
    parts = [b"BT", b"/F1 12 Tf", b"72 760 Td", b"16 TL"]
    for line in lines:
        parts.append(b"(" + _escape(line) + b") Tj")
        parts.append(b"T*")
    parts.append(b"ET")
    return b"\n".join(parts)


def build_synthetic_pdf(pages: Sequence[Sequence[str]]) -> bytes:
    """Return a minimal uncompressed PDF, one text line per given string.

    A page given as an empty sequence gets an empty content stream, which is
    how a page with no text layer reads to any extractor.
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
