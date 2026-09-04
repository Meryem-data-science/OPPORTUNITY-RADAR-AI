"""Unit coverage for the Phase 3.5A constraint extractor.

Every posting below is invented for these tests. No real listing, no real
company and no real description takes part, nothing here opens a database, and
the operational `.data/` database is never touched.

Most of these tests are about what the extractor **refuses** to say. The
failure mode that matters is not a missed requirement — that is an UNKNOWN, and
UNKNOWN is an honest answer — it is a requirement the extractor invented, which
reads exactly like one the posting wrote.
"""

import pytest

from services.collector.extractors.opportunity_constraints.rules import (
    TYPE_FIELD_PRECEDENCE,
)
from services.collector.extractors.opportunity_constraints.extractor import (
    FINGERPRINT_FIELDS,
    extract_opportunity_constraints,
    source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
    MAX_EVIDENCE_LENGTH,
    MULTI_VALUED_SLOTS,
    SLOT_KINDS,
    SPECIFIC_INTERNSHIP_TYPES,
    ConstraintConflict,
    ConstraintKind,
    ConventionRequirement,
    EducationLevel,
    EducationRequirementMode,
    ExperienceObligation,
    ExperienceRequirement,
    OpportunityConstraintError,
    OpportunitySource,
    OpportunityType,
    Slot,
    SourceField,
    StartPrecision,
    more_specific_internship,
    VisaSponsorship,
    WorkAuthorization,
    WorkMode,
)
from services.collector.extractors.opportunity_constraints.text import (
    normalize_description,
    segments,
)


def read(description=None, *, title=None, **fields):
    """One invented posting, extracted."""
    return extract_opportunity_constraints(
        OpportunitySource(
            opportunity_id=1, canonical_title=title, description=description, **fields
        )
    )


# --------------------------------------------------------------------------
# The shared registries
# --------------------------------------------------------------------------


def test_the_opportunity_type_registry_is_the_one_the_profile_side_uses() -> None:
    """One vocabulary, or Phase 3.6 compares two things that merely look alike."""
    from services.digital_twin.preferences.models import (
        OpportunityType as ProfileOpportunityType,
        WorkMode as ProfileWorkMode,
    )

    assert OpportunityType is ProfileOpportunityType
    assert WorkMode is ProfileWorkMode


def test_the_extractor_version_is_the_documented_one() -> None:
    assert EXTRACTOR_VERSION == "opportunity-constraints-v4"


# --------------------------------------------------------------------------
# Reading the text the collectors actually store
# --------------------------------------------------------------------------


def test_an_escaped_html_description_is_read_as_text() -> None:
    """Greenhouse stores entity-escaped HTML, and nothing upstream unescapes it."""
    escaped = "&lt;ul&gt;&lt;li&gt;Minimum 3 years of experience&lt;/li&gt;&lt;/ul&gt;"

    assert normalize_description(escaped) == "Minimum 3 years of experience"
    assert read(escaped).experience[0].min_months == 36


def test_block_tags_keep_two_requirements_apart() -> None:
    text = normalize_description("<li>Bac+5 required</li><li>Visa sponsorship available</li>")
    assert segments(text) == ("Bac+5 required", "Visa sponsorship available")


def test_an_ampersand_the_posting_wrote_survives() -> None:
    assert normalize_description("<p>R&amp;D team</p>") == "R&D team"


def test_a_missing_description_is_empty_text_and_asserts_nothing() -> None:
    result = read(None)
    assert result.evidence == ()
    assert result.opportunity_type is None
    assert result.visa_sponsorship is VisaSponsorship.UNKNOWN


# --------------------------------------------------------------------------
# UNKNOWN is the default, and UNKNOWN is never FALSE
# --------------------------------------------------------------------------


def test_a_posting_that_says_nothing_asserts_nothing() -> None:
    result = read("We are hiring. Join a great team and build good products.")

    assert result.opportunity_type is None
    assert result.education == ()
    assert result.experience == ()
    assert not result.duration.known
    assert not result.start.known
    assert result.work_mode is None
    assert result.visa_sponsorship is VisaSponsorship.UNKNOWN
    assert result.work_authorization is WorkAuthorization.UNKNOWN
    assert result.convention is ConventionRequirement.UNKNOWN
    assert result.conflicts == ()


def test_a_title_naming_seniority_states_no_number_of_years() -> None:
    """"Senior" is an adjective. It is not "five years", and never becomes one."""
    result = read("We are looking for an experienced professional.", title="Senior Data Scientist")

    assert result.experience == ()


def test_a_description_written_in_english_does_not_require_english() -> None:
    """No rule inspects the language of the text, so none can require it."""
    result = read("We are looking for a talented engineer to join our team in Berlin.")

    rules = {evidence.rule_id for evidence in result.evidence}
    assert not any("LANGUAGE" in rule for rule in rules)
    assert all(evidence.kind is not ConstraintKind.EDUCATION for evidence in result.evidence)


def test_a_location_alone_is_not_an_attendance_policy() -> None:
    """An address says where the company is, not that the desk is mandatory."""
    result = read("Join our Casablanca office team.", location="Casablanca", country="Morocco")

    assert result.work_mode is None
    assert result.locations == ("Casablanca", "Morocco")


def test_a_country_is_not_a_visa_policy() -> None:
    result = read("This role is based in the United States.", country="United States")

    assert result.visa_sponsorship is VisaSponsorship.UNKNOWN
    assert result.work_authorization is WorkAuthorization.UNKNOWN


def test_an_internship_does_not_imply_a_convention_a_duration_or_a_student() -> None:
    result = read("Internship opportunity in our data team.", title="Data Internship")

    assert result.opportunity_type is OpportunityType.INTERNSHIP
    assert result.convention is ConventionRequirement.UNKNOWN
    assert not result.duration.known
    assert result.education == ()


def test_a_pfe_does_not_imply_a_convention() -> None:
    result = read("PFE in data engineering.", title="PFE Data Engineer")

    assert result.opportunity_type is OpportunityType.PFE
    assert result.convention is ConventionRequirement.UNKNOWN


# --------------------------------------------------------------------------
# Opportunity type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    (
        ("PFE Data Engineer", OpportunityType.PFE),
        ("Projet de fin d'études - Data", OpportunityType.PFE),
        ("PFA en machine learning", OpportunityType.PFA),
        ("Alternance Data Analyst", OpportunityType.ALTERNANCE),
        ("Data Apprenticeship", OpportunityType.ALTERNANCE),
        ("Summer Internship - Analytics", OpportunityType.SUMMER_INTERNSHIP),
        ("Stage pré-embauche Data", OpportunityType.PRE_HIRE_INTERNSHIP),
        ("Data Science Internship", OpportunityType.INTERNSHIP),
        ("Graduate Programme Data", OpportunityType.FIRST_JOB),
        ("Junior Data Engineer", OpportunityType.JUNIOR_ROLE),
    ),
)
def test_an_explicit_title_names_the_type(title, expected) -> None:
    assert read("A role in our team.", title=title).opportunity_type is expected


def test_the_title_settles_the_type_against_a_coarser_description() -> None:
    """A PFE described as an internship is one thing at two grains."""
    result = read("This 6 months internship is based in Rabat.", title="PFE Data Engineer")

    assert result.opportunity_type is OpportunityType.PFE
    assert result.conflicts == ()


def test_the_classifier_reading_is_used_only_where_it_is_unambiguous() -> None:
    assert read(None, qualification_type="APPRENTICESHIP").opportunity_type is (
        OpportunityType.ALTERNANCE
    )
    # `GRADUATE` sits between FIRST_JOB and JUNIOR_ROLE and `JOB` between those
    # and nothing: neither is mapped, because picking one would invent it.
    assert read(None, qualification_type="GRADUATE").opportunity_type is None
    assert read(None, qualification_type="JOB").opportunity_type is None
    assert read(None, qualification_type="UNKNOWN").opportunity_type is None


def test_a_description_type_loses_to_the_title_but_is_used_alone() -> None:
    assert read("This is an alternance contract.").opportunity_type is (
        OpportunityType.ALTERNANCE
    )


# --------------------------------------------------------------------------
# A type wording is a word, not a run of characters (Phase 7B.2)
#
# The operational corpus caught this one. `v3` looked for a type wording with
# `folded.find(signal)`, so `pfe` matched wherever those three characters
# happened to sit — and they sit inside ordinary German words. Consulting and
# marketing postings were stored as `PFE`, each carrying an evidence fragment
# that named no PFE, and the Phase 2 classifier read the same postings as
# `JOB`.
#
# The tests below say the cause, not the postings: no opportunity id appears
# here, and none may. A rule keyed on an id would fix three rows and leave the
# class of bug in place.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    (
        # "Handlungsempfehlungen" contains "mpfeh" — and so "pfe".
        "Sie leiten daraus konkrete Handlungsempfehlungen ab.",
        # "auszuschöpfen" ends in "öpfen"; the accented letter is a word
        # character, which is exactly why the boundary holds.
        "Um das Potenzial der Daten voll auszuschöpfen.",
        "Sie geben Empfehlungen zur Datenstrategie.",
        # And the plain shapes of the same mistake, in any language.
        "The XPFE module is part of our stack.",
        "The internal codename for this workstream is PFE2.",
    ),
    ids=("Handlungsempfehlungen", "auszuschopfen", "Empfehlungen", "XPFE", "PFE2"),
)
def test_pfe_inside_a_longer_word_is_not_a_pfe(description) -> None:
    """A substring is not a claim, so the honest answer is UNKNOWN."""
    result = read(description, title="Senior Consultant Data & AI Strategy")

    assert result.opportunity_type is None
    assert not [
        evidence
        for evidence in result.evidence
        if evidence.kind is ConstraintKind.OPPORTUNITY_TYPE
    ]


@pytest.mark.parametrize(
    "description",
    (
        "PFE",
        "PFE Data Scientist recherché.",
        "Stage PFE de 6 mois dans notre équipe data.",
        "Projet de fin d'études en data engineering.",
        "Nous proposons un (PFE) encadré.",
        "Il s'agit d'un PFE, encadré par un ingénieur.",
        "Ce poste est un PFE.",
        "PFE: sujet en machine learning.",
    ),
)
def test_an_explicit_pfe_wording_is_still_a_pfe(description) -> None:
    """Punctuation is not a word character, so it never hides a wording."""
    assert read(description).opportunity_type is OpportunityType.PFE


def test_a_final_year_internship_is_read_as_the_registry_spells_it() -> None:
    result = read("A final-year internship in our data team.")

    assert result.opportunity_type is OpportunityType.PFE


@pytest.mark.parametrize(
    "description,expected",
    (
        ("This internship runs in our data team.", OpportunityType.INTERNSHIP),
        ("Offre de stage data à Casablanca.", OpportunityType.INTERNSHIP),
        ("Nous recherchons un stagiaire data.", OpportunityType.INTERNSHIP),
        ("Ce poste est en alternance.", OpportunityType.ALTERNANCE),
        ("Un contrat d'apprentissage de 12 mois.", OpportunityType.ALTERNANCE),
        ("Il s'agit d'un PFA encadré.", OpportunityType.PFA),
        ("A summer internship in analytics.", OpportunityType.SUMMER_INTERNSHIP),
        ("This is a pre-hire internship.", OpportunityType.PRE_HIRE_INTERNSHIP),
    ),
)
def test_the_other_types_still_read_from_a_description(description, expected) -> None:
    """The boundary is a guard on the wordings, not a narrowing of them."""
    assert read(description).opportunity_type is expected


@pytest.mark.parametrize(
    "title,expected",
    (
        ("Junior Data Engineer", OpportunityType.JUNIOR_ROLE),
        ("Graduate Programme Data", OpportunityType.FIRST_JOB),
        ("Entry-level Data Analyst", OpportunityType.FIRST_JOB),
        ("Data Science Internship", OpportunityType.INTERNSHIP),
        ("Alternance Data Analyst", OpportunityType.ALTERNANCE),
    ),
)
def test_the_title_only_types_still_read_from_a_title(title, expected) -> None:
    assert read("A role in our team.", title=title).opportunity_type is expected


def test_a_derived_word_is_not_the_wording_it_derives_from() -> None:
    """`stage` does not imply `stages`, and that is the point.

    A boundary-safe wording no longer arrives with its whole morphology for
    free. That is the invariant, not a regression: a variant the corpus shows
    is needed becomes its own registry entry, argued for like every other one.
    """
    assert read("Notre plateforme gère les stagings de données.").opportunity_type is None
    assert read("The stagecoach museum is next door.").opportunity_type is None


def test_the_evidence_quotes_the_fragment_that_named_the_type() -> None:
    """Evidence is a pointer to a real wording, never to a chance substring."""
    result = read(
        "We advise clients on strategy.\nStage PFE de 6 mois à Casablanca.",
        title="Data Scientist",
    )

    type_evidence = [
        evidence
        for evidence in result.evidence
        if evidence.kind is ConstraintKind.OPPORTUNITY_TYPE
    ]
    assert len(type_evidence) == 1
    assert type_evidence[0].rule_id == "OPPORTUNITY_TYPE_PFE_V1"
    assert type_evidence[0].source_field is SourceField.DESCRIPTION
    assert type_evidence[0].text == "Stage PFE de 6 mois à Casablanca."
    assert type_evidence[0].normalized_value == "PFE"


def test_a_description_pfe_stands_even_where_the_classifier_read_a_job() -> None:
    """The classifier is not a veto: it is one source among three.

    Phase 7B.2 is about the quality of the textual evidence and nothing else.
    A description that says `Stage PFE` says it whatever a coarser reading of
    the title concluded, and `JOB` is not in the classifier map at all.
    """
    result = read(
        "Stage PFE de 6 mois dans notre équipe data.",
        title="Data Scientist",
        qualification_type="JOB",
    )

    assert result.opportunity_type is OpportunityType.PFE


def test_every_type_signal_is_matched_on_its_boundaries() -> None:
    """The guard is registry-wide, so a wording added later inherits it."""
    from services.collector.extractors.opportunity_constraints.rules import (
        _TYPE_SIGNALS,
        _TYPE_SIGNAL_PATTERNS,
    )

    declared = {
        signal for _type, _rule, signals, _ok in _TYPE_SIGNALS for signal in signals
    }
    assert declared == set(_TYPE_SIGNAL_PATTERNS)
    for signal, pattern in _TYPE_SIGNAL_PATTERNS.items():
        assert pattern.search(signal) is not None
        # Glued to a letter on either side, the wording is no longer a wording.
        assert pattern.search(f"x{signal}") is None
        assert pattern.search(f"{signal}x") is None
        # Punctuation and spaces are not word characters, so they never hide it.
        assert pattern.search(f"({signal}).") is not None


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------


def test_an_explicit_minimum_level_is_read_as_a_floor() -> None:
    result = read("Education: Bac+3 minimum required.")

    assert result.education == (
        __import__(
            "services.collector.extractors.opportunity_constraints.models",
            fromlist=["EducationRequirement"],
        ).EducationRequirement(EducationLevel.BAC_PLUS_3, EducationRequirementMode.MINIMUM),
    )


def test_a_named_degree_without_a_floor_marker_is_exact() -> None:
    """"Master's degree" is a Master, not "Master or above"."""
    result = read("You hold a Master's degree in computer science.")

    assert [(item.level, item.mode) for item in result.education] == [
        (EducationLevel.MASTER, EducationRequirementMode.EXACT)
    ]


def test_several_accepted_levels_are_several_answers_not_a_conflict() -> None:
    result = read("Education required: Bac+5 or Master degree.")

    assert {item.level for item in result.education} == {
        EducationLevel.BAC_PLUS_5,
        EducationLevel.MASTER,
    }
    assert result.conflicts == ()


def test_bac_plus_5_and_master_stay_two_levels() -> None:
    """Merging them would be an equivalence nobody stated."""
    assert EducationLevel.BAC_PLUS_5 is not EducationLevel.MASTER


def test_the_word_master_outside_a_study_sentence_is_not_a_diploma() -> None:
    result = read("You will master our data pipeline and own it end to end.")

    assert result.education == ()


def test_student_is_not_a_level() -> None:
    """"Student" says somebody is studying, not that they hold Bac+5."""
    result = read("We welcome students from any background.")

    assert result.education == ()


# --------------------------------------------------------------------------
# Experience
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence,minimum,maximum",
    (
        ("3+ years of experience", 36, None),
        ("minimum 3 years of experience", 36, None),
        ("at least 6 months of experience", 6, None),
        ("1-2 years of experience", 12, 24),
        ("2 to 4 years of experience", 24, 48),
        ("au moins 2 ans d'expérience", 24, None),
    ),
)
def test_quantified_experience_is_normalized_to_months(sentence, minimum, maximum) -> None:
    result = read(sentence)

    assert len(result.experience) == 1
    assert result.experience[0].min_months == minimum
    assert result.experience[0].max_months == maximum


def test_a_number_with_no_experience_word_is_not_an_experience_requirement() -> None:
    result = read("We have 3 offices and 5 teams.")

    assert result.experience == ()


def test_experience_preferred_is_an_obligation_without_a_quantity() -> None:
    result = read("Prior experience with data pipelines is preferred.")

    assert [item.obligation for item in result.experience] == [
        ExperienceObligation.PREFERRED
    ]
    assert result.experience[0].min_months is None


def test_two_sentences_asking_for_experience_are_two_requirements() -> None:
    """Not one posting contradicting itself. The corpus is full of these."""
    result = read(
        "7+ years of engineering experience.\n"
        "2+ years of AI/ML production experience."
    )

    assert [item.min_months for item in result.experience] == [84, 24]
    assert result.conflicts == ()


def test_an_obligation_is_attached_to_the_experience_it_qualifies() -> None:
    result = read("Minimum 3 years of experience.\nExperience with Spark is preferred.")

    assert [
        (item.min_months, item.obligation) for item in result.experience
    ] == [
        (36, ExperienceObligation.REQUIRED),
        (None, ExperienceObligation.PREFERRED),
    ]
    assert result.conflicts == ()


def test_a_salary_range_never_becomes_a_required_experience() -> None:
    """The corpus case that made this rule necessary.

    A boilerplate paragraph about pay contains "minimum", and somewhere in the
    same sentence a word about careers or experience. `v1` read the two as
    "experience is required". "Minimum" qualified the salary.
    """
    result = read(
        "The range displayed reflects the minimum and maximum target for new "
        "hire salaries across career levels and experience."
    )

    assert result.experience == ()


def test_prior_experience_that_is_a_plus_is_preferred_and_not_required() -> None:
    """"…is a plus but not required" contains the word, and denies it."""
    result = read("Prior experience is a plus but not required.")

    assert [item.obligation for item in result.experience] == [
        ExperienceObligation.PREFERRED
    ]


def test_an_experience_requirement_must_require_something() -> None:
    with pytest.raises(OpportunityConstraintError):
        ExperienceRequirement()


# --------------------------------------------------------------------------
# Duration
# --------------------------------------------------------------------------


def test_an_explicit_duration_is_read() -> None:
    result = read("This internship lasts 6 months.")

    assert (result.duration.min_months, result.duration.max_months) == (6, 6)


def test_a_duration_range_keeps_both_bounds() -> None:
    result = read("Internship duration: 3 to 6 months.")

    assert (result.duration.min_months, result.duration.max_months) == (3, 6)


def test_a_week_count_that_divides_by_four_becomes_months() -> None:
    result = read("A 12-week internship in our data team.")

    assert (result.duration.min_months, result.duration.max_months) == (3, 3)


def test_a_week_count_that_does_not_divide_by_four_asserts_nothing() -> None:
    """Six weeks is a month and a half, and rounding it invents a bound."""
    assert not read("A 6-week internship in our data team.").duration.known


def test_an_experience_sentence_is_not_a_duration() -> None:
    result = read("This internship requires 3 years of experience.")

    assert not result.duration.known


def test_the_kind_of_posting_never_implies_a_duration() -> None:
    assert not read("Summer internship in analytics.", title="Summer Internship").duration.known


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------


def test_a_month_and_year_are_both_kept() -> None:
    result = read("Starting February 2027.")

    assert (result.start.year, result.start.month) == (2027, 2)
    assert result.start.precision is StartPrecision.MONTH


def test_a_month_without_a_year_receives_no_year() -> None:
    """Not this year, not next year, not the year the posting was published."""
    result = read("Starting in February.")

    assert result.start.month == 2
    assert result.start.year is None
    assert result.start.precision is StartPrecision.MONTH


def test_a_year_alone_is_read_at_year_precision() -> None:
    result = read("The mission starts in 2027.")

    assert (result.start.year, result.start.month) == (2027, None)
    assert result.start.precision is StartPrecision.YEAR


def test_an_iso_date_is_read_whole_and_validated() -> None:
    result = read("Starting 2027-02-01.")

    assert (result.start.year, result.start.month, result.start.day) == (2027, 2, 1)
    assert result.start.precision is StartPrecision.DATE


def test_an_impossible_iso_date_is_refused_rather_than_repaired() -> None:
    assert not read("Starting 2027-02-30.").start.known


def test_a_slashed_date_is_never_read() -> None:
    """01/02/2027 is two different days depending on the reader's country."""
    result = read("Starting 01/02/2027.")

    assert result.start.day is None


def test_a_date_without_a_start_word_is_not_a_start_date() -> None:
    assert not read("Our company was founded in 2019.").start.known


# --------------------------------------------------------------------------
# Work mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence,expected",
    (
        ("This is a fully remote position.", WorkMode.REMOTE),
        ("100% remote role.", WorkMode.REMOTE),
        ("This is a hybrid role.", WorkMode.HYBRID),
        ("This position is on-site.", WorkMode.ON_SITE),
        ("Poste en présentiel.", WorkMode.ON_SITE),
    ),
)
def test_an_explicit_work_mode_is_read(sentence, expected) -> None:
    assert read(sentence).work_mode is expected


def test_remote_within_a_country_is_still_remote() -> None:
    assert read("Fully remote within France.").work_mode is WorkMode.REMOTE


def test_a_collected_remote_type_is_used_when_the_collector_filled_it() -> None:
    result = read("A role in our team.", remote_type="hybrid")

    assert result.work_mode is WorkMode.HYBRID
    assert any(
        evidence.source_field is SourceField.REMOTE_TYPE for evidence in result.evidence
    )


def test_refusing_remote_is_not_claiming_on_site() -> None:
    """"No remote work" says where the work is not done, and stops there."""
    result = read("There is no remote work for this position.")

    assert result.work_mode is None
    assert result.conflicts == ()


# --------------------------------------------------------------------------
# Visa sponsorship, work authorization, convention
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    (
        "Visa sponsorship available for this role.",
        "We sponsor visas for international candidates.",
        "Visa sponsorship is available.",
    ),
)
def test_an_offer_to_sponsor_is_read(sentence) -> None:
    assert read(sentence).visa_sponsorship is VisaSponsorship.AVAILABLE


@pytest.mark.parametrize(
    "sentence",
    (
        "We do not sponsor visas.",
        "No sponsorship available for this position.",
        "We are unable to sponsor work visas.",
        "Visa sponsorship is not available.",
        "This role is offered without sponsorship.",
    ),
)
def test_a_refusal_to_sponsor_is_read_as_a_refusal(sentence) -> None:
    """The negation is checked first: "no sponsorship available" contains
    "sponsorship available", and a reader who looked for the positive first
    would report the opposite of what the posting says."""
    assert read(sentence).visa_sponsorship is VisaSponsorship.NOT_AVAILABLE


def test_silence_about_visas_is_not_a_refusal() -> None:
    assert read("Join our international team.").visa_sponsorship is VisaSponsorship.UNKNOWN


@pytest.mark.parametrize(
    "sentence",
    (
        "You must be authorized to work in the US.",
        "Candidates must have the right to work in France.",
        "A valid work permit required.",
    ),
)
def test_required_authorization_is_read(sentence) -> None:
    assert read(sentence).work_authorization is WorkAuthorization.REQUIRED


def test_required_authorization_is_not_a_refusal_to_sponsor() -> None:
    """Two different claims. A company can require both, or say only one."""
    result = read("You must be authorized to work in the United States.")

    assert result.work_authorization is WorkAuthorization.REQUIRED
    assert result.visa_sponsorship is VisaSponsorship.UNKNOWN


def test_sponsorship_and_authorization_can_both_be_stated() -> None:
    result = read(
        "You must be authorized to work in Germany.\nVisa sponsorship available."
    )

    assert result.work_authorization is WorkAuthorization.REQUIRED
    assert result.visa_sponsorship is VisaSponsorship.AVAILABLE


@pytest.mark.parametrize(
    "sentence",
    (
        "Convention de stage obligatoire.",
        "An internship agreement is required.",
        "Stage conventionné uniquement.",
    ),
)
def test_an_explicit_convention_requirement_is_read(sentence) -> None:
    assert read(sentence).convention is ConventionRequirement.REQUIRED


def test_an_explicit_absence_of_convention_is_read() -> None:
    assert read("No internship agreement required.").convention is (
        ConventionRequirement.NOT_REQUIRED
    )


# --------------------------------------------------------------------------
# Contradictions
# --------------------------------------------------------------------------


def test_two_contradicting_work_modes_assert_nothing() -> None:
    result = read("This is a fully remote position.\nThis role is fully on-site.")

    assert result.work_mode is None
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.slot is Slot.WORK_MODE
    assert conflict.kind is ConstraintKind.WORK_MODE
    assert conflict.values == ("ON_SITE", "REMOTE")
    assert len(conflict.rule_ids) == 2


def test_a_contradiction_keeps_the_evidence_that_disagreed() -> None:
    """A conflict nobody can inspect is not auditable."""
    result = read("Fully remote position.\nThis role is fully on-site.")

    modes = result.evidence_for(ConstraintKind.WORK_MODE)
    assert {evidence.normalized_value for evidence in modes} == {"REMOTE", "ON_SITE"}


def test_contradicting_sponsorship_statements_assert_nothing() -> None:
    result = read("Visa sponsorship available.\nWe do not sponsor visas.")

    assert result.visa_sponsorship is VisaSponsorship.UNKNOWN
    assert result.conflicting_kinds == {ConstraintKind.VISA_SPONSORSHIP}


def test_a_contradiction_in_one_field_leaves_the_others_alone() -> None:
    result = read(
        "Fully remote position.\nThis role is fully on-site.\n"
        "Minimum 3 years of experience.\nVisa sponsorship available."
    )

    assert result.work_mode is None
    assert result.experience[0].min_months == 36
    assert result.visa_sponsorship is VisaSponsorship.AVAILABLE


def test_the_same_value_stated_twice_is_not_a_contradiction() -> None:
    result = read("This is a fully remote position.\nThe role is remote-first.")

    assert result.work_mode is WorkMode.REMOTE
    assert result.conflicts == ()


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_every_asserted_value_carries_a_rule_and_a_fragment() -> None:
    result = read(
        "Minimum 3 years of experience required.\nVisa sponsorship available.\n"
        "Convention de stage obligatoire."
    )

    kinds = {evidence.kind for evidence in result.evidence}
    assert ConstraintKind.EXPERIENCE in kinds
    assert ConstraintKind.VISA_SPONSORSHIP in kinds
    assert ConstraintKind.CONVENTION in kinds
    for evidence in result.evidence:
        assert evidence.rule_id.strip()
        assert evidence.text.strip()


def test_evidence_is_a_fragment_and_never_a_copy_of_the_posting() -> None:
    long_posting = (
        "We are a company. " * 60 + "Minimum 3 years of experience required."
    )
    result = read(long_posting)

    assert result.experience[0].min_months == 36
    for evidence in result.evidence:
        assert len(evidence.text) <= MAX_EVIDENCE_LENGTH


def test_evidence_names_the_field_it_was_read_from() -> None:
    result = read("An alternance contract.", title="PFE Data Engineer")

    type_evidence = result.evidence_for(ConstraintKind.OPPORTUNITY_TYPE)
    assert {evidence.source_field for evidence in type_evidence} == {SourceField.TITLE}


def test_nothing_is_asserted_without_evidence() -> None:
    result = read("Minimum 3 years of experience required.")

    assert result.experience
    assert result.evidence_for(ConstraintKind.EXPERIENCE)


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


def test_the_fingerprint_covers_exactly_the_fields_the_extractor_reads() -> None:
    """An input the fingerprint forgets is a change that looks like no change."""
    read_fields = {
        name
        for name in OpportunitySource.__dataclass_fields__
        if name != "opportunity_id"
    }
    assert set(FINGERPRINT_FIELDS) == read_fields


def test_the_fingerprint_is_a_lowercase_sha256() -> None:
    digest = source_fingerprint(OpportunitySource(1, description="A role."))

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_the_fingerprint_is_stable_across_calls() -> None:
    source = OpportunitySource(1, canonical_title="A", description="B", location="C")

    assert source_fingerprint(source) == source_fingerprint(source)


def test_the_fingerprint_ignores_the_row_id() -> None:
    """Two postings with the same words were read from the same text."""
    first = OpportunitySource(1, description="A role.")
    second = OpportunitySource(999, description="A role.")

    assert source_fingerprint(first) == source_fingerprint(second)


def test_a_changed_description_changes_the_fingerprint() -> None:
    before = OpportunitySource(1, description="A role.")
    after = OpportunitySource(1, description="A role. Visa sponsorship available.")

    assert source_fingerprint(before) != source_fingerprint(after)


@pytest.mark.parametrize(
    "field", ("canonical_title", "country", "location", "qualification_type", "remote_type")
)
def test_every_read_field_moves_the_fingerprint(field) -> None:
    before = OpportunitySource(1, description="A role.")
    after = OpportunitySource(1, description="A role.", **{field: "changed"})

    assert source_fingerprint(before) != source_fingerprint(after)


# --------------------------------------------------------------------------
# Nothing here decides anything about anybody
# --------------------------------------------------------------------------


def test_the_reading_carries_no_verdict_and_no_number_to_compare() -> None:
    result = read("Minimum 3 years of experience. Visa sponsorship available.")
    fields = set(type(result).__dataclass_fields__)

    for forbidden in (
        "score", "match_score", "priority_score", "rank", "ranking",
        "confidence", "weight", "recommendation", "profile_id",
    ):
        assert forbidden not in fields


# --------------------------------------------------------------------------
# A contradiction belongs to a slot, not to a kind
# --------------------------------------------------------------------------


def test_two_experience_requirements_are_never_a_contradiction() -> None:
    """`v1` recorded two conflicts here and asserted neither requirement.

    Running those rules over 373 real postings produced 46 conflicts, and this
    shape was most of them. A posting asking for two things is asking for two
    things.
    """
    result = read(
        "Minimum 3 years of engineering experience required.\n"
        "At least 5 years of consulting experience preferred."
    )

    assert result.conflicts == ()
    assert [
        (item.min_months, item.obligation) for item in result.experience
    ] == [
        (36, ExperienceObligation.REQUIRED),
        (60, ExperienceObligation.PREFERRED),
    ]


def test_experience_is_multi_valued_and_holds_no_conflict_slot() -> None:
    assert Slot.EXPERIENCE in MULTI_VALUED_SLOTS
    with pytest.raises(OpportunityConstraintError):
        ConstraintConflict(slot=Slot.EXPERIENCE, values=("A", "B"), rule_ids=("R",))


def test_a_conflict_derives_its_kind_from_its_slot() -> None:
    """The two can never drift apart, because only one of them is passed in."""
    for slot, kind in SLOT_KINDS.items():
        if slot in MULTI_VALUED_SLOTS:
            continue
        conflict = ConstraintConflict(slot=slot, values=("A", "B"), rule_ids=("R",))
        assert conflict.kind is kind


@pytest.mark.parametrize("slot", sorted(MULTI_VALUED_SLOTS, key=lambda s: s.value))
def test_a_multi_valued_slot_cannot_hold_a_conflict(slot) -> None:
    """Several levels or several places are several answers, never a dispute."""
    with pytest.raises(OpportunityConstraintError):
        ConstraintConflict(slot=slot, values=("A", "B"), rule_ids=("R",))


def test_several_locations_are_never_a_conflict() -> None:
    result = read("A role.", location="Ville Exemple", country="Pays Exemple")

    assert result.locations == ("Ville Exemple", "Pays Exemple")
    assert result.conflicts == ()


# --------------------------------------------------------------------------
# A projected location is explainable, like every other value
# --------------------------------------------------------------------------


def test_a_location_alone_is_projected_with_its_evidence() -> None:
    result = read("A role.", location="Ville Exemple")

    assert result.locations == ("Ville Exemple",)
    evidence = result.evidence_for(ConstraintKind.LOCATION)
    assert len(evidence) == 1
    assert evidence[0].source_field is SourceField.LOCATION
    assert evidence[0].rule_id == "LOCATION_COLLECTED_FIELD_V1"
    assert evidence[0].text == "Ville Exemple"
    assert evidence[0].normalized_value == "Ville Exemple"


def test_a_country_alone_is_projected_with_its_evidence() -> None:
    result = read("A role.", country="Pays Exemple")

    assert result.locations == ("Pays Exemple",)
    evidence = result.evidence_for(ConstraintKind.LOCATION)
    assert len(evidence) == 1
    assert evidence[0].source_field is SourceField.COUNTRY
    assert evidence[0].rule_id == "COUNTRY_COLLECTED_FIELD_V1"


def test_a_location_and_a_country_are_two_places_and_two_proofs() -> None:
    result = read("A role.", location="Ville Exemple", country="Pays Exemple")
    evidence = result.evidence_for(ConstraintKind.LOCATION)

    assert result.locations == ("Ville Exemple", "Pays Exemple")
    assert [item.source_field for item in evidence] == [
        SourceField.LOCATION,
        SourceField.COUNTRY,
    ]
    assert [item.normalized_value for item in evidence] == [
        "Ville Exemple",
        "Pays Exemple",
    ]


def test_no_collected_place_means_no_location_and_no_evidence() -> None:
    result = read("A role with no address anywhere in it.")

    assert result.locations == ()
    assert result.evidence_for(ConstraintKind.LOCATION) == ()


def test_a_place_written_twice_is_projected_once_and_explained_once() -> None:
    """Otherwise one row would carry two proofs and look corroborated."""
    result = read("A role.", location="Ville Exemple", country="Ville Exemple")

    assert result.locations == ("Ville Exemple",)
    assert len(result.evidence_for(ConstraintKind.LOCATION)) == 1


def test_a_collected_place_is_trimmed_and_never_expanded() -> None:
    """No geocoding, no country from a city, no city from a country."""
    result = read("A role.", location="  Ville Exemple  ")

    assert result.locations == ("Ville Exemple",)


# --------------------------------------------------------------------------
# What the real corpus taught, case by case
# --------------------------------------------------------------------------
#
# Each of these reproduces a *shape* observed while reading 373 collected
# postings with the v1 rules. The wordings below are written for these tests;
# no real description is copied. Every one of them produced a wrong value or a
# false conflict before the v2 rules existed.


def test_two_independent_experience_requirements_A() -> None:
    result = read(
        "7+ years engineering experience.\n2+ years AI/ML production experience."
    )

    assert [item.min_months for item in result.experience] == [84, 24]
    assert result.conflicts == ()


def test_two_independent_experience_requirements_B() -> None:
    result = read(
        "4+ years strategic experience.\n2+ years consulting experience."
    )

    assert [item.min_months for item in result.experience] == [48, 24]
    assert result.conflicts == ()


def test_a_denied_internship_is_not_an_internship_G() -> None:
    assert read("This isn't a research internship.").opportunity_type is None


def test_a_past_internship_requirement_is_not_the_type_of_the_offer_H() -> None:
    result = read("Prior internship experience required.")

    assert result.opportunity_type is None


def test_mentoring_juniors_does_not_make_the_offer_junior_I() -> None:
    assert read("Mentoring junior consultants and interns.").opportunity_type is None
    assert read(
        "Demonstrated ability to mentor junior team members."
    ).opportunity_type is None


def test_being_enrolled_in_a_graduate_program_is_not_a_first_job_J() -> None:
    """That is the candidate's studies, not a programme the company runs."""
    result = read("You are currently enrolled in an undergraduate or graduate program.")

    assert result.opportunity_type is None


def test_the_classifier_outranks_an_incidental_description_mention_K() -> None:
    result = read(
        "This is an internship opportunity for the team.",
        qualification_type="APPRENTICESHIP",
    )

    assert result.opportunity_type is OpportunityType.ALTERNANCE
    assert result.conflicts == ()


def test_a_title_still_outranks_the_classifier() -> None:
    result = read("A role.", title="PFE Data Engineer", qualification_type="INTERNSHIP")

    assert result.opportunity_type is OpportunityType.PFE


def test_a_tracking_tag_does_not_contradict_a_stated_work_mode_L() -> None:
    """`#LI-Onsite` is appended by an applicant-tracking system, not written
    by the employer describing the job."""
    result = read("This is a fully remote position.\n#LI-Onsite")

    assert result.work_mode is WorkMode.REMOTE
    assert result.conflicts == ()


def test_time_on_site_does_not_contradict_a_hybrid_role_M() -> None:
    """Spending time on site is what hybrid means."""
    result = read(
        "A hybrid customer-facing engineering role.\n"
        "Expect significant time on-site with customers."
    )

    assert result.work_mode is WorkMode.HYBRID
    assert result.conflicts == ()


def test_onsite_qualifying_something_other_than_the_role_is_not_a_work_mode_N() -> None:
    assert read("You will be developing onsite solutions.").work_mode is None
    assert read("You will mentor remote teams across Europe.").work_mode is None


def test_two_global_declarations_that_disagree_are_still_a_conflict_O() -> None:
    """The guard narrows what counts as a declaration; it settles nothing."""
    result = read("This is a fully remote role.\nThis position is fully on-site.")

    assert result.work_mode is None
    assert [conflict.slot for conflict in result.conflicts] == [Slot.WORK_MODE]
    assert result.conflicts[0].values == ("ON_SITE", "REMOTE")


# --------------------------------------------------------------------------
# A PFE is an internship, named more precisely
# --------------------------------------------------------------------------
#
# The last of the 46 conflicts the v1 rules produced over 373 real postings: a
# listing described as a `stage` that also, explicitly, called itself a PFE was
# reported as contradicting itself. It was not. One relation is modelled — the
# four specific internship kinds are `INTERNSHIP` said precisely — and nothing
# beyond it.


@pytest.mark.parametrize(
    "description,expected",
    (
        (
            "Vous serez stagiaire dans notre équipe.\nCe PFE porte sur la data.",
            OpportunityType.PFE,
        ),
        (
            "Offre de stage data.\nIl s'agit d'un PFA encadré.",
            OpportunityType.PFA,
        ),
        (
            "This internship runs in our data team.\n"
            "A summer internship in analytics.",
            OpportunityType.SUMMER_INTERNSHIP,
        ),
        (
            "An internship in our data team.\nThis is a pre-hire internship.",
            OpportunityType.PRE_HIRE_INTERNSHIP,
        ),
    ),
    ids=("PFE", "PFA", "SUMMER_INTERNSHIP", "PRE_HIRE_INTERNSHIP"),
)
def test_a_specific_internship_kind_beats_the_generic_one(description, expected) -> None:
    result = read(description)

    assert result.opportunity_type is expected
    assert result.conflicts == ()


def test_the_evidence_kept_is_the_evidence_for_what_was_asserted() -> None:
    """An auditor never reads a row whose value the projection does not hold."""
    result = read("Vous serez stagiaire.\nCe PFE porte sur la data.")

    assert result.opportunity_type is OpportunityType.PFE
    assert [
        item.normalized_value
        for item in result.evidence_for(ConstraintKind.OPPORTUNITY_TYPE)
    ] == ["PFE"]


@pytest.mark.parametrize(
    "description,expected_values",
    (
        (
            "Ce PFE porte sur la data.\nIl s'agit aussi d'un PFA.",
            ("PFA", "PFE"),
        ),
        (
            "Ce PFE porte sur la data.\nContrat en alternance.",
            ("ALTERNANCE", "PFE"),
        ),
        (
            "A summer internship in analytics.\nThis is a pre-hire internship.",
            ("PRE_HIRE_INTERNSHIP", "SUMMER_INTERNSHIP"),
        ),
    ),
    ids=("PFE_PFA", "PFE_ALTERNANCE", "SUMMER_PRE_HIRE"),
)
def test_two_specific_kinds_that_disagree_are_still_a_conflict(
    description, expected_values
) -> None:
    """The relation is one generic type against one specific one, and no more.

    A posting cannot be both a PFE and a PFA, an alternance is not an
    internship at all, and whether an internship is a summer one or a pre-hire
    one is a real disagreement about what it is for.
    """
    result = read(description)

    assert result.opportunity_type is None
    assert [conflict.values for conflict in result.conflicts] == [expected_values]


def test_the_relation_names_exactly_four_kinds() -> None:
    assert SPECIFIC_INTERNSHIP_TYPES == {
        OpportunityType.PFE,
        OpportunityType.PFA,
        OpportunityType.SUMMER_INTERNSHIP,
        OpportunityType.PRE_HIRE_INTERNSHIP,
    }


def test_the_relation_refuses_anything_that_is_not_one_generic_and_one_specific() -> None:
    assert more_specific_internship(
        {OpportunityType.INTERNSHIP, OpportunityType.PFE}
    ) is OpportunityType.PFE
    # Two specific kinds, no generic one, and a non-internship all get None.
    assert more_specific_internship(
        {OpportunityType.INTERNSHIP, OpportunityType.PFE, OpportunityType.PFA}
    ) is None
    assert more_specific_internship({OpportunityType.PFE, OpportunityType.PFA}) is None
    assert more_specific_internship(
        {OpportunityType.INTERNSHIP, OpportunityType.ALTERNANCE}
    ) is None
    assert more_specific_internship(
        {OpportunityType.INTERNSHIP, OpportunityType.JUNIOR_ROLE}
    ) is None


def test_the_precedence_between_fields_is_unchanged() -> None:
    """The relation applies inside one tier; it does not reorder the tiers."""
    assert TYPE_FIELD_PRECEDENCE == (
        SourceField.TITLE,
        SourceField.QUALIFICATION_TYPE,
        SourceField.DESCRIPTION,
    )
    # A title still settles it against anything the description says.
    assert read(
        "This is an alternance contract.", title="PFE Data Engineer"
    ).opportunity_type is OpportunityType.PFE
