"""Unit coverage for the Phase 3.4B2 education, certification and language rules.

Every value below is synthetic: invented schools, invented programmes, invented
certifications, invented languages. No real CV, no real address and no real
personal data takes part, and nothing here opens a database or a file.

There are three verified `EDUCATION` facts in the operational database today
and no `CERTIFICATION` or `LANGUAGE` fact at all. None of that appears here:
these rules are proved against wordings invented for the tests, so that no
count and no phrasing of anybody's CV can turn into a constant of the code.

Most of these tests are about what the rules refuse to do. The refusal they all
share is the one that separates this slice from a guess: **punctuation proves
that segments exist, it never proves what they are about**. A pipe-delimited
education line is not a promise that segment 1 is a diploma and segment 2 a
school, so where the closed registry cannot tell them apart the period alone is
kept and both stay `None` — a `NULL` is worth more than an invented value.
"""

import pytest

from services.digital_twin.structured_profile.models import (
    STRUCTURED_PROFILE_VERSION,
    StructuringRule,
)
from services.digital_twin.structured_profile.structurer import (
    INSTITUTION_MARKERS,
    PROFICIENCY_FORMS,
    PROGRAM_MARKERS,
    is_explicit_proficiency,
    names_a_program,
    names_an_institution,
    states_an_intention,
    structure_certification,
    structure_education,
    structure_language,
)

# --------------------------------------------------------------------------
# EDUCATION — the fully explicit reading
# --------------------------------------------------------------------------


def test_a_marked_institution_and_a_period_give_the_whole_reading() -> None:
    reading = structure_education(
        "Master 2 Data Science | Université de Paris | 2020 - 2022"
    )

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_PIPE_EXPLICIT_V1
    assert reading.institution_text == "Université de Paris"
    assert reading.program_text == "Master 2 Data Science"
    assert reading.period_text == "2020 - 2022"
    assert reading.description_text is None
    assert reading.structurer_version == STRUCTURED_PROFILE_VERSION


def test_the_reading_does_not_depend_on_the_order_of_the_segments() -> None:
    """The rule asks the registry which segment is a school; it never counts.

    This is the whole difference with the Phase 3.4B1 experience rule, and the
    reason a separate rule exists: a CV writes `role | employer | dates` in
    that order, and writes a diploma and a school in either.
    """
    diploma_first = structure_education(
        "Master 2 Data Science | Université de Paris | 2020 - 2022"
    )
    school_first = structure_education(
        "Université de Paris | Master 2 Data Science | 2020 - 2022"
    )

    assert (
        diploma_first.institution_text
        == school_first.institution_text
        == "Université de Paris"
    )
    assert (
        diploma_first.program_text
        == school_first.program_text
        == "Master 2 Data Science"
    )


def test_the_period_is_found_wherever_the_document_wrote_it() -> None:
    reading = structure_education(
        "2020 - 2022 | Licence Informatique | Université de Lyon"
    )

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_PIPE_EXPLICIT_V1
    assert reading.period_text == "2020 - 2022"
    assert reading.institution_text == "Université de Lyon"
    assert reading.program_text == "Licence Informatique"


def test_both_roles_need_their_own_proof() -> None:
    """A marker on one segment is never proof about the other one."""
    reading = structure_education("Master Data Science | Université Exemple | 2023-2025")

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_PIPE_EXPLICIT_V1
    assert reading.institution_text == "Université Exemple"
    assert reading.program_text == "Master Data Science"


def test_both_proofs_read_the_same_in_either_order() -> None:
    programme_first = structure_education(
        "Master Data Science | Université Exemple | 2023-2025"
    )
    school_first = structure_education(
        "Université Exemple | Master Data Science | 2023-2025"
    )

    assert programme_first.institution_text == school_first.institution_text
    assert programme_first.program_text == school_first.program_text
    assert school_first.institution_text == "Université Exemple"
    assert school_first.program_text == "Master Data Science"


@pytest.mark.parametrize(
    "value",
    [
        "University Diploma in AI | Sorbonne | 2024",
        "Sorbonne | University Diploma in AI | 2024",
    ],
)
def test_a_segment_proving_both_roles_never_inverts_the_two_fields(
    value: str,
) -> None:
    """The counter-example the marker-only rule got wrong, in both orders.

    `University Diploma in AI` carries an institution marker and is a
    programme; `Sorbonne` carries no marker at all and is the school. Reading
    the institution marker alone would store the inversion as a structured
    fact. Nothing proves either role exclusively, so nothing is stored.
    """
    reading = structure_education(value)

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_PERIOD_ONLY_V1
    )
    assert reading.institution_text is None
    assert reading.program_text is None
    assert reading.period_text == "2024"


def test_the_inverting_counter_example_stays_unparsed_without_a_period() -> None:
    reading = structure_education("University Diploma in AI | Sorbonne")

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_UNPARSED_V1
    assert (reading.institution_text, reading.program_text) == (None, None)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (
            "Master de l'Université Exemple | Autre chose | 2024",
            "one segment proves both roles at once",
        ),
        (
            "Université Exemple | Sciences | 2024",
            "an institution is proven, a programme is not",
        ),
        (
            "Master Data Science | Autre chose | 2024",
            "a programme is proven, an institution is not",
        ),
        (
            "Université Exemple | École Exemple | 2024",
            "both segments prove the same role",
        ),
        ("Sciences | Autre chose | 2024", "neither role is proven"),
    ],
)
def test_a_role_without_an_exclusive_proof_keeps_only_the_period(
    value: str, reason: str
) -> None:
    reading = structure_education(value)

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_PERIOD_ONLY_V1
    ), reason
    assert (reading.institution_text, reading.program_text) == (None, None)
    assert reading.period_text == "2024"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Master de l'Université Exemple | Autre chose", "both roles in one segment"),
        ("Université Exemple | Sciences", "no programme proven"),
        ("Master Data Science | Autre chose", "no institution proven"),
        ("Sciences | Autre chose", "neither role proven"),
    ],
)
def test_a_dateless_pair_without_exclusive_proofs_is_unparsed(
    value: str, reason: str
) -> None:
    reading = structure_education(value)

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_UNPARSED_V1, reason
    assert (
        reading.institution_text,
        reading.program_text,
        reading.period_text,
    ) == (None, None, None)


@pytest.mark.parametrize("marker", sorted(PROGRAM_MARKERS))
def test_every_closed_marker_names_a_programme(marker: str) -> None:
    assert names_a_program(f"{marker} en Test")
    assert names_a_program(marker.upper())


@pytest.mark.parametrize(
    "segment",
    [
        "Sciences",
        "Data Science",
        "Masterclass",
        "Licences",
        "Diplômes",
        "Université Exemple",
        "2020 - 2022",
    ],
)
def test_a_word_outside_the_programme_registry_proves_no_programme(
    segment: str,
) -> None:
    """No stem, no plural folding, no prefix match and no fuzzy comparison."""
    assert not names_a_program(segment)


def test_the_two_registries_do_not_overlap() -> None:
    """A word proving both roles at once would prove neither, by construction."""
    assert not set(INSTITUTION_MARKERS) & set(PROGRAM_MARKERS)


@pytest.mark.parametrize("marker", sorted(INSTITUTION_MARKERS))
def test_every_closed_marker_names_an_institution(marker: str) -> None:
    assert names_an_institution(f"{marker} de Test")
    assert names_an_institution(marker.upper())


@pytest.mark.parametrize(
    "segment",
    [
        "Data Science",
        "Master 2",
        "Universitaire",
        "Universités",
        "Schooling",
        "Institutionnel",
        "2020 - 2022",
    ],
)
def test_a_word_outside_the_closed_registry_names_no_institution(
    segment: str,
) -> None:
    """No stem, no plural folding, no prefix match and no fuzzy comparison."""
    assert not names_an_institution(segment)


def test_the_segments_are_trimmed_and_otherwise_untouched() -> None:
    reading = structure_education(
        "   Master   2  Data Science   |   École  Normale   |  2020 - 2022  "
    )

    assert reading.program_text == "Master   2  Data Science"
    assert reading.institution_text == "École  Normale"


def test_the_lines_after_the_header_are_kept_whole_and_in_order() -> None:
    reading = structure_education(
        "Master | Université de Paris | 2020 - 2022\n"
        "Mention très bien\n"
        "\n"
        "Mémoire de recherche"
    )

    assert reading.description_text == (
        "Mention très bien\n\nMémoire de recherche"
    )


# --------------------------------------------------------------------------
# EDUCATION — two segments, no date, one marker
# --------------------------------------------------------------------------


def test_two_segments_and_one_marker_name_the_school_and_the_programme() -> None:
    """A CV writes a diploma and a school on one line as often as with a date.

    Two written segments are a structure the document put there. They still do
    not say which is which — the closed registry does.
    """
    reading = structure_education("Master fictif | Université Exemple")

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_INSTITUTION_PROGRAM_V1
    )
    assert reading.institution_text == "Université Exemple"
    assert reading.program_text == "Master fictif"
    assert reading.period_text is None
    assert reading.description_text is None
    assert reading.structurer_version == STRUCTURED_PROFILE_VERSION


def test_the_two_segment_reading_follows_the_marker_and_not_the_position() -> None:
    reading = structure_education("Institute Example | Master fictif")

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_INSTITUTION_PROGRAM_V1
    )
    assert reading.institution_text == "Institute Example"
    assert reading.program_text == "Master fictif"


def test_the_two_segment_reading_is_the_same_in_either_order() -> None:
    school_first = structure_education("Université Exemple | Master fictif")
    programme_first = structure_education("Master fictif | Université Exemple")

    assert (
        school_first.institution_text
        == programme_first.institution_text
        == "Université Exemple"
    )
    assert (
        school_first.program_text
        == programme_first.program_text
        == "Master fictif"
    )


def test_the_two_segment_reading_keeps_the_following_lines() -> None:
    reading = structure_education(
        "Master fictif | Université Exemple\nMention très bien\n\nMémoire"
    )

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_INSTITUTION_PROGRAM_V1
    )
    assert reading.description_text == "Mention très bien\n\nMémoire"


def test_the_two_segments_are_trimmed_and_otherwise_untouched() -> None:
    reading = structure_education("  Master   fictif  |   Université  Exemple ")

    assert reading.program_text == "Master   fictif"
    assert reading.institution_text == "Université  Exemple"


def test_no_year_is_looked_for_inside_a_two_segment_header() -> None:
    """A header with no date is a header with no date."""
    reading = structure_education("Master fictif 2022 | Université Exemple")

    assert reading.period_text is None
    assert reading.program_text == "Master fictif 2022"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Autre chose | Encore autre chose", "no marker at all"),
        ("Université Exemple | École Exemple", "two markers: still nothing says"),
        ("Master fictif |   ", "an empty segment"),
        ("  | Université Exemple", "an empty segment"),
        ("- Master fictif | Université Exemple", "a list marker opens it"),
        ("Master fictif, Université Exemple", "a comma is not a pipe"),
    ],
)
def test_a_two_segment_header_the_registry_cannot_answer_stays_unparsed(
    value: str, reason: str
) -> None:
    reading = structure_education(value)

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_UNPARSED_V1, reason
    assert (
        reading.institution_text,
        reading.program_text,
        reading.period_text,
        reading.description_text,
    ) == (None, None, None, None)


@pytest.mark.parametrize(
    "value",
    [
        "Université Exemple | 2020 - 2022",
        "2020 - 2022 | Université Exemple",
        "Programme fictif | 2023/2024",
        "2020 | 2022",
    ],
)
def test_a_two_segment_header_holding_a_date_is_never_read(value: str) -> None:
    """Calling a date a programme is exactly the invention this package refuses.

    Two segments carry no evidence of what the unmarked one is *for*, and when
    that unmarked one is a date, reading it as a programme would be a plain
    falsehood. The fact keeps its wording and the row keeps its `NULL`s.
    """
    reading = structure_education(value)

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_UNPARSED_V1
    assert (reading.institution_text, reading.program_text) == (None, None)
    assert reading.period_text is None


def test_a_three_segment_header_with_no_date_is_still_not_read() -> None:
    """The third segment would have no honest home, so the reading declines."""
    reading = structure_education("Master fictif | Université Exemple | Ville")

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_UNPARSED_V1


# --------------------------------------------------------------------------
# EDUCATION — the period is certain and the rest is not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Master 2 Data Science | Promotion 2020 | 2020 - 2022", "no marker at all"),
        ("École Normale | Institut Curie | 2020 - 2022", "two marked segments"),
        ("Master | Université de Paris | Paris | 2020 - 2022", "a third segment"),
        ("2020 - 2022 | Data Science | Paris", "no marker, period first"),
    ],
)
def test_an_undecidable_header_keeps_only_the_period(value: str, reason: str) -> None:
    """The certain part is preserved; the uncertain part is not invented."""
    reading = structure_education(value)

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_PERIOD_ONLY_V1
    ), reason
    assert reading.institution_text is None
    assert reading.program_text is None
    assert reading.period_text is not None


def test_a_period_only_reading_still_keeps_the_following_lines() -> None:
    reading = structure_education(
        "Master 2 Data Science | Promotion 2020 | 2020 - 2022\nMention très bien"
    )

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_PERIOD_ONLY_V1
    )
    assert reading.description_text == "Mention très bien"


def test_a_school_year_is_kept_verbatim_and_never_becomes_two_dates() -> None:
    reading = structure_education("Licence | Université de Lyon | 2023/2024")

    assert reading.period_text == "2023/2024"


# --------------------------------------------------------------------------
# EDUCATION — declining altogether
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Master 2 Data Science, Université de Paris, 2020-2022",
        "Master 2 Data Science | Autre chose",
        "Master | Université de Paris | 2020 | 2021",
        "Master | Université de Paris | Paris",
        "Master | | 2020 - 2022",
        "- Master | Université de Paris | 2020 - 2022",
        "Diplômée de l'Université de Paris en 2022",
        "",
    ],
)
def test_an_unreadable_education_keeps_every_fragment_null(value: str) -> None:
    reading = structure_education(value)

    assert reading.structuring_rule_id is StructuringRule.EDUCATION_UNPARSED_V1
    assert (
        reading.institution_text,
        reading.program_text,
        reading.period_text,
        reading.description_text,
    ) == (None, None, None, None)


def test_no_study_level_and_no_bac_plus_is_ever_derived() -> None:
    """"Master" is a word the document wrote. It is not `Bac+5`."""
    reading = structure_education("Master 2 | Université de Paris | 2020 - 2022")

    assert reading.program_text == "Master 2"
    assert "Bac" not in str(reading)
    assert not hasattr(reading, "degree_level")
    assert not hasattr(reading, "study_level")
    assert not hasattr(reading, "graduation_year")


def test_a_diploma_is_never_deduced_from_a_school() -> None:
    """A marker on one segment says nothing about the other one.

    `Sciences` is a subject, not a proven programme, so nothing says the
    document meant it as one. The period is still certain, so the period is
    what is kept.
    """
    reading = structure_education("Université de Paris | Sciences | 2020 - 2022")

    assert reading.structuring_rule_id is (
        StructuringRule.EDUCATION_PIPE_PERIOD_ONLY_V1
    )
    assert reading.program_text is None
    assert reading.institution_text is None
    assert reading.period_text == "2020 - 2022"


def test_a_school_is_never_deduced_from_a_sentence() -> None:
    reading = structure_education("Master obtenu à Paris en 2022")

    assert reading.institution_text is None


# --------------------------------------------------------------------------
# CERTIFICATION
# --------------------------------------------------------------------------


def test_an_explicit_label_alone_names_the_certification() -> None:
    reading = structure_certification("Certification : Test Cloud Practitioner")

    assert reading.structuring_rule_id is StructuringRule.CERTIFICATION_EXPLICIT_V1
    assert reading.certification_text == "Test Cloud Practitioner"
    assert reading.issuer_text is None
    assert reading.period_text is None


def test_a_label_an_issuer_and_a_period_are_all_read() -> None:
    reading = structure_certification(
        "Certification : Test Cloud | Délivré par : Organisme Test | 2023\n"
        "Identifiant public"
    )

    assert reading.structuring_rule_id is StructuringRule.CERTIFICATION_EXPLICIT_V1
    assert reading.certification_text == "Test Cloud"
    assert reading.issuer_text == "Organisme Test"
    assert reading.period_text == "2023"
    assert reading.description_text == "Identifiant public"


@pytest.mark.parametrize(
    "value",
    [
        "Certificat : Test Cloud | Issued by : Organisme Test",
        "certificate: Test Cloud | ISSUER : Organisme Test",
        "• Certification : Test Cloud | Organisme : Organisme Test",
    ],
)
def test_the_labels_are_compared_folded_and_the_values_stay_verbatim(
    value: str,
) -> None:
    reading = structure_certification(value)

    assert reading.certification_text == "Test Cloud"
    assert reading.issuer_text == "Organisme Test"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Test Cloud Practitioner", "no label at all"),
        ("Test Cloud | Organisme Test | 2023", "pipes, but no label"),
        ("Diplôme : Test Cloud", "a label outside the closed registry"),
        ("Certification : Test Cloud | Test Data", "one unreadable segment"),
        ("Certification : A | Certificat : B", "the name label twice"),
        ("Certification : A | Issuer : B | Organisme : C", "the issuer twice"),
        ("Certification : A | 2023 | 2024", "two periods"),
        ("Certification :", "an empty value"),
        ("", "an empty fact"),
    ],
)
def test_an_unreadable_certification_keeps_every_fragment_null(
    value: str, reason: str
) -> None:
    reading = structure_certification(value)

    assert reading.structuring_rule_id is (
        StructuringRule.CERTIFICATION_UNPARSED_V1
    ), reason
    assert (
        reading.certification_text,
        reading.issuer_text,
        reading.period_text,
        reading.description_text,
    ) == (None, None, None, None)


@pytest.mark.parametrize(
    "value",
    [
        "Préparation à la certification Test Cloud",
        "Certification : Test Cloud (en préparation)",
        "Objectif : certification Test Cloud",
        "Certification : Test Cloud\nObjectif 2026",
        "Certification Test Cloud prévue en 2026",
        "Certification : Test Cloud | Délivré par : Organisme Test | planned",
    ],
)
def test_a_stated_intention_is_never_read_as_an_obtained_certification(
    value: str,
) -> None:
    """A plan is not a credential, wherever in the fact the plan is written."""
    reading = structure_certification(value)

    assert reading.structuring_rule_id is StructuringRule.CERTIFICATION_UNPARSED_V1
    assert reading.certification_text is None
    assert states_an_intention(value)


def test_no_issuer_and_no_date_is_ever_invented() -> None:
    reading = structure_certification("Certification : Test Cloud Practitioner")

    assert reading.issuer_text is None
    assert reading.period_text is None
    assert not hasattr(reading, "obtained_at")
    assert not hasattr(reading, "expires_at")
    assert not hasattr(reading, "obtained")


def test_the_word_certification_in_a_sentence_proves_nothing() -> None:
    reading = structure_certification(
        "Formation suivie en vue d'une certification cloud"
    )

    assert reading.structuring_rule_id is StructuringRule.CERTIFICATION_UNPARSED_V1


# --------------------------------------------------------------------------
# LANGUAGE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "language", "proficiency"),
    [
        ("Anglais : C1", "Anglais", "C1"),
        ("Anglais - C1", "Anglais", "C1"),
        ("Anglais – B2", "Anglais", "B2"),
        ("Anglais | A2", "Anglais", "A2"),
        ("Anglais (C2)", "Anglais", "C2"),
        ("Espagnol : courant", "Espagnol", "courant"),
        ("Allemand : débutant", "Allemand", "débutant"),
        ("English: fluent", "English", "fluent"),
        ("Arabe : bilingue", "Arabe", "bilingue"),
        ("Langue des signes : intermédiaire", "Langue des signes", "intermédiaire"),
    ],
)
def test_an_explicit_separator_and_a_closed_form_are_read(
    value: str, language: str, proficiency: str
) -> None:
    reading = structure_language(value)

    assert reading.structuring_rule_id is (
        StructuringRule.LANGUAGE_EXPLICIT_PROFICIENCY_V1
    )
    assert reading.language_text == language
    assert reading.proficiency_text == proficiency


@pytest.mark.parametrize(
    ("value", "language", "proficiency"),
    [
        ("• Langue fictive : C1", "Langue fictive", "C1"),
        ("- Example Language: fluent", "Example Language", "fluent"),
        ("* Langue fictive | avancé", "Langue fictive", "avancé"),
        ("▪ Langue fictive (B2)", "Langue fictive", "B2"),
        ("•Langue fictive : C1", "Langue fictive", "C1"),
        ("—  Example Language : native", "Example Language", "native"),
    ],
)
def test_one_list_marker_is_removed_before_the_line_is_split(
    value: str, language: str, proficiency: str
) -> None:
    """A CV writes its languages as a bulleted list as often as not.

    The Phase 3.2B extractor keeps the source text of a bullet block, so a fact
    can reach the structurer reading `• Anglais : C1`. The marker is
    punctuation the list wrote, not part of the language's name.
    """
    reading = structure_language(value)

    assert reading.structuring_rule_id is (
        StructuringRule.LANGUAGE_EXPLICIT_PROFICIENCY_V1
    )
    assert reading.language_text == language
    assert reading.proficiency_text == proficiency


def test_a_line_without_a_list_marker_is_unchanged_by_the_removal() -> None:
    with_marker = structure_language("• Langue fictive : C1")
    without_marker = structure_language("Langue fictive : C1")

    assert without_marker.language_text == with_marker.language_text
    assert without_marker.proficiency_text == with_marker.proficiency_text == "C1"


def test_exactly_one_list_marker_is_removed_and_never_two() -> None:
    """A second marker is content, and it stays where the document put it."""
    reading = structure_language("-- Example Language : fluent")

    assert reading.language_text == "- Example Language"
    assert reading.proficiency_text == "fluent"


def test_the_level_stays_verbatim_behind_a_list_marker() -> None:
    reading = structure_language("• Langue fictive : COURANT")

    assert reading.proficiency_text == "COURANT"
    assert "C1" not in str(reading)


@pytest.mark.parametrize(
    "value",
    [
        "• Langue fictive courante",
        "- Example Language: very good",
        "• Langue fictive",
        "•",
        "• : C1",
    ],
)
def test_a_bulleted_line_that_is_still_ambiguous_stays_unparsed(value: str) -> None:
    """Removing the marker is not a reason to be less strict about the rest."""
    reading = structure_language(value)

    assert reading.structuring_rule_id is StructuringRule.LANGUAGE_UNPARSED_V1
    assert (reading.language_text, reading.proficiency_text) == (None, None)


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("Anglais : c1", "c1"),
        ("Anglais : C1", "C1"),
        ("Anglais : COURANT", "COURANT"),
        ("Anglais : Courant", "Courant"),
        ("Anglais : AVANCÉ", "AVANCÉ"),
    ],
)
def test_case_is_folded_to_compare_and_never_to_store(value: str, stored: str) -> None:
    """The registry decides whether it is a level. It never rewrites one."""
    reading = structure_language(value)

    assert reading.proficiency_text == stored


@pytest.mark.parametrize("form", sorted(PROFICIENCY_FORMS))
def test_every_closed_form_is_recognised_whole(form: str) -> None:
    assert is_explicit_proficiency(form)
    assert is_explicit_proficiency(form.upper())
    assert not is_explicit_proficiency(f"{form} professionnel")


def test_courant_is_never_turned_into_a_cefr_level() -> None:
    reading = structure_language("Anglais : courant")

    assert reading.proficiency_text == "courant"
    assert "C1" not in str(reading)
    assert not hasattr(reading, "cefr_level")
    assert not hasattr(reading, "level")


def test_fluent_is_never_turned_into_a_cefr_level() -> None:
    reading = structure_language("English : fluent")

    assert reading.proficiency_text == "fluent"
    assert "C2" not in str(reading)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Anglais courant", "no separator"),
        ("Franco-Anglais", "a dash the document did not space"),
        ("Anglais : niveau professionnel", "a wording outside the registry"),
        ("Anglais : lu, écrit, parlé", "a wording outside the registry"),
        ("Anglais : très bon niveau", "a wording outside the registry"),
        ("Anglais", "a language and nothing else"),
        (" : C1", "an empty language"),
        ("Anglais :", "an empty level"),
        ("Anglais : C1\nTOEIC 900", "a second line this table cannot keep"),
        ("", "an empty fact"),
    ],
)
def test_an_unreadable_language_keeps_both_fragments_null(
    value: str, reason: str
) -> None:
    reading = structure_language(value)

    assert reading.structuring_rule_id is StructuringRule.LANGUAGE_UNPARSED_V1, reason
    assert (reading.language_text, reading.proficiency_text) == (None, None)


def test_a_language_is_never_deduced_from_a_text_written_in_one() -> None:
    """An English sentence in a fact is a sentence, not a declared language."""
    reading = structure_language("Built an English-language reporting pipeline")

    assert reading.structuring_rule_id is StructuringRule.LANGUAGE_UNPARSED_V1
    assert reading.language_text is None


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reader", "value"),
    [
        (structure_education, "Master | Université de Paris | 2020 - 2022"),
        (structure_education, "Prose libre sur des études"),
        (structure_certification, "Certification : Test Cloud | 2023"),
        (structure_certification, "Prose libre sur une certification"),
        (structure_language, "Anglais : C1"),
        (structure_language, "Prose libre sur une langue"),
    ],
)
def test_reading_the_same_value_twice_gives_the_same_reading(reader, value) -> None:
    assert reader(value) == reader(value)


def test_the_registries_stay_small_closed_and_free_of_duplicates() -> None:
    """A registry that grows by guesswork is a lexicon of hallucinations."""
    for registry in (INSTITUTION_MARKERS, PROGRAM_MARKERS, PROFICIENCY_FORMS):
        assert len(registry) == len(set(registry))
        assert len(registry) <= 25
        assert all(entry == entry.casefold().strip() for entry in registry)
