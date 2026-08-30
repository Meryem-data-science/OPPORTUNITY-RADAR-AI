"""Unit coverage for the Phase 3.5B skill and language reading.

Every posting below is invented for the test that uses it. No real listing,
company, description or person takes part, no database is opened, and the
operational `.data/` database is never touched: the whole module exercises pure
functions.

Most of these tests are about what the extractor must **not** produce. A
requirement nobody wrote is the failure this slice exists to prevent, so the
"our stack includes…" case, the "you will build pipelines using…" case, the
"no prior experience required" case and the "Python or R" case each get their
own test, and each asserts an absence.
"""

from dataclasses import fields

import unicodedata

import pytest

from services.collector.extractors.opportunity_constraints.models import (
    MAX_EVIDENCE_LENGTH,
)
from services.collector.extractors.opportunity_constraints.requirements.extractor import (
    REQUIREMENT_FINGERPRINT_FIELDS,
    extract_opportunity_requirements,
    requirement_source_fingerprint,
)
from services.collector.extractors.opportunity_constraints.requirements.language_catalog import (
    LANGUAGE_CATALOG,
)
from services.collector.extractors.opportunity_constraints.requirements.languages import (
    read_language_requirements,
)
from services.collector.extractors.opportunity_constraints.requirements.matcher import (
    LANGUAGE_MATCHER,
    SKILL_MATCHER,
    mentions_catalogue_term,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    REQUIREMENT_EXTRACTOR_VERSION,
    AmbiguityReason,
    ExtractedRequirements,
    LanguageRequirement,
    RequirementError,
    RequirementEvidence,
    RequirementKind,
    RequirementLevel,
    RequirementSource,
    RequirementSourceField,
    SectionContext,
    SkillRequirement,
    stronger,
)
from services.collector.extractors.opportunity_constraints.requirements.sections import (
    HEADINGS,
    heading_context,
    looks_like_heading,
    parse_requirement_segments,
)
from services.collector.extractors.opportunity_constraints.requirements.signals import (
    Link,
    cancels_requirement,
    clause_level,
    link_between,
    term_clauses,
    term_runs,
)
from services.collector.extractors.opportunity_constraints.requirements.skill_catalog import (
    COMPOUND_SKILL_EXPRESSIONS,
    SKILL_CATALOG,
    SkillCatalogError,
    is_compound_expression,
)
from services.collector.extractors.opportunity_constraints.requirements.skills import (
    read_skill_requirements,
)
from services.digital_twin.skills.normalizer import technical_key


def read(description: str):
    """The skills, languages and refusals of one invented description."""
    reading = extract_opportunity_requirements(
        RequirementSource(opportunity_id=1, description=description)
    )
    return reading


def skills_of(description: str) -> dict[str, str]:
    return {
        item.canonical_name: item.requirement.value for item in read(description).skills
    }


def languages_of(description: str) -> dict[str, tuple[str, str | None]]:
    return {
        item.language_name: (item.requirement.value, item.proficiency_text)
        for item in read(description).languages
    }


def reasons_of(description: str) -> list[str]:
    return [item.reason.value for item in read(description).ambiguities]


# --------------------------------------------------------------------------
# The version and the contract
# --------------------------------------------------------------------------


def test_the_requirement_version_is_its_own_contract() -> None:
    """3.5B moves when 3.5B's rules move, and not when 3.5A's do."""
    from services.collector.extractors.opportunity_constraints.models import (
        EXTRACTOR_VERSION,
    )

    assert REQUIREMENT_EXTRACTOR_VERSION == "opportunity-requirements-v3"
    assert REQUIREMENT_EXTRACTOR_VERSION != EXTRACTOR_VERSION


def test_the_fingerprint_covers_exactly_what_the_source_holds() -> None:
    """A field the extractor reads and the digest ignores breaks idempotence."""
    declared = tuple(
        sorted(
            field.name
            for field in fields(RequirementSource)
            if field.name != "opportunity_id"
        )
    )
    assert declared == tuple(sorted(REQUIREMENT_FINGERPRINT_FIELDS))


def test_the_fingerprint_ignores_the_posting_s_identity() -> None:
    first = RequirementSource(opportunity_id=1, description="Python required.")
    second = RequirementSource(opportunity_id=999, description="Python required.")

    assert requirement_source_fingerprint(first) == requirement_source_fingerprint(second)


def test_a_changed_description_changes_the_fingerprint() -> None:
    before = RequirementSource(opportunity_id=1, description="Python required.")
    after = RequirementSource(opportunity_id=1, description="SQL required.")

    assert requirement_source_fingerprint(before) != requirement_source_fingerprint(after)


def test_the_extraction_is_deterministic() -> None:
    source = RequirementSource(
        opportunity_id=1, description="Requirements\n- Python\n- Fluent English"
    )

    assert extract_opportunity_requirements(source) == extract_opportunity_requirements(
        source
    )


def test_the_extractor_refuses_anything_but_its_own_source() -> None:
    with pytest.raises(TypeError):
        extract_opportunity_requirements({"description": "Python required."})


def test_an_absent_description_reads_as_nothing_required() -> None:
    reading = extract_opportunity_requirements(
        RequirementSource(opportunity_id=1, description=None)
    )

    assert (reading.skills, reading.languages, reading.ambiguities) == ((), (), ())
    assert reading.extractor_version == REQUIREMENT_EXTRACTOR_VERSION


# --------------------------------------------------------------------------
# The catalogues
# --------------------------------------------------------------------------


def test_the_skill_catalogue_uses_the_shared_normalizer_s_keys() -> None:
    """One vocabulary, so the offer side and the profile side can ever meet."""
    for term in SKILL_CATALOG:
        assert term.canonical_key == technical_key(term.canonical_name)


def test_the_skill_catalogue_covers_the_roles_this_radar_collects() -> None:
    names = {term.canonical_name for term in SKILL_CATALOG}
    for expected in (
        "Python", "SQL", "R", "Machine Learning", "Deep Learning", "PyTorch",
        "TensorFlow", "Apache Spark", "Apache Kafka", "Apache Airflow",
        "PostgreSQL", "MongoDB", "AWS", "Azure", "Google Cloud", "Docker",
        "Kubernetes", "MLflow", "Power BI", "Tableau", "Pandas", "NumPy",
        "FastAPI", "Large Language Models", "Generative AI",
        "Natural Language Processing", "Computer Vision", "dbt", "Snowflake",
    ):
        assert expected in names


def test_no_catalogue_entry_is_a_job_title_or_a_soft_skill() -> None:
    """A role is not a technology, and neither is a personality."""
    names = {term.canonical_name.casefold() for term in SKILL_CATALOG}
    for refused in (
        "data scientist", "data engineer", "machine learning engineer",
        "team player", "communication", "leadership", "problem solving",
        "autonomy", "rigueur", "curiosity",
    ):
        assert refused not in names


def test_the_language_registry_keeps_its_keys_stable_and_ascii() -> None:
    for term in LANGUAGE_CATALOG:
        assert term.language_key == term.language_key.casefold()
        assert term.language_key.isascii()


def test_the_language_registry_covers_the_languages_the_corpus_writes() -> None:
    names = {term.language_name for term in LANGUAGE_CATALOG}
    assert {"English", "French", "Arabic", "Spanish", "German"} <= names


# --------------------------------------------------------------------------
# The matcher: boundaries and overlap
# --------------------------------------------------------------------------


def keys_in(text: str) -> list[str]:
    return [match.key for match in SKILL_MATCHER.find(text)]


def test_postgresql_does_not_produce_sql() -> None:
    """The classic overlap. A posting requiring Postgres never required SQL."""
    assert keys_in("PostgreSQL required.") == ["postgresql"]


def test_pyspark_does_not_produce_spark() -> None:
    assert keys_in("PySpark required.") == ["pyspark"]


def test_spark_is_still_read_when_the_posting_writes_it_separately() -> None:
    assert keys_in("PySpark and Spark required.") == ["pyspark", "apache spark"]


def test_the_three_c_languages_stay_distinct() -> None:
    assert keys_in("C, C++ and C# required.") == ["c", "c++", "c#"]


def test_r_does_not_match_a_letter_inside_a_word() -> None:
    assert keys_in("Rust and React are wonderful, our R&D team says.") == ["rust"]


def test_go_does_not_match_google() -> None:
    assert keys_in("Google Cloud is our home.") == ["google cloud"]


def test_a_short_alias_must_be_written_as_the_catalogue_writes_it() -> None:
    """`go` is a verb, `c` is a letter, `r` is a consonant."""
    assert keys_in("we go fast and we c things clearly") == []
    assert keys_in("Go required.") == ["go"]


def test_the_longest_alias_wins_at_one_position() -> None:
    assert keys_in("Apache Spark required.") == ["apache spark"]
    assert keys_in("Google Cloud Platform required.") == ["google cloud"]


def test_two_spellings_of_one_technology_are_one_skill() -> None:
    assert keys_in("scikit-learn required.") == keys_in("sklearn required.")


def test_matches_never_overlap() -> None:
    text = "Apache Spark, PySpark, PostgreSQL and CI/CD."
    matches = SKILL_MATCHER.find(text)
    for earlier, later in zip(matches, matches[1:], strict=False):
        assert earlier.end <= later.start


def test_the_matcher_knows_a_line_names_a_catalogue_term() -> None:
    assert mentions_catalogue_term("Python")
    assert mentions_catalogue_term("Fluent English")
    assert not mentions_catalogue_term("Additional Information")


def test_the_language_matcher_reads_accented_and_plain_spellings() -> None:
    assert [item.key for item in LANGUAGE_MATCHER.find("français")] == ["french"]
    assert [item.key for item in LANGUAGE_MATCHER.find("francais")] == ["french"]


# --------------------------------------------------------------------------
# The context parser
# --------------------------------------------------------------------------


def contexts_of(description: str) -> list[tuple[str, str]]:
    return [
        (segment.text, segment.context.value)
        for segment in parse_requirement_segments(description)
    ]


def test_a_required_heading_opens_a_required_section() -> None:
    assert contexts_of("Required Qualifications\nPython\nSQL") == [
        ("Python", "REQUIRED"),
        ("SQL", "REQUIRED"),
    ]


def test_a_preferred_heading_opens_a_preferred_section() -> None:
    assert contexts_of("Nice to Have\nSpark") == [("Spark", "PREFERRED")]


def test_a_neutral_heading_ends_the_section_before_it() -> None:
    parsed = contexts_of(
        "Required Qualifications\nPython\nResponsibilities\nBuild pipelines with Spark"
    )

    assert parsed == [
        ("Python", "REQUIRED"),
        ("Build pipelines with Spark", "NEUTRAL"),
    ]


def test_an_unknown_heading_does_not_leak_the_previous_context() -> None:
    """The leak this guard exists for: a list of technologies under a title
    nobody recognised must not inherit "Required Qualifications"."""
    parsed = contexts_of(
        "Required Qualifications\nPython\nAdditional Information\nKafka"
    )

    assert parsed == [("Python", "REQUIRED"), ("Kafka", "NEUTRAL")]


def test_a_one_word_item_naming_a_technology_is_not_a_heading() -> None:
    """`Python` is short, capitalised and unpunctuated — and it is an item."""
    assert not looks_like_heading("Python")
    assert not looks_like_heading("Fluent English")


def test_a_sentence_is_never_a_heading_however_short() -> None:
    assert not looks_like_heading("We are hiring.")
    assert not looks_like_heading("- Airflow")


def test_a_title_shaped_line_is_a_heading() -> None:
    assert looks_like_heading("Additional Information")
    assert looks_like_heading("BENEFITS")
    assert looks_like_heading("About the team:")


def test_a_long_line_is_never_a_heading() -> None:
    assert not looks_like_heading(
        "Everything we would love the person joining this team to already know"
    )


def test_a_known_heading_must_be_the_whole_line() -> None:
    """`Requirements: 3 years` is a sentence, not a section title."""
    assert heading_context("Requirements") is SectionContext.REQUIRED
    assert heading_context("Requirements:") is SectionContext.REQUIRED
    assert heading_context("Requirements: three years of Python") is None


def test_a_local_marker_outranks_the_section_it_sits_in() -> None:
    assert skills_of("Required Qualifications\n- Python preferred") == {
        "Python": "PREFERRED"
    }


def test_the_stack_heading_is_neutral() -> None:
    assert heading_context("Our stack") is SectionContext.NEUTRAL
    assert heading_context("Tech Stack") is SectionContext.NEUTRAL


def test_no_heading_is_claimed_by_two_contexts() -> None:
    assert len(HEADINGS) == len(set(HEADINGS))


def test_a_segment_keeps_the_heading_it_sat_under_verbatim() -> None:
    parsed = parse_requirement_segments("Required Qualifications\nPython")

    assert parsed[0].heading_text == "Required Qualifications"
    # The heading is stored beside the fragment, never welded onto it.
    assert "Required Qualifications" not in parsed[0].text


# --------------------------------------------------------------------------
# Signals: cancelling, local markers, connectors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    (
        "No prior Python experience is required.",
        "No Python experience required.",
        "SQL is not required.",
        "Training in Python will be provided.",
        "We will train you on Spark.",
        "Aucune expérience en Python n'est requise.",
    ),
)
def test_a_cancelling_sentence_states_no_demand(sentence: str) -> None:
    assert cancels_requirement(sentence)
    assert clause_level(sentence, SectionContext.REQUIRED) is None


def test_a_cancelling_sentence_beats_its_section() -> None:
    assert skills_of("Required Qualifications\n- No Python experience required") == {}


@pytest.mark.parametrize(
    "gap, expected",
    (
        (" and ", Link.AND),
        (" et ", Link.AND),
        (" or ", Link.OR),
        (" ou ", Link.OR),
        ("/", Link.SLASH),
        (", or ", Link.OR),
        (",", Link.LIST),
        (" which we use daily, and ", Link.NONE),
    ),
)
def test_the_connector_between_two_terms_is_classified(gap: str, expected) -> None:
    assert link_between(gap) is expected


def test_a_run_of_one_term_is_not_a_choice() -> None:
    runs = term_runs([(0, 6)], "Python required.")

    assert runs == (type(runs[0])(indexes=(0,), alternative=False),)


def test_required_beats_preferred_in_one_place() -> None:
    assert stronger(RequirementLevel.PREFERRED, RequirementLevel.REQUIRED) is (
        RequirementLevel.REQUIRED
    )
    assert stronger(RequirementLevel.PREFERRED, RequirementLevel.PREFERRED) is (
        RequirementLevel.PREFERRED
    )


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------


def test_a_required_section_lists_requirements() -> None:
    assert skills_of("Required Qualifications\n- Python\n- SQL") == {
        "Python": "REQUIRED",
        "SQL": "REQUIRED",
    }


def test_a_nice_to_have_section_lists_preferences() -> None:
    assert skills_of("Nice to Have\n- Spark\n- Airflow") == {
        "Apache Spark": "PREFERRED",
        "Apache Airflow": "PREFERRED",
    }


def test_a_stack_listing_requires_nothing() -> None:
    assert skills_of("Our stack includes Python and Spark.") == {}


def test_a_responsibility_requires_nothing() -> None:
    assert skills_of("You will build pipelines using Python.") == {}


def test_a_habit_requires_nothing() -> None:
    assert skills_of("We use SQL across the company.") == {}


def test_a_promise_to_train_requires_nothing() -> None:
    assert skills_of("Training in Python will be provided.") == {}


def test_a_cancelled_demand_requires_nothing() -> None:
    assert skills_of("No Python experience required.") == {}


def test_must_have_states_requirements_outside_any_section() -> None:
    assert skills_of("Must have experience with Python and SQL.") == {
        "Python": "REQUIRED",
        "SQL": "REQUIRED",
    }


@pytest.mark.parametrize(
    "sentence",
    (
        "Python is required.",
        "Strong Python skills required.",
        "Proficiency in Python required.",
        "Python is mandatory.",
    ),
)
def test_a_local_required_marker_states_a_requirement(sentence: str) -> None:
    assert skills_of(sentence) == {"Python": "REQUIRED"}


@pytest.mark.parametrize(
    "sentence",
    (
        "Python preferred.",
        "Experience with Python is a plus.",
        "Python would be appreciated.",
        "Python est un atout.",
    ),
)
def test_a_local_preferred_marker_states_a_preference(sentence: str) -> None:
    assert skills_of(sentence) == {"Python": "PREFERRED"}


def test_one_skill_required_and_preferred_projects_required() -> None:
    reading = read("Nice to Have\n- Python\nRequirements\n- Python")

    assert [item.requirement for item in reading.skills] == [RequirementLevel.REQUIRED]
    observed = [item.observed_requirement.value for item in reading.skills[0].evidence]
    assert observed == ["PREFERRED", "REQUIRED"]


def test_an_alternative_is_never_stored_as_two_obligations() -> None:
    """The failure this refusal prevents: Phase 3.6 demanding both."""
    reading = read("Python or R required.")

    assert reading.skills == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED
    ]
    assert reading.ambiguities[0].kind is RequirementKind.SKILL


def test_a_three_way_alternative_invents_no_obligation() -> None:
    reading = read("Experience with one of Python, R or Julia is required.")

    assert reading.skills == ()
    assert reasons_of("Experience with one of Python, R or Julia is required.") == [
        "ALTERNATIVE_GROUP_UNSUPPORTED"
    ]


def test_an_alternative_whose_other_side_is_unknown_is_still_a_choice() -> None:
    assert skills_of("Python or Julia required.") == {}


def test_an_explicit_conjunction_is_two_requirements() -> None:
    assert skills_of("Python and R required.") == {"Python": "REQUIRED", "R": "REQUIRED"}


def test_a_comma_list_with_a_conjunction_is_several_requirements() -> None:
    assert skills_of("Python, SQL and Spark are required.") == {
        "Python": "REQUIRED",
        "SQL": "REQUIRED",
        "Apache Spark": "REQUIRED",
    }


def test_an_alternative_in_a_neutral_section_records_no_refusal() -> None:
    """Nothing was refused: the sentence stated no demand to begin with."""
    assert reasons_of("Our stack includes Python or R.") == []


def test_every_skill_requirement_quotes_the_words_that_stated_it() -> None:
    reading = read("Required Qualifications\n- Strong Python skills")
    evidence = reading.skills[0].evidence[0]

    assert evidence.text == "Strong Python skills"
    assert evidence.context_heading_text == "Required Qualifications"
    assert evidence.source_field is RequirementSourceField.DESCRIPTION
    assert evidence.rule_id


def test_evidence_stays_a_fragment_and_never_a_copy() -> None:
    long_sentence = "Requirements\n- " + ("Python and SQL experience " * 40)
    reading = read(long_sentence)

    for requirement in reading.skills:
        for evidence in requirement.evidence:
            assert len(evidence.text) <= MAX_EVIDENCE_LENGTH


def test_a_catalogue_term_nobody_wrote_produces_no_requirement() -> None:
    reading = read("Requirements\n- Python")

    assert {item.canonical_name for item in reading.skills} == {"Python"}


def test_html_entities_are_read_through_the_shared_normalizer() -> None:
    """One cleaner, shared with 3.5A: two would give two readings of one posting."""
    assert skills_of(
        "&lt;ul&gt;&lt;li&gt;Python is required&lt;/li&gt;&lt;/ul&gt;"
    ) == {"Python": "REQUIRED"}


# --------------------------------------------------------------------------
# Languages
# --------------------------------------------------------------------------


def test_a_required_language_is_read() -> None:
    assert languages_of("English required.") == {"English": ("REQUIRED", None)}


def test_a_language_in_a_required_section_keeps_its_written_level() -> None:
    assert languages_of("Required Qualifications\n- Fluent English") == {
        "English": ("REQUIRED", "Fluent")
    }


def test_a_cefr_code_is_stored_as_the_posting_wrote_it() -> None:
    assert languages_of("French B2 required.") == {"French": ("REQUIRED", "B2")}


def test_a_preferred_language_is_read_as_preferred() -> None:
    assert languages_of("French is a plus.") == {"French": ("PREFERRED", None)}


def test_a_posting_written_in_english_has_not_required_english() -> None:
    assert languages_of("We build good products with a great team.") == {}


def test_an_attributive_mention_is_not_a_demand() -> None:
    assert languages_of("You will work with English-speaking customers.") == {}
    assert languages_of("Requirements\n- Own the French market") == {}
    assert languages_of("Requirements\n- Maintain English documentation") == {}


def test_a_place_never_implies_a_language() -> None:
    """There is no location field in `RequirementSource` at all."""
    assert languages_of("Requirements\n- Based in Paris, France") == {}


def test_bilingual_names_both_languages() -> None:
    assert languages_of("Bilingual English/French required.") == {
        "English": ("REQUIRED", None),
        "French": ("REQUIRED", None),
    }


def test_a_conjunction_of_languages_names_both() -> None:
    assert languages_of("English and French required.") == {
        "English": ("REQUIRED", None),
        "French": ("REQUIRED", None),
    }


def test_an_alternative_of_languages_names_neither() -> None:
    reading = read("English or French required.")

    assert reading.languages == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED
    ]
    assert reading.ambiguities[0].kind is RequirementKind.LANGUAGE


def test_an_explicit_or_beats_the_bilingual_marker() -> None:
    assert languages_of("Bilingual English or French required.") == {}


def test_one_language_required_and_preferred_projects_required() -> None:
    reading = read("English required. English is a plus.")

    assert reading.languages[0].requirement is RequirementLevel.REQUIRED
    assert [
        item.observed_requirement.value for item in reading.languages[0].evidence
    ] == ["REQUIRED", "PREFERRED"]


def test_the_required_level_is_the_one_kept_when_a_preference_names_another() -> None:
    assert languages_of("English B2 required. English C1 preferred.") == {
        "English": ("REQUIRED", "B2")
    }


def test_two_incompatible_required_levels_leave_the_level_unknown() -> None:
    reading = read("English B2 required. English C1 required.")

    assert reading.languages[0].requirement is RequirementLevel.REQUIRED
    assert reading.languages[0].proficiency_text is None
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.CONFLICTING_LANGUAGE_PROFICIENCY
    ]


def test_the_conflicting_mentions_both_survive_as_evidence() -> None:
    reading = read("English B2 required. English C1 required.")

    assert [
        item.observed_proficiency_text for item in reading.languages[0].evidence
    ] == ["B2", "C1"]


@pytest.mark.parametrize(
    "sentence, level",
    (
        ("Fluent English required.", "Fluent"),
        ("Native English required.", "Native"),
        ("Professional proficiency in English required.", "Professional proficiency"),
        ("English C1 required.", "C1"),
    ),
)
def test_a_level_is_stored_as_written_and_never_translated(
    sentence: str, level: str
) -> None:
    assert languages_of(sentence) == {"English": ("REQUIRED", level)}


def test_fluent_never_becomes_a_cefr_code() -> None:
    stored = languages_of("Fluent English required.")["English"][1]

    assert stored == "Fluent"
    assert stored not in {"C1", "C2", "B2"}


def test_native_never_becomes_a_cefr_code() -> None:
    assert languages_of("Native English required.")["English"][1] == "Native"


def test_a_language_requirement_quotes_the_words_that_stated_it() -> None:
    reading = read("Required Qualifications\n- Fluent English")
    evidence = reading.languages[0].evidence[0]

    assert evidence.text == "Fluent English"
    assert evidence.context_heading_text == "Required Qualifications"
    assert evidence.observed_proficiency_text == "Fluent"


# --------------------------------------------------------------------------
# Ambiguities and the shape of the reading
# --------------------------------------------------------------------------


def test_ambiguities_are_numbered_once_across_skills_and_languages() -> None:
    reading = read("Python or R required. English or French required.")

    assert [item.position for item in reading.ambiguities] == [0, 1]
    assert [item.kind for item in reading.ambiguities] == [
        RequirementKind.SKILL,
        RequirementKind.LANGUAGE,
    ]


def test_an_ambiguity_quotes_a_fragment_and_names_its_rule() -> None:
    ambiguity = read("Requirements\n- Python or R").ambiguities[0]

    assert ambiguity.text == "Python or R"
    assert ambiguity.context_heading_text == "Requirements"
    assert ambiguity.rule_id == "SKILL_ALTERNATIVE_GROUP_V2"


def test_a_silent_posting_records_nothing_at_all() -> None:
    reading = read("We build good products with a great team in our office.")

    assert (reading.skills, reading.languages, reading.ambiguities) == ((), (), ())


# --------------------------------------------------------------------------
# Unicode token boundaries
#
# The `v1` matcher bounded terms with `[A-Za-z0-9_+#&]`, which leaves every
# accented letter *outside* the class. `é` therefore read as a word boundary
# and the one-letter alias `R` matched the start of `Réseaux`. The corpus is
# partly French, `R` is a real technology, and a false `R` under a requirements
# heading is a hard requirement nobody wrote.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    (
        "Réseaux",
        "Régression",
        "Réalisation",
        "Rédaction",
        "Rétro-ingénierie",
        "Résultats",
    ),
)
def test_a_french_word_never_produces_the_r_skill(word: str) -> None:
    assert keys_in(word) == []
    assert keys_in(f"Requirements: {word} de neurones") == []


@pytest.mark.parametrize("word", ("Câblage", "Cœur", "Contrôle", "Ça"))
def test_a_french_word_never_produces_the_c_skill(word: str) -> None:
    assert keys_in(word) == []


@pytest.mark.parametrize(
    "word", ("Réseaux", "Régression", "Réalisation", "Câblage", "Ça")
)
def test_a_decomposed_spelling_is_bounded_too(word: str) -> None:
    """NFC is not the only form a posting can arrive in.

    `Ça` may be one character or `C` followed by a combining cedilla, and a
    boundary that only knew about letters would read the second as a standalone
    `C`. Both forms are tested because a collector normalizes nothing.
    """
    assert keys_in(unicodedata.normalize("NFD", word)) == []
    assert keys_in(unicodedata.normalize("NFC", word)) == []


def test_a_unicode_letter_never_hides_a_real_requirement() -> None:
    """The guard bounds terms; it does not swallow the ones a posting wrote."""
    assert keys_in("Réseaux de neurones, R et Python") == ["r", "python"]


@pytest.mark.parametrize(
    "text, expected",
    (
        ("R", ["r"]),
        ("C", ["c"]),
        ("R required.", ["r"]),
        ("C required.", ["c"]),
        ("C++", ["c++"]),
        ("C#", ["c#"]),
        ("R&D", []),
        ("Google", []),
        ("PostgreSQL", ["postgresql"]),
        ("PySpark", ["pyspark"]),
        ("C, C++ and C#", ["c", "c++", "c#"]),
    ),
)
def test_the_historical_boundary_protections_survive(text: str, expected) -> None:
    assert keys_in(text) == expected


# --------------------------------------------------------------------------
# One sentence, two demands of different strength
#
# `v1` scored the markers over the whole sentence, so two technologies in one
# sentence were forced to share one level — and one clause's cancelling words
# deleted another clause's demand entirely.
# --------------------------------------------------------------------------


def test_two_skills_in_one_sentence_keep_their_own_levels() -> None:
    assert skills_of("Python required and Spark preferred.") == {
        "Python": "REQUIRED",
        "Apache Spark": "PREFERRED",
    }


def test_the_order_of_the_two_demands_does_not_change_them() -> None:
    assert skills_of("Python preferred and SQL required.") == {
        "Python": "PREFERRED",
        "SQL": "REQUIRED",
    }


def test_a_cancelled_skill_does_not_cancel_its_neighbour() -> None:
    """The clause about Python says nothing about SQL."""
    assert skills_of("No Python experience required, but SQL is required.") == {
        "SQL": "REQUIRED"
    }


def test_a_local_marker_does_not_reach_the_next_item_of_a_required_section() -> None:
    assert skills_of("Required Qualifications\n- Python preferred and SQL") == {
        "Python": "PREFERRED",
        "SQL": "REQUIRED",
    }


def test_a_local_marker_does_not_reach_the_next_item_of_a_preferred_section() -> None:
    assert skills_of("Preferred Qualifications\n- Python required\n- Spark") == {
        "Python": "REQUIRED",
        "Apache Spark": "PREFERRED",
    }


def test_two_languages_in_one_sentence_keep_their_own_levels() -> None:
    assert languages_of("English required and French preferred.") == {
        "English": ("REQUIRED", None),
        "French": ("PREFERRED", None),
    }


def test_a_cancelled_language_does_not_cancel_its_neighbour() -> None:
    assert languages_of("English not required but French required.") == {
        "French": ("REQUIRED", None)
    }


def test_a_conjunction_still_shares_one_level_across_both_terms() -> None:
    """The marker sits at the end of a clause both terms belong to."""
    assert skills_of("Python and SQL required.") == {
        "Python": "REQUIRED",
        "SQL": "REQUIRED",
    }
    assert skills_of("Python and R preferred.") == {
        "Python": "PREFERRED",
        "R": "PREFERRED",
    }
    assert languages_of("English and French required.") == {
        "English": ("REQUIRED", None),
        "French": ("REQUIRED", None),
    }


def test_clause_splitting_does_not_break_an_alternative_group() -> None:
    assert skills_of("Python or R required.") == {}
    assert reasons_of("Python or R required.") == ["ALTERNATIVE_GROUP_UNSUPPORTED"]
    assert languages_of("English or French required.") == {}
    assert reasons_of("English or French required.") == [
        "ALTERNATIVE_GROUP_UNSUPPORTED"
    ]


def test_a_sentence_with_one_demand_is_scored_over_the_whole_sentence() -> None:
    """One run, one window: the clause split changes nothing here."""
    text = "Must have experience with Python and SQL."
    matches = SKILL_MATCHER.find(text)
    spans = [(item.start, item.end) for item in matches]
    runs = term_runs(spans, text)

    assert term_clauses(spans, text, runs) == ((0, len(text)),) * len(runs)


def test_a_clause_window_never_loses_the_edges_of_the_sentence() -> None:
    text = "Python required and Spark preferred."
    matches = SKILL_MATCHER.find(text)
    spans = [(item.start, item.end) for item in matches]
    windows = term_clauses(spans, text, term_runs(spans, text))

    assert windows[0][0] == 0
    assert windows[-1][1] == len(text)
    assert "preferred" not in text[windows[0][0] : windows[0][1]]
    assert "required" not in text[windows[-1][0] : windows[-1][1]]


def test_each_mention_still_records_the_level_it_stated() -> None:
    reading = read("Python required and Spark preferred.")
    levels = {
        item.canonical_name: item.evidence[0].observed_requirement.value
        for item in reading.skills
    }

    assert levels == {"Python": "REQUIRED", "Apache Spark": "PREFERRED"}


def test_the_local_rules_say_they_are_clause_local() -> None:
    """A `_V1` id would describe a rule that could read a neighbour's marker."""
    from services.collector.extractors.opportunity_constraints.requirements import (
        signals,
    )

    assert signals.LOCAL_REQUIRED_RULE_ID.endswith("_V2")
    assert signals.LOCAL_PREFERRED_RULE_ID.endswith("_V2")
    assert signals.CANCELLING_RULE_ID.endswith("_V2")
    # A heading meant the same thing before and means it now.
    assert signals.SECTION_REQUIRED_RULE_ID.endswith("_V1")
    assert signals.SECTION_PREFERRED_RULE_ID.endswith("_V1")


# --------------------------------------------------------------------------
# A slash is not an `or`
#
# `v2` classified every bare `/` as an alternative. Over 373 real postings that
# refused `AI/ML engineering`, `AI/ML APIs` and `ML/LLM-powered system` as
# choices the employer never offered. `v3` separates the two links, keeps a
# closed registry of the compounds, and still stores neither half of either.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gap, expected",
    (
        (" or ", Link.OR),
        (" ou ", Link.OR),
        (" and/or ", Link.OR),
        (" et/ou ", Link.OR),
        ("|", Link.OR),
        ("/", Link.SLASH),
        (" / ", Link.SLASH),
        (" and ", Link.AND),
        (",", Link.LIST),
    ),
)
def test_a_bare_slash_is_its_own_connector(gap: str, expected) -> None:
    assert link_between(gap) is expected


@pytest.mark.parametrize(
    "sentence", ("AI/ML required.", "ML/AI required.", "ML/LLM required.",
                 "LLM/ML required.")
)
def test_a_registered_compound_stores_neither_half(sentence: str) -> None:
    reading = read(sentence)

    assert reading.skills == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.COMPOUND_SKILL_EXPRESSION_UNSUPPORTED
    ]
    assert reading.ambiguities[0].rule_id == "SKILL_COMPOUND_EXPRESSION_UNSUPPORTED_V1"


@pytest.mark.parametrize(
    "sentence",
    (
        "Strong AI/ML engineering experience is required.",
        "Experience building ML/LLM-powered systems is required.",
        "AI/ML APIs required.",
    ),
)
def test_a_compound_is_recognised_inside_a_longer_phrase(sentence: str) -> None:
    reading = read(sentence)

    assert reading.skills == ()
    assert [item.reason.value for item in reading.ambiguities] == [
        "COMPOUND_SKILL_EXPRESSION_UNSUPPORTED"
    ]


@pytest.mark.parametrize(
    "sentence",
    (
        "Python/R required.",
        "JavaScript/TypeScript required.",
        "C/C++ required.",
        "TensorFlow/PyTorch required.",
    ),
)
def test_an_unregistered_slash_stays_the_conservative_reading(sentence: str) -> None:
    """A slash nobody registered is still never read as a conjunction."""
    reading = read(sentence)

    assert reading.skills == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED
    ]


@pytest.mark.parametrize(
    "sentence", ("Python and/or R required.", "Python et/ou R required.")
)
def test_and_or_is_an_alternative_and_not_a_slash(sentence: str) -> None:
    """The posting wrote the word, whatever punctuation it wrapped it in."""
    reading = read(sentence)

    assert reading.skills == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED
    ]


def test_a_written_or_beats_a_slash_inside_one_run() -> None:
    assert reasons_of("AI/ML or Python required.") == ["ALTERNATIVE_GROUP_UNSUPPORTED"]


def test_a_slash_whose_other_side_is_unknown_is_still_guarded() -> None:
    """`Python/Julia` must not become a hard Python requirement."""
    assert skills_of("Python/Julia required.") == {}
    assert reasons_of("Python/Julia required.") == ["ALTERNATIVE_GROUP_UNSUPPORTED"]


def test_a_compound_does_not_silence_the_rest_of_the_sentence() -> None:
    reading = read("AI/ML and Python required.")

    assert {item.canonical_name: item.requirement.value for item in reading.skills} == {
        "Python": "REQUIRED"
    }
    assert [item.reason.value for item in reading.ambiguities] == [
        "COMPOUND_SKILL_EXPRESSION_UNSUPPORTED"
    ]


def test_a_compound_under_a_required_heading_refuses_and_keeps_the_neighbour() -> None:
    reading = read(
        "Required Qualifications\n- AI/ML experience plus Python required"
    )

    assert {item.canonical_name for item in reading.skills} == {"Python"}
    assert [item.reason.value for item in reading.ambiguities] == [
        "COMPOUND_SKILL_EXPRESSION_UNSUPPORTED"
    ]


def test_a_compound_clause_that_demands_nothing_refuses_nothing() -> None:
    """Clause-local reading, unchanged: a clause with no marker made no demand,
    so there is no demand to refuse — the same rule that keeps `Our stack
    includes Python or R` out of the ambiguity table."""
    reading = read("AI/ML experience plus Python required.")

    assert {item.canonical_name for item in reading.skills} == {"Python"}
    assert reading.ambiguities == ()


@pytest.mark.parametrize(
    "sentence",
    (
        "Python or JavaScript required.",
        "AWS, GCP, or Azure required.",
        "TensorFlow, PyTorch, or HuggingFace required.",
        "Databricks, Snowflake or similar required.",
    ),
)
def test_a_real_choice_is_still_refused(sentence: str) -> None:
    """The corpus is full of these and every one of them must stay a refusal."""
    reading = read(sentence)

    assert reading.skills == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED
    ]


def test_a_conjunction_is_still_two_requirements() -> None:
    assert skills_of("Python and SQL required.") == {
        "Python": "REQUIRED",
        "SQL": "REQUIRED",
    }


def test_the_compound_registry_is_closed_and_keyed_by_canonical_skill() -> None:
    known = {term.canonical_key for term in SKILL_CATALOG}
    for group in COMPOUND_SKILL_EXPRESSIONS:
        assert len(group) >= 2
        assert group <= known
    assert is_compound_expression({"artificial intelligence", "machine learning"})
    assert is_compound_expression({"machine learning", "artificial intelligence"})
    assert not is_compound_expression({"python", "r"})
    # Exact membership, never a subset: three terms are not any registered pair.
    assert not is_compound_expression(
        {"artificial intelligence", "machine learning", "large language models"}
    )


def test_the_compound_registry_refuses_a_skill_the_catalogue_lacks() -> None:
    from services.collector.extractors.opportunity_constraints.requirements import (
        skill_catalog,
    )

    with pytest.raises(SkillCatalogError):
        skill_catalog._resolve_compounds((("Python", "TEST ONLY not a skill"),))


def test_the_alternative_rule_says_its_responsibility_narrowed() -> None:
    from services.collector.extractors.opportunity_constraints.requirements import (
        skills as skill_rules,
    )

    assert skill_rules.ALTERNATIVE_GROUP_RULE_ID == "SKILL_ALTERNATIVE_GROUP_V2"
    assert (
        skill_rules.COMPOUND_EXPRESSION_RULE_ID
        == "SKILL_COMPOUND_EXPRESSION_UNSUPPORTED_V1"
    )


# --------------------------------------------------------------------------
# Bilingualism
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    (
        "Bilingualism (English/French) is a significant asset.",
        "Bilingualism (English/French) is an asset.",
        "Bilinguisme (anglais/français) est un atout.",
    ),
)
def test_bilingualism_names_both_languages_as_a_preference(sentence: str) -> None:
    """The noun, not only the adjective. A real posting writes the noun."""
    reading = read(sentence)

    assert {item.language_name: item.requirement.value for item in reading.languages} == {
        "English": "PREFERRED",
        "French": "PREFERRED",
    }
    assert reading.ambiguities == ()


def test_bilingual_still_names_both_languages_as_a_demand() -> None:
    assert languages_of("Bilingual English/French required.") == {
        "English": ("REQUIRED", None),
        "French": ("REQUIRED", None),
    }


def test_a_slashed_pair_without_the_marker_stays_refused() -> None:
    reading = read("English/French required.")

    assert reading.languages == ()
    assert [item.reason for item in reading.ambiguities] == [
        AmbiguityReason.ALTERNATIVE_GROUP_UNSUPPORTED
    ]


def test_a_written_or_still_beats_the_bilingual_marker() -> None:
    assert languages_of("Bilingual English or French required.") == {}
    assert reasons_of("Bilingual English or French required.") == [
        "ALTERNATIVE_GROUP_UNSUPPORTED"
    ]


def test_a_language_choice_is_still_refused() -> None:
    assert languages_of("English, Dutch or French required.") == {}
    assert reasons_of("English, Dutch or French required.") == [
        "ALTERNATIVE_GROUP_UNSUPPORTED"
    ]


def test_asset_needs_its_copula_to_read_as_a_preference() -> None:
    """`asset management` is a domain; `is an asset` is a preference."""
    assert skills_of("Python is an asset.") == {"Python": "PREFERRED"}
    assert skills_of("Asset management experience with Python.") == {}


# --------------------------------------------------------------------------
# Identical refusals are recorded once
# --------------------------------------------------------------------------


def test_two_identical_refusals_of_one_sentence_are_stored_once() -> None:
    """The row holds no terms, so a second identical row carries nothing."""
    reading = read("Python or R required, and Java or Scala required.")

    assert reading.skills == ()
    assert len(reading.ambiguities) == 1
    assert reading.ambiguities[0].position == 0


def test_two_refusals_with_different_words_are_both_kept() -> None:
    reading = read("Python or R required. Java or Scala required.")

    assert len(reading.ambiguities) == 2
    assert [item.position for item in reading.ambiguities] == [0, 1]
    assert len({item.text for item in reading.ambiguities}) == 2


def test_two_refusals_with_different_reasons_are_both_kept() -> None:
    reading = read("AI/ML required. Python or R required.")

    assert {item.reason.value for item in reading.ambiguities} == {
        "COMPOUND_SKILL_EXPRESSION_UNSUPPORTED",
        "ALTERNATIVE_GROUP_UNSUPPORTED",
    }
    assert [item.position for item in reading.ambiguities] == [0, 1]


def test_a_skill_refusal_and_a_language_refusal_are_both_kept() -> None:
    reading = read("Python or R required. English or French required.")

    assert [item.kind.value for item in reading.ambiguities] == ["SKILL", "LANGUAGE"]
    assert [item.position for item in reading.ambiguities] == [0, 1]


def test_deduplication_keeps_the_first_occurrence_and_renumbers_from_zero() -> None:
    reading = read(
        "AI/ML required, and AI/ML required. Python or R required."
    )

    assert [item.position for item in reading.ambiguities] == list(
        range(len(reading.ambiguities))
    )
    assert reading.ambiguities[0].reason is (
        AmbiguityReason.COMPOUND_SKILL_EXPRESSION_UNSUPPORTED
    )


# --------------------------------------------------------------------------
# The models refuse malformed readings before any write
# --------------------------------------------------------------------------


def test_a_requirement_without_evidence_is_refused() -> None:
    with pytest.raises(RequirementError):
        SkillRequirement(
            canonical_key="python",
            canonical_name="Python",
            requirement=RequirementLevel.REQUIRED,
        )


def test_a_language_requirement_without_evidence_is_refused() -> None:
    with pytest.raises(RequirementError):
        LanguageRequirement(
            language_key="english",
            language_name="English",
            requirement=RequirementLevel.REQUIRED,
        )


def test_evidence_longer_than_a_fragment_is_refused() -> None:
    with pytest.raises(RequirementError):
        RequirementEvidence(
            position=0,
            source_field=RequirementSourceField.DESCRIPTION,
            observed_requirement=RequirementLevel.REQUIRED,
            rule_id="RULE",
            text="x" * (MAX_EVIDENCE_LENGTH + 1),
        )


def test_a_reading_with_a_bad_fingerprint_is_refused() -> None:
    with pytest.raises(RequirementError):
        ExtractedRequirements(opportunity_id=1, source_fingerprint="not-a-digest")


def test_a_reading_cannot_require_one_skill_twice() -> None:
    evidence = (
        RequirementEvidence(
            position=0,
            source_field=RequirementSourceField.DESCRIPTION,
            observed_requirement=RequirementLevel.REQUIRED,
            rule_id="RULE",
            text="Python required",
        ),
    )
    duplicate = SkillRequirement("python", "Python", RequirementLevel.REQUIRED, evidence)
    with pytest.raises(RequirementError):
        ExtractedRequirements(
            opportunity_id=1,
            source_fingerprint="0" * 64,
            skills=(duplicate, duplicate),
        )


# --------------------------------------------------------------------------
# The boundary with Phase 3.6 and Phase 4
# --------------------------------------------------------------------------


def test_no_reading_carries_a_verdict_about_anybody() -> None:
    reading = read("Requirements\n- Python\n- Fluent English")
    rendered = repr(reading).casefold()

    for forbidden in (
        "eligible", "eligibility", "candidate_has", "missing_skill", "skill_gap",
        "match_score", "cosine", "tfidf", "ranking", "recommendation",
        "notification", "profile_id",
    ):
        assert forbidden not in rendered


def test_the_pure_reading_touches_no_profile_module() -> None:
    """The one import from the Digital Twin is the normalizer, and it is pure."""
    import services.collector.extractors.opportunity_constraints.requirements.skills as module

    source = open(module.__file__, encoding="utf-8").read()
    for forbidden in ("profile_skills", "profile_languages", "profiles"):
        assert forbidden not in source


def test_read_helpers_agree_with_the_rule_modules() -> None:
    """The two entry points read one posting the same way."""
    segments = parse_requirement_segments("Requirements\n- Python\n- Fluent English")
    skills, skill_refusals = read_skill_requirements(segments)
    languages, language_refusals = read_language_requirements(segments)
    reading = read("Requirements\n- Python\n- Fluent English")

    assert reading.skills == skills
    assert reading.languages == languages
    assert skill_refusals == () and language_refusals == ()
