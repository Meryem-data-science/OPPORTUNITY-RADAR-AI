"""Unit coverage for the Phase 3.2B structured candidate extractor.

Every CV below is invented and built in memory. TEST ONLY: no real CV, person,
email address, phone number, employer, school or URL appears here, in the
fixtures or in the git history. Every domain is under `.invalid`, which is
reserved by RFC 2606 and can never resolve.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest

from services.digital_twin.cv.candidates import (
    CANDIDATE_EXTRACTOR_VERSION,
    CandidateType,
    CandidateWarningCode,
    ExtractedCandidate,
    ExtractionRule,
    StructuredCvExtraction,
    extract_candidates,
)
from services.digital_twin.cv.models import (
    PARSER_VERSION,
    ExtractedPage,
    ParsedCv,
    SectionType,
)
from services.digital_twin.cv.parser import parse_cv_bytes
from services.digital_twin.cv.sections import detect_sections

# TEST ONLY content; it describes nobody.
HEADER = [
    "Jeanne Exemple",
    "Data Scientist",
    "Email : jeanne.exemple@example.invalid",
    "Tel : +33 6 00 00 00 00",
    "github.com/exemple-compte",
    "https://fr.linkedin.com/in/exemple-compte",
    "Portfolio : https://exemple-portfolio.invalid",
    "https://blog.exemple.invalid/articles",
]
BODY = [
    "FORMATION",
    "Master Data & IA - Universite Exemple (2023-2025)",
    "Licence Informatique - Universite Exemple (2020-2023)",
    "EXPERIENCE PROFESSIONNELLE",
    "- Stage analyste donnees - Societe Exemple - 2024",
    "- Alternance data - Autre Societe Exemple - 2023",
    "PROJETS",
    "Projet A : pipeline de donnees fictif",
    "Projet B : tableau de bord fictif",
    "COMPETENCES TECHNIQUES",
    "Langages : Python, SQL, R",
    "Azure Data Platform (avance)",
    "CERTIFICATIONS",
    "Certification Fictive Cloud - 2024",
    "LANGUES",
    "Francais : natif",
    "Anglais : C1",
]


@pytest.fixture()
def extract(synthetic_pdf):
    """Build a PDF from the given pages, parse it, and extract its candidates."""

    def _extract(*pages: list[str]) -> StructuredCvExtraction:
        return extract_candidates(parse_cv_bytes(synthetic_pdf(list(pages))))

    return _extract


def _texts(result: StructuredCvExtraction, candidate_type: CandidateType) -> list[str]:
    return [candidate.raw_text for candidate in result.of_type(candidate_type)]


#: An obviously fake digest. These tests build the `ParsedCv` by hand where the
#: page layout itself is what is under test — a blank line between entries, or
#: a section running across a page break — because the synthetic PDF builder
#: emits no empty text line and one page per call.
FAKE_SHA256 = "0" * 64


def parsed_from_pages(*page_texts: str) -> ParsedCv:
    """Build the Phase 3.2A result of already-normalized page texts."""
    pages = tuple(
        ExtractedPage(page_number=number, text=text)
        for number, text in enumerate(page_texts, start=1)
    )
    sections, warnings = detect_sections(pages)
    return ParsedCv(
        parser_version=PARSER_VERSION,
        content_sha256=FAKE_SHA256,
        page_count=len(pages),
        pages=pages,
        sections=sections,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# 1-3. Determinism, version, provenance
# --------------------------------------------------------------------------


def test_the_same_parsed_cv_always_yields_the_same_candidates(synthetic_pdf) -> None:
    content = synthetic_pdf([HEADER, BODY])
    parsed = parse_cv_bytes(content)

    first = extract_candidates(parsed)
    second = extract_candidates(parse_cv_bytes(content))

    assert first == second
    assert first.candidates == second.candidates
    for left, right in zip(first.candidates, second.candidates, strict=True):
        assert left.as_dict() == right.as_dict()


def test_the_result_and_every_candidate_carry_the_extractor_version(extract) -> None:
    result = extract(HEADER, BODY)

    assert result.extractor_version == CANDIDATE_EXTRACTOR_VERSION == "cv-candidates-v1"
    assert result.extractor_version != result.parser_version
    assert result.candidates
    assert all(
        candidate.extractor_version == CANDIDATE_EXTRACTOR_VERSION
        for candidate in result.candidates
    )


def test_every_candidate_carries_its_full_provenance(extract, synthetic_pdf) -> None:
    parsed = parse_cv_bytes(synthetic_pdf([HEADER, BODY]))
    result = extract_candidates(parsed)

    assert result.cv_sha256 == parsed.content_sha256
    assert result.parser_version == PARSER_VERSION
    for candidate in result.candidates:
        assert candidate.cv_sha256 == parsed.content_sha256
        assert candidate.parser_version == PARSER_VERSION
        assert candidate.page_numbers
        assert list(candidate.page_numbers) == sorted(set(candidate.page_numbers))
        assert all(1 <= page <= parsed.page_count for page in candidate.page_numbers)
        assert isinstance(candidate.rule_id, ExtractionRule)
        assert candidate.section_index is not None
        assert parsed.sections[candidate.section_index].section_type is (
            candidate.section_type
        )
        assert candidate.fingerprint


def test_provenance_points_at_the_page_the_text_was_written_on(extract) -> None:
    result = extract(HEADER, BODY)

    (email,) = result.of_type(CandidateType.EMAIL)
    assert email.page_numbers == (1,)
    assert email.section_type is SectionType.UNCLASSIFIED

    certifications = result.of_type(CandidateType.CERTIFICATION_ENTRY)
    assert [candidate.page_numbers for candidate in certifications] == [(2,)]


# --------------------------------------------------------------------------
# 4-5. Contact: email and phone
# --------------------------------------------------------------------------


def test_an_email_is_extracted_with_its_source_value_kept(extract) -> None:
    result = extract(HEADER, BODY)

    (email,) = result.of_type(CandidateType.EMAIL)
    assert email.raw_text == "jeanne.exemple@example.invalid"
    assert email.rule_id is ExtractionRule.EMAIL_PATTERN


def test_the_email_normal_form_is_kept_apart_from_the_source_value(extract) -> None:
    result = extract(["Jeanne Exemple", "Contact : Jeanne.EXEMPLE@Example.Invalid"])

    (email,) = result.of_type(CandidateType.EMAIL)
    assert email.raw_text == "Jeanne.EXEMPLE@Example.Invalid"
    assert email.normalized_value == "jeanne.exemple@example.invalid"


@pytest.mark.parametrize(
    ("line", "expected_rule"),
    [
        ("+33 6 00 00 00 00", ExtractionRule.PHONE_INTERNATIONAL_PREFIX),
        ("Telephone : 06 11 22 33 44", ExtractionRule.PHONE_LABELLED_LINE),
        ("06 11 22 33 44", ExtractionRule.PHONE_HEADER_TRUNK_ZERO),
    ],
)
def test_a_phone_number_is_extracted_by_a_named_rule(
    extract, line: str, expected_rule: ExtractionRule
) -> None:
    result = extract(["Jeanne Exemple", line, "PROFIL", "Texte fictif."])

    (phone,) = result.of_type(CandidateType.PHONE)
    assert phone.rule_id is expected_rule


def test_a_phone_number_never_gains_a_country_code_it_did_not_have(extract) -> None:
    result = extract(["Jeanne Exemple", "Telephone : 06 11 22 33 44"])

    (phone,) = result.of_type(CandidateType.PHONE)
    assert phone.raw_text == "06 11 22 33 44"
    # Compacted, never completed: no "+33", no country, no area code.
    assert phone.normalized_value == "0611223344"


def test_dates_and_reference_numbers_are_not_read_as_phone_numbers(extract) -> None:
    result = extract(
        [
            "Jeanne Exemple",
            "EXPERIENCE",
            "Mission fictive 01 2020 - 12 2024 chez Societe Exemple",
            "Reference interne 1234567890123",
        ]
    )

    assert result.of_type(CandidateType.PHONE) == ()


@pytest.mark.parametrize(
    "header_line",
    [
        "01 2020 - 12 2024",
        "09 2019 - 06 2023",
        "Disponible 01 2020 - 12 2024",
        "2019 2020 2021 2022",
    ],
)
def test_a_period_in_the_header_is_never_read_as_a_phone_number(
    extract, header_line: str
) -> None:
    """The header holds availability dates next to the contact details."""
    result = extract(["Jeanne Exemple", header_line, "PROFIL", "Texte fictif."])

    assert result.of_type(CandidateType.PHONE) == ()


def test_a_phone_label_does_not_turn_a_period_into_a_phone_number(extract) -> None:
    """"Portable" is a French adjective as often as it is a phone label."""
    result = extract(
        [
            "Jeanne Exemple",
            "EXPERIENCE",
            "Poste portable de 01 2020 a 12 2024 chez Societe Exemple",
        ]
    )

    assert result.of_type(CandidateType.PHONE) == ()


@pytest.mark.parametrize(
    "header_line",
    [
        "06 11 22 33 44",
        "0611223344",
        "06.11.22.33.44",
        # A four-digit group that is not a year: the rule is about the shape of
        # the grouping, not about four-digit groups being suspicious.
        "0470 12 34 56",
    ],
)
def test_a_national_number_written_the_usual_ways_still_survives(
    extract, header_line: str
) -> None:
    result = extract(["Jeanne Exemple", header_line, "PROFIL", "Texte fictif."])

    (phone,) = result.of_type(CandidateType.PHONE)
    assert phone.rule_id is ExtractionRule.PHONE_HEADER_TRUNK_ZERO


# --------------------------------------------------------------------------
# 6-9. URLs
# --------------------------------------------------------------------------


def test_github_and_linkedin_are_classified_by_hostname(extract) -> None:
    result = extract(HEADER, BODY)

    (github,) = result.of_type(CandidateType.GITHUB_URL)
    assert github.raw_text == "github.com/exemple-compte"
    assert github.rule_id is ExtractionRule.URL_GITHUB_HOST

    (linkedin,) = result.of_type(CandidateType.LINKEDIN_URL)
    assert linkedin.raw_text == "https://fr.linkedin.com/in/exemple-compte"
    assert linkedin.rule_id is ExtractionRule.URL_LINKEDIN_HOST


def test_a_portfolio_is_only_claimed_where_a_label_says_so(extract) -> None:
    result = extract(HEADER, BODY)

    (portfolio,) = result.of_type(CandidateType.PORTFOLIO_URL)
    assert portfolio.raw_text == "https://exemple-portfolio.invalid"
    assert portfolio.rule_id is ExtractionRule.URL_PORTFOLIO_LABELLED_LINE


def test_an_unlabelled_url_stays_professional_rather_than_being_guessed(
    extract,
) -> None:
    result = extract(HEADER, BODY)

    (generic,) = result.of_type(CandidateType.PROFESSIONAL_URL)
    assert generic.raw_text == "https://blog.exemple.invalid/articles"
    assert generic.rule_id is ExtractionRule.URL_UNLABELLED_PROFESSIONAL
    assert CandidateType.PORTFOLIO_URL not in {
        candidate.candidate_type
        for candidate in result.candidates
        if candidate.raw_text == generic.raw_text
    }


def test_a_personal_looking_domain_alone_is_never_a_portfolio(extract) -> None:
    result = extract(["Jeanne Exemple", "https://jeanne-exemple.invalid"])

    (url,) = result.of_type(CandidateType.PROFESSIONAL_URL)
    assert url.rule_id is ExtractionRule.URL_UNLABELLED_PROFESSIONAL
    assert result.of_type(CandidateType.PORTFOLIO_URL) == ()


def test_a_url_is_not_carved_out_of_an_email_address(extract) -> None:
    result = extract(["Jeanne Exemple", "contact@github.com.invalid"])

    assert _texts(result, CandidateType.EMAIL) == ["contact@github.com.invalid"]
    assert result.of_type(CandidateType.GITHUB_URL) == ()


@pytest.mark.parametrize(
    "look_alike",
    [
        "github.com.evil.invalid",
        "linkedin.com.attacker.invalid",
        "foo.github.com.evil.invalid",
        "github.com.evil.invalid/user",
        # A known name that is only the start of a longer label.
        "github.community/topic",
    ],
)
def test_a_host_that_merely_opens_with_a_known_name_is_not_that_host(
    extract, look_alike: str
) -> None:
    """A bare host is recognised only where the hostname really ends."""
    result = extract(["Jeanne Exemple", look_alike])

    assert result.of_type(CandidateType.GITHUB_URL) == ()
    assert result.of_type(CandidateType.LINKEDIN_URL) == ()
    # Nothing was carved out of the middle of the name either.
    assert all(
        candidate.raw_text == look_alike for candidate in result.candidates[1:]
    )


@pytest.mark.parametrize(
    "written",
    ["https://github.com.evil.invalid/user", "www.linkedin.com.attacker.invalid"],
)
def test_a_look_alike_host_written_in_full_stays_a_plain_professional_url(
    extract, written: str
) -> None:
    """A complete URL is kept whole and classified on its real hostname."""
    result = extract(["Jeanne Exemple", written])

    (url,) = result.of_type(CandidateType.PROFESSIONAL_URL)
    assert url.raw_text == written
    assert url.rule_id is ExtractionRule.URL_UNLABELLED_PROFESSIONAL


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("github.com/user", CandidateType.GITHUB_URL),
        ("foo.github.com/user", CandidateType.GITHUB_URL),
        ("exemple.github.io/projet", CandidateType.GITHUB_URL),
        ("linkedin.com/in/example", CandidateType.LINKEDIN_URL),
        ("fr.linkedin.com/in/example", CandidateType.LINKEDIN_URL),
    ],
)
def test_the_legitimate_bare_hosts_are_still_recognised(
    extract, written: str, expected: CandidateType
) -> None:
    result = extract(["Jeanne Exemple", written])

    (url,) = result.of_type(expected)
    assert url.raw_text == written


def test_a_host_ending_a_sentence_keeps_its_full_name(extract) -> None:
    """A trailing dot closes a sentence; it is not a further hostname label."""
    result = extract(["Jeanne Exemple", "Code publie sur github.com/user."])

    (url,) = result.of_type(CandidateType.GITHUB_URL)
    assert url.raw_text == "github.com/user"


# --------------------------------------------------------------------------
# 10. Identity
# --------------------------------------------------------------------------


def test_the_header_proposes_a_name_candidate_and_a_title(extract) -> None:
    result = extract(HEADER, BODY)

    (name,) = result.of_type(CandidateType.NAME_CANDIDATE)
    assert name.raw_text == "Jeanne Exemple"
    assert name.rule_id is ExtractionRule.HEADER_FIRST_LINE_NAME_SHAPE
    assert name.section_type is SectionType.UNCLASSIFIED
    assert name.normalized_value is None

    (title,) = result.of_type(CandidateType.PROFESSIONAL_TITLE)
    assert title.raw_text == "Data Scientist"
    assert title.rule_id is ExtractionRule.HEADER_TITLE_KEYWORD_LINE


@pytest.mark.parametrize(
    "first_line",
    [
        "Curriculum Vitae",
        "Jeanne Exemple - Data Scientist",
        "jeanne.exemple@example.invalid",
        "12 rue Fictive, Ville Exemple",
        "Universite Exemple, promotion fictive 2025",
    ],
)
def test_an_ambiguous_header_line_proposes_no_name_at_all(
    extract, first_line: str
) -> None:
    result = extract([first_line, "FORMATION", "Master fictif"])

    assert result.of_type(CandidateType.NAME_CANDIDATE) == ()
    assert CandidateWarningCode.NO_IDENTITY_CANDIDATE in {
        warning.code for warning in result.warnings
    }


def test_a_name_is_never_built_from_an_email_address(extract) -> None:
    result = extract(["jeanne.exemple@example.invalid", "PROFIL", "Texte fictif."])

    assert result.of_type(CandidateType.NAME_CANDIDATE) == ()
    assert _texts(result, CandidateType.EMAIL) == ["jeanne.exemple@example.invalid"]


def test_a_document_opening_on_a_heading_reports_no_header_block(extract) -> None:
    result = extract(["COMPETENCES", "Python, SQL"])

    assert result.of_type(CandidateType.NAME_CANDIDATE) == ()
    assert CandidateWarningCode.NO_HEADER_BLOCK in {
        warning.code for warning in result.warnings
    }


# --------------------------------------------------------------------------
# 11-16. Structured section entries and skills
# --------------------------------------------------------------------------


def test_education_entries_keep_their_source_text(extract) -> None:
    result = extract(HEADER, BODY)

    assert _texts(result, CandidateType.EDUCATION_ENTRY) == [
        "Master Data & IA - Universite Exemple (2023-2025)",
        "Licence Informatique - Universite Exemple (2020-2023)",
    ]


def test_experience_entries_are_cut_on_the_list_markers(extract) -> None:
    result = extract(HEADER, BODY)

    experiences = result.of_type(CandidateType.EXPERIENCE_ENTRY)
    assert [candidate.raw_text for candidate in experiences] == [
        "- Stage analyste donnees - Societe Exemple - 2024",
        "- Alternance data - Autre Societe Exemple - 2023",
    ]
    assert all(
        candidate.rule_id is ExtractionRule.SECTION_BULLET_BLOCK
        for candidate in experiences
    )


def test_a_blank_line_keeps_a_multi_line_entry_in_one_block() -> None:
    parsed = parsed_from_pages(
        "Jeanne Exemple\n"
        "EXPERIENCE\n"
        "Analyste fictif - Societe Exemple\n"
        "2023 - 2024\n"
        "\n"
        "Stagiaire fictif - Autre Societe Exemple"
    )

    experiences = extract_candidates(parsed).of_type(CandidateType.EXPERIENCE_ENTRY)

    assert [candidate.raw_text for candidate in experiences] == [
        "Analyste fictif - Societe Exemple\n2023 - 2024",
        "Stagiaire fictif - Autre Societe Exemple",
    ]
    assert all(
        candidate.rule_id is ExtractionRule.SECTION_BLANK_LINE_BLOCK
        for candidate in experiences
    )


def test_an_entry_running_over_a_page_break_carries_both_pages() -> None:
    parsed = parsed_from_pages(
        "Jeanne Exemple\nEXPERIENCE\n\nAnalyste fictif - Societe Exemple",
        "2023 - 2024\n\nStagiaire fictif - Autre Societe Exemple",
    )

    first, second = extract_candidates(parsed).of_type(CandidateType.EXPERIENCE_ENTRY)

    assert first.raw_text == "Analyste fictif - Societe Exemple\n2023 - 2024"
    assert first.page_numbers == (1, 2)
    assert second.page_numbers == (2,)


def test_project_entries_are_extracted(extract) -> None:
    result = extract(HEADER, BODY)

    assert _texts(result, CandidateType.PROJECT_ENTRY) == [
        "Projet A : pipeline de donnees fictif",
        "Projet B : tableau de bord fictif",
    ]


def test_certification_entries_are_extracted(extract) -> None:
    result = extract(HEADER, BODY)

    assert _texts(result, CandidateType.CERTIFICATION_ENTRY) == [
        "Certification Fictive Cloud - 2024"
    ]


def test_a_certification_is_never_derived_from_a_technology(extract) -> None:
    result = extract(
        ["Jeanne Exemple", "COMPETENCES", "Azure, Kubernetes, Python"]
    )

    assert result.of_type(CandidateType.CERTIFICATION_ENTRY) == ()


def test_language_entries_keep_their_line_unsplit(extract) -> None:
    result = extract(HEADER, BODY)

    assert _texts(result, CandidateType.LANGUAGE_ENTRY) == [
        "Francais : natif",
        "Anglais : C1",
    ]


def test_skills_are_split_on_the_separators_the_cv_used(extract) -> None:
    result = extract(HEADER, BODY)

    skills = result.of_type(CandidateType.SKILL)
    assert [candidate.raw_text for candidate in skills] == [
        "Python",
        "SQL",
        "R",
        "Azure Data Platform (avance)",
    ]
    assert skills[0].rule_id is ExtractionRule.SKILLS_LABELLED_LIST_LINE
    assert skills[0].section_type is SectionType.SKILLS


def test_a_skills_label_is_dropped_only_when_a_list_follows_it(extract) -> None:
    result = extract(
        ["Jeanne Exemple", "COMPETENCES", "Outils : Docker ; Git", "Python: 5 ans"]
    )

    assert _texts(result, CandidateType.SKILL) == ["Docker", "Git", "Python: 5 ans"]


def test_skills_outside_a_skills_section_are_not_invented(extract) -> None:
    result = extract(
        ["Jeanne Exemple", "EXPERIENCE", "Analyste fictif, Python, SQL, Docker"]
    )

    assert result.of_type(CandidateType.SKILL) == ()


# --------------------------------------------------------------------------
# 17-19. Deduplication, ambiguity, absence of any level model
# --------------------------------------------------------------------------


def test_the_same_value_repeated_in_the_document_yields_one_candidate(extract) -> None:
    result = extract(
        [
            "Alex Exemple",
            "alex@example.invalid",
            "COMPETENCES",
            "Python, SQL",
            "Python ; Docker",
            "EXPERIENCE",
            "Contact : ALEX@EXAMPLE.INVALID",
        ]
    )

    assert _texts(result, CandidateType.EMAIL) == ["alex@example.invalid"]
    assert _texts(result, CandidateType.SKILL) == ["Python", "SQL", "Docker"]
    fingerprints = [candidate.fingerprint for candidate in result.candidates]
    assert len(fingerprints) == len(set(fingerprints))


def test_distinct_values_of_the_same_type_all_survive(extract) -> None:
    result = extract(
        [
            "Alex Exemple",
            "alex@example.invalid",
            "contact.alex@example.invalid",
        ]
    )

    assert _texts(result, CandidateType.EMAIL) == [
        "alex@example.invalid",
        "contact.alex@example.invalid",
    ]


def test_a_fingerprint_is_stable_across_two_documents_holding_the_value(
    extract,
) -> None:
    first = extract(["Alex Exemple", "alex@example.invalid"])
    second = extract(["Alex Exemple", "PROFIL", "Ecrire a alex@example.invalid."])

    (left,) = first.of_type(CandidateType.EMAIL)
    (right,) = second.of_type(CandidateType.EMAIL)
    assert left.fingerprint == right.fingerprint
    # The fingerprint identifies a value, not a stored fact.
    assert left.section_type is not right.section_type


def test_one_phone_number_written_two_ways_yields_one_candidate(extract) -> None:
    """The technical normal form is what two spellings are compared on."""
    result = extract(
        [
            "Jeanne Exemple",
            "Tel : +33 6 00 00 00 00",
            "Mobile : +33600000000",
        ]
    )

    (phone,) = result.of_type(CandidateType.PHONE)
    # The first occurrence keeps its own text and its own provenance.
    assert phone.raw_text == "+33 6 00 00 00 00"
    assert phone.normalized_value == "+33600000000"


def test_two_numbers_that_differ_stay_two_candidates(extract) -> None:
    """A trunk zero is not the same number as an international prefix here.

    Deciding they are the same would mean inferring a country, which no rule of
    this package is allowed to do.
    """
    result = extract(["Jeanne Exemple", "+33 6 00 00 00 00", "06 00 00 00 00"])

    phones = result.of_type(CandidateType.PHONE)
    assert [phone.normalized_value for phone in phones] == [
        "+33600000000",
        "0600000000",
    ]


def test_one_email_written_two_ways_yields_one_candidate(extract) -> None:
    result = extract(
        [
            "Jeanne Exemple",
            "Jeanne.EXEMPLE@Example.Invalid",
            "PROFIL",
            "Ecrire a jeanne.exemple@example.invalid.",
        ]
    )

    (email,) = result.of_type(CandidateType.EMAIL)
    assert email.raw_text == "Jeanne.EXEMPLE@Example.Invalid"
    assert email.normalized_value == "jeanne.exemple@example.invalid"


def test_values_with_no_normal_form_are_compared_on_their_own_text(extract) -> None:
    """URLs, entries and skills have no unambiguous normal form, so none is used."""
    result = extract(
        [
            "Jeanne Exemple",
            "https://exemple.invalid/page-a",
            "https://exemple.invalid/page-b",
            "COMPETENCES",
            "Python, python, Python 3",
        ]
    )

    assert _texts(result, CandidateType.PROFESSIONAL_URL) == [
        "https://exemple.invalid/page-a",
        "https://exemple.invalid/page-b",
    ]
    # Case folding is all the text comparison does, so "python" is "Python"
    # and "Python 3" is a different mention.
    assert _texts(result, CandidateType.SKILL) == ["Python", "Python 3"]
    assert all(
        candidate.normalized_value is None
        for candidate in result.candidates
        if candidate.candidate_type
        not in {CandidateType.EMAIL, CandidateType.PHONE}
    )


def test_the_fingerprint_identifies_a_value_and_not_a_document(extract) -> None:
    """Two CVs carrying one address fingerprint it the same; `cv_sha256` differs."""
    first = extract(["Alex Exemple", "alex@example.invalid"])
    second = extract(["Autre Exemple", "PROFIL", "Ecrire a ALEX@EXAMPLE.INVALID."])

    (left,) = first.of_type(CandidateType.EMAIL)
    (right,) = second.of_type(CandidateType.EMAIL)
    assert left.fingerprint == right.fingerprint
    assert left.raw_text != right.raw_text
    assert left.cv_sha256 != right.cv_sha256


def test_ambiguous_prose_produces_no_assertion_of_any_kind(extract) -> None:
    result = extract(
        [
            "Curriculum Vitae",
            "PROFIL",
            "Interesse par Azure, souhaite se former a Kubernetes.",
        ]
    )

    assert result.of_type(CandidateType.NAME_CANDIDATE) == ()
    assert result.of_type(CandidateType.SKILL) == ()
    assert result.of_type(CandidateType.CERTIFICATION_ENTRY) == ()
    assert result.of_type(CandidateType.EXPERIENCE_ENTRY) == ()


def test_a_written_level_is_kept_as_text_and_never_turned_into_a_level(
    extract,
) -> None:
    result = extract(
        [
            "Jeanne Exemple",
            "COMPETENCES",
            "Azure Data Platform (avance)",
            "Python - expert",
        ]
    )

    assert _texts(result, CandidateType.SKILL) == [
        "Azure Data Platform (avance)",
        "Python - expert",
    ]


def test_the_candidate_model_has_no_level_verification_or_score_field() -> None:
    """A field that cannot exist cannot be filled in with a guess."""
    forbidden = {
        "level",
        "skill_level",
        "proficiency",
        "seniority",
        "confidence",
        "score",
        "verified",
        "is_verified",
        "accepted",
        "rejected",
        "corrected",
        "status",
        "fact_id",
        "profile_id",
        "user_id",
    }
    names = {field.name for field in fields(ExtractedCandidate)}

    assert not names & forbidden
    assert names == {
        "candidate_type",
        "raw_text",
        "normalized_value",
        "page_numbers",
        "section_type",
        "section_index",
        "rule_id",
        "fingerprint",
        "cv_sha256",
        "parser_version",
        "extractor_version",
    }


def test_no_candidate_type_names_a_level_or_a_verified_fact() -> None:
    values = {candidate_type.value for candidate_type in CandidateType}

    assert not any(
        marker in value
        for value in values
        for marker in ("LEVEL", "PROFICIENCY", "SCORE", "VERIFIED", "FACT")
    )


# --------------------------------------------------------------------------
# 20. No database, no clock, no network
# --------------------------------------------------------------------------


_PACKAGE = Path("services/digital_twin/cv/candidates")
#: Modules that would make the extractor depend on state outside its input.
_FORBIDDEN_ROOTS = {
    "sqlite3",
    "libsql",
    "socket",
    "ssl",
    "http",
    "urllib",
    "httpx",
    "requests",
    "datetime",
    "time",
    "random",
    "uuid",
    "secrets",
}
_FORBIDDEN_PREFIXES = (
    "services.collector.database",
    "services.digital_twin.repository",
)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module)
    return roots


@pytest.mark.parametrize(
    "module_path", sorted(_PACKAGE.glob("*.py")), ids=lambda path: path.name
)
def test_the_extractor_imports_no_database_clock_or_network_module(
    module_path: Path,
) -> None:
    """The extractor is a pure function of its `ParsedCv`, enforced statically."""
    for module in _imported_roots(module_path):
        assert module.split(".")[0] not in _FORBIDDEN_ROOTS, module
        assert not module.startswith(_FORBIDDEN_PREFIXES), module


def test_the_extractor_writes_nothing_to_disk(extract, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = extract(HEADER, BODY)

    assert result.candidates
    assert list(tmp_path.iterdir()) == []


def _statements(migration: Path) -> str:
    """Return the migration with its SQL comments removed."""
    return "\n".join(
        line.split("--", 1)[0]
        for line in migration.read_text(encoding="utf-8").casefold().splitlines()
    )


def test_this_slice_creates_no_table_for_candidates_or_profile_facts() -> None:
    """Phase 3.2B stores nothing: the schema must be exactly what 3.1A left."""
    migrations = sorted(Path("migrations").glob("*.sql"))

    assert [migration.name for migration in migrations] == [
        "0001_opportunity_foundation.sql",
        "0002_deduplication_decisions.sql",
        "0003_deduplication_merges.sql",
        "0004_opportunity_qualifications.sql",
        "0005_source_runs.sql",
        "0006_user_profile_foundation.sql",
    ]
    for migration in migrations:
        statements = _statements(migration)
        assert "profile_facts" not in statements
        assert "cv_candidate" not in statements
        assert "cv_version" not in statements


def test_the_summary_never_quotes_the_cv(extract) -> None:
    result = extract(HEADER, BODY)
    rendered = repr(result.summary())

    for candidate in result.candidates:
        # A one-character mention such as "R" occurs in any English prose; the
        # values that carry personal data are the ones worth asserting on.
        if len(candidate.raw_text) > 3:
            assert candidate.raw_text not in rendered
    for secret in ("jeanne", "example.invalid", "+33", "exemple-compte"):
        assert secret not in rendered.casefold()
    assert result.as_dict()["candidates"]
