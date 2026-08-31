"""Unit coverage for the Phase 3.2A CV PDF parser.

Every PDF is built in memory from the invented content below. TEST ONLY: no
real CV, person, address or phone number appears here or in the fixtures.
"""

import hashlib
from io import BytesIO

import math

import pytest
from pypdf import PdfWriter

from services.digital_twin.cv.models import (
    PARSER_VERSION,
    ExtractedPage,
    SectionType,
    WarningCode,
)
from services.digital_twin.cv.parser import parse_cv_bytes, parse_cv_pdf
from services.digital_twin.cv.pdf import (
    EmptyPdfTextError,
    EncryptedPdfError,
    InvalidPdfError,
    PdfFileNotFoundError,
    PdfNotAFileError,
)
from tests.unit.conftest import PlacedText, build_synthetic_pdf

# TEST ONLY content; it describes nobody.
FIRST_PAGE = [
    "Jeanne Exemple",
    "jeanne@example.invalid",
    "PROFIL",
    "Etudiante en donnees et IA.",
    "FORMATION",
    "Master Data & IA - Universite Exemple",
]
SECOND_PAGE = [
    "EXPÉRIENCE PROFESSIONNELLE",
    "Stage analyste donnees",
    "Compétences :",
    "Python, SQL",
    "LANGUES",
    "Francais, Anglais",
]


def _write(tmp_path, content: bytes, name: str = "cv.pdf"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_a_two_page_cv_is_extracted_segmented_and_hashed(synthetic_pdf) -> None:
    content = synthetic_pdf([FIRST_PAGE, SECOND_PAGE])

    result = parse_cv_bytes(content)

    assert result.parser_version == PARSER_VERSION == "cv-parser-v4"
    assert result.content_sha256 == hashlib.sha256(content).hexdigest()
    assert result.page_count == 2
    assert [page.page_number for page in result.pages] == [1, 2]
    assert result.section_types == (
        SectionType.UNCLASSIFIED,
        SectionType.PROFILE,
        SectionType.EDUCATION,
        SectionType.EXPERIENCE,
        SectionType.SKILLS,
        SectionType.LANGUAGES,
    )
    experience = result.sections[3]
    assert experience.heading_text == "EXPÉRIENCE PROFESSIONNELLE"
    assert experience.heading_page == 2
    assert experience.content == "Stage analyste donnees"
    assert experience.page_numbers == (2,)
    assert [warning.code for warning in result.warnings] == [
        WarningCode.UNCLASSIFIED_LEADING_CONTENT
    ]


def test_the_same_file_always_produces_the_same_result(synthetic_pdf, tmp_path) -> None:
    path = _write(tmp_path, synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))

    first = parse_cv_pdf(path)
    second = parse_cv_pdf(str(path))

    # Frozen dataclasses compare by value: equality here is the whole result,
    # sections, provenance and warnings included, with nothing time-dependent.
    assert first == second


def test_an_empty_page_is_reported_and_keeps_the_real_page_numbers(
    synthetic_pdf,
) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, [], SECOND_PAGE]))

    assert result.page_count == 3
    assert result.pages[1].is_empty
    assert result.summary()["empty_page_numbers"] == [2]
    assert result.warnings[0].code is WarningCode.EMPTY_PAGE
    assert result.warnings[0].page_number == 2
    assert result.sections[-1].section_type is SectionType.LANGUAGES
    assert result.sections[-1].page_numbers == (3,)


def test_a_missing_path_is_refused(tmp_path) -> None:
    with pytest.raises(PdfFileNotFoundError):
        parse_cv_pdf(tmp_path / "absent.pdf")


def test_a_directory_is_refused(tmp_path) -> None:
    with pytest.raises(PdfNotAFileError):
        parse_cv_pdf(tmp_path)


@pytest.mark.parametrize("content", [b"", b"not a pdf at all", b"%PDF-1.4\ntruncated"])
def test_a_file_that_is_not_a_readable_pdf_is_refused(tmp_path, content) -> None:
    with pytest.raises(InvalidPdfError):
        parse_cv_pdf(_write(tmp_path, content))


def test_an_encrypted_pdf_is_refused_instead_of_half_read(
    synthetic_pdf, tmp_path
) -> None:
    writer = PdfWriter(clone_from=BytesIO(synthetic_pdf([FIRST_PAGE])))
    writer.encrypt("test-only-password")
    path = tmp_path / "encrypted.pdf"
    with path.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(EncryptedPdfError):
        parse_cv_pdf(path)


def test_a_scanned_cv_without_a_text_layer_is_reported_not_invented(
    synthetic_pdf,
) -> None:
    with pytest.raises(EmptyPdfTextError) as error:
        parse_cv_bytes(synthetic_pdf([[], []]))

    assert "OCR" in str(error.value)


def test_no_error_message_quotes_the_file_path(tmp_path) -> None:
    marker = "Jeanne-Exemple-TEST-ONLY"
    path = tmp_path / f"CV-{marker}.pdf"
    path.write_bytes(b"not a pdf at all")

    with pytest.raises(InvalidPdfError) as error:
        parse_cv_pdf(path)

    assert marker not in str(error.value)


def test_the_privacy_safe_summary_carries_no_cv_text(synthetic_pdf) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))

    rendered = repr(result.summary())

    for secret in (
        "Jeanne",
        "jeanne@example.invalid",
        "Etudiante",
        "Python",
        "Stage",
        # The heading as the CV wrote it is content too, and stays out as well.
        "EXPÉRIENCE PROFESSIONNELLE",
    ):
        assert secret not in rendered
    assert result.summary()["section_types"][1] == "PROFILE"


def test_the_detailed_view_keeps_the_text_and_its_provenance(synthetic_pdf) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))

    detailed = result.as_dict()

    assert [page["page_number"] for page in detailed["pages"]] == [1, 2]
    assert "Stage analyste donnees" in detailed["pages"][1]["text"]
    assert detailed["sections"][1] == {
        "section_type": "PROFILE",
        "heading_text": "PROFIL",
        "heading_page": 1,
        "content": "Etudiante en donnees et IA.",
        "page_numbers": [1],
    }


# --------------------------------------------------------------------------
# The layout facts each page carries alongside its text
# --------------------------------------------------------------------------


def test_every_line_of_a_page_carries_where_it_sat_on_that_page(synthetic_pdf) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))

    page = result.pages[0]

    # The correspondence the rest of the package relies on: one entry per line
    # of `text`, in order, with the very same string.
    assert [line.text for line in page.lines] == page.text.split("\n")
    assert page.has_layout
    layouts = [line.layout for line in page.lines]
    assert all(layout is not None for layout in layouts)
    # The synthetic builder writes one column and one leading, and that is what
    # comes back: a single left edge, one size, and a constant step downwards.
    assert {layout.x_start for layout in layouts} == {72.0}
    assert {layout.font_size for layout in layouts} == {12.0}
    steps = {
        round(first.y - second.y, 6)
        for first, second in zip(layouts, layouts[1:], strict=False)
    }
    assert steps == {16.0}


def test_the_text_of_a_page_is_unchanged_by_the_layout_being_read(
    synthetic_pdf,
) -> None:
    """`text` stays the page's primary representation, character for character."""
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, SECOND_PAGE]))

    assert result.pages[0].text == "\n".join(FIRST_PAGE)
    assert result.pages[1].text == "\n".join(SECOND_PAGE)


def test_a_page_built_from_text_alone_carries_no_layout_and_no_default_one() -> None:
    """The historical constructor still works, and invents no position."""
    page = ExtractedPage(page_number=3, text="one line\nanother line")

    assert page.lines == ()
    assert not page.has_layout
    assert page.text == "one line\nanother line"


def test_a_rotated_line_gets_no_layout_rather_than_a_meaningless_one() -> None:
    angle = 0.3
    cosine, sine = math.cos(angle), math.sin(angle)
    # Built through the imported builder rather than the fixture, so the
    # `PlacedText` written here is the one the builder itself compares against.
    content = build_synthetic_pdf(
        [
            [
                PlacedText(text="Upright test only line", x=72.0, y=760.0),
                PlacedText(
                    text="Rotated test only line",
                    x=72.0,
                    y=730.0,
                    matrix=(cosine, sine, -sine, cosine),
                ),
            ]
        ]
    )

    page = parse_cv_bytes(content).pages[0]

    assert [line.text for line in page.lines] == page.text.split("\n")
    assert page.lines[0].layout is not None
    assert page.lines[1].layout is None
    assert not page.has_layout


def test_an_empty_page_carries_no_line_at_all(synthetic_pdf) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, [], SECOND_PAGE]))

    assert result.pages[1].lines == ()
    assert not result.pages[1].has_layout


def test_the_summary_reports_which_pages_carry_layout_and_never_their_text(
    synthetic_pdf,
) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE, [], SECOND_PAGE]))

    summary = result.summary()

    assert summary["layout_page_numbers"] == [1, 3]
    assert "Jeanne" not in repr(summary)


def test_the_detailed_view_carries_the_layout_next_to_the_text(
    synthetic_pdf,
) -> None:
    result = parse_cv_bytes(synthetic_pdf([FIRST_PAGE]))

    first = result.as_dict()["pages"][0]

    assert [entry["text"] for entry in first["lines"]] == FIRST_PAGE
    assert first["lines"][0]["layout"] == {
        "x_start": 72.0,
        "y": 760.0,
        "font_size": 12.0,
        # The `/BaseFont` the builder declares for `F1`, copied out verbatim.
        "start_font_signature": "/Helvetica",
    }
