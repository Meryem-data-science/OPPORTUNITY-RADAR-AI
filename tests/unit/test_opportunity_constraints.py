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

from services.collector.extractors.opportunity_constraints.extractor import (
    FINGERPRINT_FIELDS,
    extract_opportunity_constraints,
    source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
    MAX_EVIDENCE_LENGTH,
    ConstraintKind,
    ConventionRequirement,
    EducationLevel,
    EducationRequirementMode,
    ExperienceObligation,
    OpportunitySource,
    OpportunityType,
    SourceField,
    StartPrecision,
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
    assert EXTRACTOR_VERSION == "opportunity-constraints-v1"


# --------------------------------------------------------------------------
# Reading the text the collectors actually store
# --------------------------------------------------------------------------


def test_an_escaped_html_description_is_read_as_text() -> None:
    """Greenhouse stores entity-escaped HTML, and nothing upstream unescapes it."""
    escaped = "&lt;ul&gt;&lt;li&gt;Minimum 3 years of experience&lt;/li&gt;&lt;/ul&gt;"

    assert normalize_description(escaped) == "Minimum 3 years of experience"
    assert read(escaped).experience.min_months == 36


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
    assert not result.experience.known
    assert result.experience.obligation is ExperienceObligation.UNKNOWN
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

    assert not result.experience.known
    assert result.experience.min_months is None


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

    assert result.experience.min_months == minimum
    assert result.experience.max_months == maximum


def test_a_number_with_no_experience_word_is_not_an_experience_requirement() -> None:
    result = read("We have 3 offices and 5 teams.")

    assert not result.experience.known


def test_experience_preferred_is_an_obligation_without_a_quantity() -> None:
    result = read("Prior experience with data pipelines is preferred.")

    assert result.experience.obligation is ExperienceObligation.PREFERRED
    assert not result.experience.known


def test_the_obligation_belongs_to_the_sentence_that_quantified() -> None:
    """"3 years required" beside "Spark preferred" is two requirements.

    Reading them as one posting contradicting itself would lose both.
    """
    result = read("Minimum 3 years of experience.\nExperience with Spark is preferred.")

    assert result.experience.min_months == 36
    assert result.experience.obligation is ExperienceObligation.REQUIRED
    assert result.conflicts == ()


def test_an_obligation_alone_is_used_when_nothing_was_quantified() -> None:
    result = read("Experience with data pipelines is preferred.")

    assert not result.experience.known
    assert result.experience.obligation is ExperienceObligation.PREFERRED


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
    assert result.experience.min_months == 36
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

    assert result.experience.min_months == 36
    for evidence in result.evidence:
        assert len(evidence.text) <= MAX_EVIDENCE_LENGTH


def test_evidence_names_the_field_it_was_read_from() -> None:
    result = read("An alternance contract.", title="PFE Data Engineer")

    type_evidence = result.evidence_for(ConstraintKind.OPPORTUNITY_TYPE)
    assert {evidence.source_field for evidence in type_evidence} == {SourceField.TITLE}


def test_nothing_is_asserted_without_evidence() -> None:
    result = read("Minimum 3 years of experience required.")

    assert result.experience.known
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
