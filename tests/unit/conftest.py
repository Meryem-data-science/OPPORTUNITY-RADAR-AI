"""Synthetic PDF builder shared by the CV parser unit tests.

Every PDF the tests read is built here, in memory, from invented text. No real
CV, no real person and no real contact detail enters the test suite or the git
history.

A page is given either as plain strings — evenly spaced lines at one size, the
shape every test written before layout facts existed uses — or as `PlacedText`
values, which state where each line sits, how big it is and which of the page's
fonts it is set in. The plain form emits exactly the content stream it always
did, byte for byte, so the PDFs those tests hash and parse are unchanged.

Every page carries the same small closed set of font resources, so a test that
needs two lines set in *different* typefaces names a different one rather than
inventing a font dictionary of its own. The typefaces are the base-14 names any
PDF reader knows; nothing here depends on what they look like, only on the fact
that the PDF gives them different `/BaseFont` names.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

#: Font resource name -> the `/BaseFont` the page's font dictionary gives it.
#: `F1` is first and is what every line uses unless a test says otherwise, so
#: the documents written before this mattered are set exactly as they were.
#: `F4` is deliberately declared with no `/BaseFont` at all: it is how a test
#: describes a document whose typography the PDF does not state.
FONT_BASE_NAMES: dict[str, str | None] = {
    "F1": "/Helvetica",
    "F2": "/Helvetica-Bold",
    "F3": "/Times-Roman",
    "F4": None,
}

_PAGE_HEADER = (
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    "/Resources << /Font << {fonts} >> >> /Contents {contents} 0 R >>"
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
    #: The page font resource `text` is set in, a key of `FONT_BASE_NAMES`.
    #: This is the font the line *starts* in, since `text` is emitted first.
    font: str = "F1"
    #: Further `(text, font)` runs set on the same baseline, after `text`. A
    #: test describing a line whose typeface changes part-way through writes
    #: the change here; the line's text is the concatenation of everything.
    runs: tuple[tuple[str, str], ...] = ()

    @property
    def full_text(self) -> str:
        """The whole line as a reader sees it, every run concatenated."""
        return self.text + "".join(run_text for run_text, _ in self.runs)


def _number(value: float) -> bytes:
    """Render a coordinate the way a PDF producer would, without exponents."""
    return f"{value:.4f}".rstrip("0").rstrip(".").encode("ascii")


def _placed_content_stream(lines: Sequence[PlacedText]) -> bytes:
    """Emit one `Tm` per line, and one `Tf`/`Tj` pair per run of that line.

    A line with several runs is written the way a real producer writes a line
    whose typeface changes part-way: the baseline is set once, and each run
    selects its font and shows its own string. The reader therefore sees the
    line's *first* run set in the line's own font, which is what a rule reading
    "the typeface this line starts in" has to be held to.
    """
    parts = [b"BT"]
    for line in lines:
        operands = [_number(value) for value in line.matrix]
        operands += [_number(line.x), _number(line.y)]
        placed = False
        for text, font in ((line.text, line.font), *line.runs):
            parts.append(
                b"/" + font.encode("ascii") + b" " + _number(line.font_size) + b" Tf"
            )
            if not placed:
                parts.append(b" ".join(operands) + b" Tm")
                placed = True
            parts.append(b"(" + _escape(text) + b") Tj")
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
    first_font_number = 3 + 2 * count
    font_numbers = {
        name: first_font_number + offset
        for offset, name in enumerate(FONT_BASE_NAMES)
    }
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(count))
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii"),
    }
    for name, base_font in FONT_BASE_NAMES.items():
        # A font declared with no `/BaseFont` is a font the PDF names nowhere,
        # which is exactly the document that states no typography at all.
        named = f" /BaseFont {base_font}" if base_font is not None else ""
        bodies[font_numbers[name]] = (
            f"<< /Type /Font /Subtype /Type1{named} /Encoding /WinAnsiEncoding >>"
        ).encode("ascii")
    fonts = " ".join(
        f"/{name} {number} 0 R" for name, number in font_numbers.items()
    )
    for index, lines in enumerate(pages):
        page_number = 3 + 2 * index
        contents_number = page_number + 1
        bodies[page_number] = _PAGE_HEADER.format(
            fonts=fonts, contents=contents_number
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
