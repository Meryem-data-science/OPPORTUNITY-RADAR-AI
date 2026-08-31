"""Unit coverage for the deterministic CV section detection.

Every string here is invented test content. The headings are real heading
labels, but the bodies describe nobody.
"""

import pytest

from services.digital_twin.cv.models import ExtractedPage, SectionType, WarningCode
from services.digital_twin.cv.sections import (
    MAX_HEADING_LENGTH,
    SECTION_HEADINGS,
    classify_heading,
    detect_sections,
    fold_heading,
    segment_document,
)


def _pages(*page_texts: str) -> tuple[ExtractedPage, ...]:
    return tuple(
        ExtractedPage(page_number=number, text=text)
        for number, text in enumerate(page_texts, start=1)
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("PROFIL", SectionType.PROFILE),
        ("Objectif professionnel", SectionType.PROFILE),
        ("FORMATION", SectionType.EDUCATION),
        ("Formations académiques", SectionType.EDUCATION),
        ("EXPÉRIENCE PROFESSIONNELLE", SectionType.EXPERIENCE),
        ("Expériences professionnelles :", SectionType.EXPERIENCE),
        ("Stages", SectionType.EXPERIENCE),
        ("PROJETS", SectionType.PROJECTS),
        ("PROJETS SÉLECTIONNÉS", SectionType.PROJECTS),
        ("Projets sélectionnés :", SectionType.PROJECTS),
        ("Compétences techniques", SectionType.SKILLS),
        ("• COMPETENCES", SectionType.SKILLS),
        ("Certifications", SectionType.CERTIFICATIONS),
        ("LANGUES", SectionType.LANGUAGES),
    ],
)
def test_french_headings_are_recognised(line: str, expected: SectionType) -> None:
    assert classify_heading(line) is expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("PROFILE", SectionType.PROFILE),
        ("Professional Summary", SectionType.PROFILE),
        ("EDUCATION", SectionType.EDUCATION),
        ("Work Experience", SectionType.EXPERIENCE),
        ("EXPERIENCE", SectionType.EXPERIENCE),
        ("Projects", SectionType.PROJECTS),
        ("TECHNICAL SKILLS", SectionType.SKILLS),
        ("Skills:", SectionType.SKILLS),
        ("Licenses & Certifications", SectionType.CERTIFICATIONS),
        ("Languages", SectionType.LANGUAGES),
    ],
)
def test_english_headings_are_recognised(line: str, expected: SectionType) -> None:
    assert classify_heading(line) is expected


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "Jane Doe",
        "1. Formation",
        "Formation continue en apprentissage automatique",
        "Skills, tools and methods",
        "I improved my skills",
        "x" * (MAX_HEADING_LENGTH + 1),
    ],
)
def test_a_line_that_is_not_a_known_label_is_never_a_heading(line: str) -> None:
    assert classify_heading(line) is None


def test_the_accented_french_projects_label_folds_to_its_lexicon_entry() -> None:
    """The lookup stays an exact one: folding is all that bridges the accents."""
    assert fold_heading("PROJETS SÉLECTIONNÉS") == "projets selectionnes"
    assert SECTION_HEADINGS["projets selectionnes"] is SectionType.PROJECTS


def test_a_projects_heading_closes_the_experience_section_before_it() -> None:
    pages = _pages(
        "EXPERIENCE\n"
        "Poste fictif | Societe Exemple | 2024\n"
        "PROJETS SÉLECTIONNÉS\n"
        "Projet fictif : description inventee"
    )

    sections, warnings = detect_sections(pages)

    assert [section.section_type for section in sections] == [
        SectionType.EXPERIENCE,
        SectionType.PROJECTS,
    ]
    assert sections[0].content == "Poste fictif | Societe Exemple | 2024"
    assert sections[1].heading_text == "PROJETS SÉLECTIONNÉS"
    assert sections[1].content == "Projet fictif : description inventee"
    assert warnings == ()


def test_folding_only_removes_case_accents_and_decoration() -> None:
    assert fold_heading("  •  COMPÉTENCES :  ") == "competences"
    assert fold_heading("Licenses & Certifications") == "licenses and certifications"
    assert fold_heading("Data Engineer") == "data engineer"


def test_no_folded_label_is_claimed_by_two_section_types() -> None:
    # `SECTION_HEADINGS` is built with that check at import; assert the shape
    # it guarantees rather than trusting the import to have run it.
    assert len(SECTION_HEADINGS) == len(set(SECTION_HEADINGS))
    assert all(isinstance(value, SectionType) for value in SECTION_HEADINGS.values())
    assert SectionType.UNCLASSIFIED not in set(SECTION_HEADINGS.values())


def test_several_sections_keep_their_document_order_and_their_content() -> None:
    pages = _pages(
        "PROFIL\nEtudiante en donnees.\n\nFORMATION\nMaster Data & IA\n"
        "EXPERIENCE\nStage analyste\nCOMPETENCES\nPython\nSQL"
    )

    sections, warnings = detect_sections(pages)

    assert [section.section_type for section in sections] == [
        SectionType.PROFILE,
        SectionType.EDUCATION,
        SectionType.EXPERIENCE,
        SectionType.SKILLS,
    ]
    assert [section.heading_text for section in sections] == [
        "PROFIL",
        "FORMATION",
        "EXPERIENCE",
        "COMPETENCES",
    ]
    assert sections[0].content == "Etudiante en donnees."
    assert sections[1].content == "Master Data & IA"
    assert sections[3].content == "Python\nSQL"
    assert warnings == ()


def test_content_before_the_first_heading_is_kept_without_a_category() -> None:
    pages = _pages("Jane Doe\njane@example.invalid\nPROFIL\nEtudiante en donnees.")

    sections, warnings = detect_sections(pages)

    assert sections[0].section_type is SectionType.UNCLASSIFIED
    assert sections[0].heading_text is None
    assert sections[0].heading_page is None
    assert sections[0].content == "Jane Doe\njane@example.invalid"
    assert [warning.code for warning in warnings] == [
        WarningCode.UNCLASSIFIED_LEADING_CONTENT
    ]
    assert warnings[0].page_number == 1


def test_an_unknown_heading_gets_no_invented_category_and_loses_no_content() -> None:
    pages = _pages("PROFIL\nEtudiante.\nCENTRES D'INTERET\nRandonnee\nLANGUES\nAnglais")

    sections, warnings = detect_sections(pages)

    assert [section.section_type for section in sections] == [
        SectionType.PROFILE,
        SectionType.LANGUAGES,
    ]
    # The unrecognised heading is not a boundary: its lines stay attached to
    # the section that precedes them instead of being given a category.
    assert sections[0].content == "Etudiante.\nCENTRES D'INTERET\nRandonnee"
    assert warnings == ()


def test_a_document_without_any_known_heading_is_kept_whole_and_flagged() -> None:
    pages = _pages("Jane Doe\nRandonnee\nBenevolat")

    sections, warnings = detect_sections(pages)

    assert len(sections) == 1
    assert sections[0].section_type is SectionType.UNCLASSIFIED
    assert sections[0].content == "Jane Doe\nRandonnee\nBenevolat"
    assert [warning.code for warning in warnings] == [
        WarningCode.NO_SECTION_HEADING_DETECTED
    ]


def test_a_repeated_heading_opens_its_own_section_and_is_flagged() -> None:
    pages = _pages("EXPERIENCE\nStage un", "EXPERIENCE\nStage deux")

    sections, warnings = detect_sections(pages)

    assert [section.section_type for section in sections] == [
        SectionType.EXPERIENCE,
        SectionType.EXPERIENCE,
    ]
    assert [section.content for section in sections] == ["Stage un", "Stage deux"]
    assert [warning.code for warning in warnings] == [WarningCode.REPEATED_SECTION_TYPE]
    assert warnings[0].section_type is SectionType.EXPERIENCE
    assert warnings[0].page_number == 2


def test_a_heading_followed_by_nothing_is_reported_as_empty() -> None:
    pages = _pages("PROFIL\nEtudiante.\nCERTIFICATIONS")

    sections, warnings = detect_sections(pages)

    assert sections[-1].section_type is SectionType.CERTIFICATIONS
    assert sections[-1].is_empty
    assert [warning.code for warning in warnings] == [WarningCode.EMPTY_SECTION_CONTENT]


def test_a_section_records_every_page_it_spans() -> None:
    pages = _pages("PROFIL\nEtudiante.", "", "Suite du profil\nLANGUES\nAnglais")

    sections, _ = detect_sections(pages)

    assert sections[0].section_type is SectionType.PROFILE
    assert sections[0].heading_page == 1
    assert sections[0].page_numbers == (1, 3)
    assert sections[0].content == "Etudiante.\nSuite du profil"
    assert sections[1].page_numbers == (3,)


def test_detection_is_repeatable_for_the_same_pages() -> None:
    pages = _pages("PROFIL\nEtudiante.\nCOMPETENCES\nPython")

    assert detect_sections(pages) == detect_sections(pages)


def test_segments_carry_the_page_of_every_line_they_hold() -> None:
    pages = _pages("Jeanne Exemple\nFORMATION\nMaster fictif", "Licence fictive")

    segments, _ = segment_document(pages)

    header, education = segments
    assert header.section_type is SectionType.UNCLASSIFIED
    assert header.heading_line is None
    assert [(line.text, line.page_number) for line in header.body] == [
        ("Jeanne Exemple", 1)
    ]
    assert education.heading_line is not None
    assert education.heading_line.text == "FORMATION"
    assert education.heading_page == 1
    assert [(line.text, line.page_number) for line in education.body] == [
        ("Master fictif", 1),
        ("Licence fictive", 2),
    ]


def test_the_flattened_sections_are_exactly_what_the_segments_say() -> None:
    """`detect_sections` is a view over `segment_document`, not a second parse."""
    pages = _pages(
        "Jeanne Exemple\nFORMATION\nMaster fictif",
        "EXPERIENCE\nStage fictif",
    )

    segments, segment_warnings = segment_document(pages)
    sections, section_warnings = detect_sections(pages)

    assert sections == tuple(segment.as_section() for segment in segments)
    assert section_warnings == segment_warnings


def test_professional_development_is_an_exact_structural_heading() -> None:
    assert classify_heading("PROFESSIONAL DEVELOPMENT") is SectionType.PROFESSIONAL_DEVELOPMENT
    assert classify_heading("Professional development programme") is None
