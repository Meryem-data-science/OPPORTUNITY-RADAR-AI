"""Representative deterministic fine Data/AI category rule coverage."""

import inspect

import pytest

from services.collector.qualification import (
    FINE_CATEGORY_PRECEDENCE, FINE_CLASSIFIER_VERSION, EvidenceField, EvidenceKind,
    FineCategory, Qualification, classify_fine_categories, classify_opportunity,
)


def fine(title: str, description: str | None = None):
    """Classify exactly as the audit does: coarse relevance first, then fine category."""
    coarse = classify_opportunity(title, description)
    return classify_fine_categories(title, description, qualification=coarse.qualification)


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Data Scientist", FineCategory.DATA_SCIENCE),
        ("Senior Data Scientist", FineCategory.DATA_SCIENCE),
        ("Data Engineer", FineCategory.DATA_ENGINEERING),
        ("Analytics Engineer", FineCategory.DATA_ENGINEERING),
        ("Machine Learning Engineer", FineCategory.MACHINE_LEARNING),
        ("ML Engineer", FineCategory.MACHINE_LEARNING),
        ("AI Engineer", FineCategory.ARTIFICIAL_INTELLIGENCE),
        ("Artificial Intelligence Engineer", FineCategory.ARTIFICIAL_INTELLIGENCE),
        ("Generative AI Engineer", FineCategory.GENERATIVE_AI),
        ("LLM Engineer", FineCategory.GENERATIVE_AI),
        ("NLP Engineer", FineCategory.NLP),
        ("Computer Vision Engineer", FineCategory.COMPUTER_VISION),
        ("BI Analyst", FineCategory.BUSINESS_INTELLIGENCE),
        ("Business Intelligence Developer", FineCategory.BUSINESS_INTELLIGENCE),
        ("Data Analyst", FineCategory.DATA_ANALYTICS),
        ("MLOps Engineer", FineCategory.MLOPS),
    ],
)
def test_basic_role_titles_map_to_one_fine_category(title: str, category: FineCategory) -> None:
    result = fine(title)
    assert result.primary_category is category
    assert result.secondary_categories == ()
    assert result.classifier_version == FINE_CLASSIFIER_VERSION
    assert all(item.field is EvidenceField.TITLE for item in result.evidence)


def test_a_role_phrase_suppresses_the_broader_phrases_nested_inside_it() -> None:
    """"Generative AI Engineer" contains "ai engineer"; only the specific role counts."""
    result = fine("Generative AI Engineer")
    assert result.primary_category is FineCategory.GENERATIVE_AI
    assert [item.signal for item in result.evidence] == ["generative ai engineer"]
    assert result.evidence[0].kind is EvidenceKind.ROLE_PHRASE


@pytest.mark.parametrize(
    ("title", "primary", "secondaries"),
    [
        (
            "Machine Learning Research Engineer, Agents - Enterprise GenAI",
            FineCategory.GENERATIVE_AI, (FineCategory.MACHINE_LEARNING,),
        ),
        (
            "Machine Learning Engineer, Natural Language Processing",
            FineCategory.NLP, (FineCategory.MACHINE_LEARNING,),
        ),
        (
            "Machine Learning Engineer, Computer Vision",
            FineCategory.COMPUTER_VISION, (FineCategory.MACHINE_LEARNING,),
        ),
        (
            "Frontier Agents Engineer (Applied AI)",
            FineCategory.GENERATIVE_AI, (FineCategory.ARTIFICIAL_INTELLIGENCE,),
        ),
        (
            "GTI - Postdoctoral Researcher – AI/ML for Energy Systems",
            FineCategory.MACHINE_LEARNING, (FineCategory.ARTIFICIAL_INTELLIGENCE,),
        ),
    ],
)
def test_multi_category_titles_expose_a_deterministic_primary_and_secondaries(
    title: str, primary: FineCategory, secondaries: tuple[FineCategory, ...],
) -> None:
    result = fine(title)
    assert result.primary_category is primary
    assert result.secondary_categories == secondaries


def test_secondary_categories_follow_the_documented_precedence_order() -> None:
    result = fine("Machine Learning Engineer, Computer Vision and Data Platform")
    assert result.primary_category is FineCategory.COMPUTER_VISION
    assert result.secondary_categories == (
        FineCategory.MACHINE_LEARNING, FineCategory.DATA_ENGINEERING,
    )
    order = [FINE_CATEGORY_PRECEDENCE.index(value) for value in result.secondary_categories]
    assert order == sorted(order)


def test_title_evidence_outranks_description_evidence_for_the_primary_category() -> None:
    """A Data Engineer describing model serving stays Data Engineering, with MLOps second."""
    result = fine(
        "Data Engineer",
        "Own the data pipelines and ETL, and support model serving and model deployment.",
    )
    assert result.primary_category is FineCategory.DATA_ENGINEERING
    assert result.secondary_categories == (FineCategory.MLOPS,)
    fields = {
        (item.category, item.field) for item in result.evidence
    }
    assert (FineCategory.DATA_ENGINEERING, EvidenceField.TITLE) in fields
    assert (FineCategory.MLOPS, EvidenceField.TITLE) not in fields
    assert (FineCategory.MLOPS, EvidenceField.DESCRIPTION) in fields


@pytest.mark.parametrize(
    "title",
    ["Software Engineer", "Research Scientist", "Engineer", "Consultant", "Analyst"],
)
def test_uncertain_opportunities_receive_no_fine_category_and_never_other(title: str) -> None:
    coarse = classify_opportunity(title)
    result = fine(title)
    assert coarse.qualification is Qualification.UNCERTAIN
    assert result.primary_category is None
    assert result.primary_category is not FineCategory.OTHER
    assert (result.secondary_categories, result.evidence) == ((), ())


@pytest.mark.parametrize(
    "title",
    [
        "AI Account Executive", "AI Product Manager (Coding/Multimodal)",
        "Recruiter - AI Division", "Marketing Manager, AI Products",
        "Data Center Technician", "Strategic Projects Lead, Generative AI",
        "Head of Field Marketing and Events, Gen AI",
    ],
)
def test_out_of_scope_opportunities_receive_no_fine_category(title: str) -> None:
    coarse = classify_opportunity(title)
    result = fine(title)
    assert coarse.qualification is Qualification.OUT_OF_SCOPE
    assert result.primary_category is None
    assert result.evidence == ()


def test_a_non_target_title_is_not_rescued_by_strong_fine_description_evidence() -> None:
    result = fine(
        "AI Product Manager",
        "Coordinate model serving, model deployment, and retrieval augmented generation work.",
    )
    assert result.primary_category is None


@pytest.mark.parametrize(
    ("description", "category"),
    [
        ("Build production model serving and model deployment systems.", FineCategory.MLOPS),
        (
            "Develop machine learning systems, including model training and model evaluation.",
            FineCategory.MACHINE_LEARNING,
        ),
        (
            "Design data pipelines and ETL jobs feeding the data warehouse.",
            FineCategory.DATA_ENGINEERING,
        ),
    ],
)
def test_a_generic_title_classifies_only_from_concrete_description_concepts(
    description: str, category: FineCategory,
) -> None:
    result = fine("Software Engineer", description)
    assert result.primary_category is category
    assert all(item.kind is EvidenceKind.CONCRETE_CONCEPT for item in result.evidence)
    assert len(result.evidence) >= 2


@pytest.mark.parametrize(
    "description",
    [
        "We are an AI company building the future of technology.",
        "Our products are powered by generative AI and large language models.",
        "Join a team applying machine learning across the business.",
    ],
)
def test_weak_company_boilerplate_never_produces_a_fine_category(description: str) -> None:
    result = fine("Software Engineer", description)
    assert result.primary_category is None
    assert result.evidence == ()


def test_a_single_concrete_concept_is_a_mention_rather_than_the_work() -> None:
    """One concept, and the aliases of one concept, cannot evidence a category."""
    coarse = classify_opportunity("Data Engineer", "We occasionally do model serving.")
    single = classify_fine_categories(
        "Data Engineer", "We occasionally do model serving.",
        qualification=coarse.qualification,
    )
    aliases = classify_fine_categories(
        "Data Engineer", "We use retrieval augmented generation (RAG).",
        qualification=coarse.qualification,
    )
    assert single.secondary_categories == ()
    assert aliases.secondary_categories == ()


@pytest.mark.parametrize(
    "title",
    ["Data Governance Analyst", "Data Quality Analyst", "AI Advisory Consultant"],
)
def test_demonstrably_data_ai_roles_without_a_supported_category_become_other(title: str) -> None:
    coarse = classify_opportunity(title)
    result = fine(title)
    assert coarse.qualification in (Qualification.CORE_TARGET, Qualification.ADJACENT_TARGET)
    assert result.primary_category is FineCategory.OTHER
    assert (result.secondary_categories, result.evidence) == ((), ())


def test_other_is_never_the_answer_for_an_unknown_or_generic_role() -> None:
    assert fine("Software Engineer").primary_category is None
    assert fine("Research Scientist").primary_category is None
    assert fine("Data Governance Analyst").primary_category is FineCategory.OTHER


def test_other_is_never_a_secondary_category_and_never_carries_evidence() -> None:
    for title in ("Data Governance Analyst", "Machine Learning Engineer, Computer Vision"):
        result = fine(title)
        assert FineCategory.OTHER not in result.secondary_categories
        assert not any(item.category is FineCategory.OTHER for item in result.evidence)


def test_bi_evidence_wins_over_generic_analytics_evidence() -> None:
    result = fine("Data Analyst, Power BI Reporting")
    assert result.primary_category is FineCategory.BUSINESS_INTELLIGENCE
    assert result.secondary_categories == (FineCategory.DATA_ANALYTICS,)


def test_classification_is_reproducible_for_the_same_logical_input() -> None:
    title = "Machine Learning Research Engineer, Agents - Enterprise GenAI"
    description = "Own model training, model evaluation, and retrieval augmented generation."
    first = fine(title, description)
    second = fine(title, description)
    assert first == second
    assert first.evidence == second.evidence
    assert [item.signal for item in first.evidence] == [item.signal for item in second.evidence]
    assert first.classifier_version == second.classifier_version == FINE_CLASSIFIER_VERSION


def test_normalization_is_shared_boundary_safe_and_accent_folded() -> None:
    assert fine("INGÉNIEUR   MACHINE—LEARNING").primary_category is FineCategory.MACHINE_LEARNING
    assert fine("Data Engineer, Databricks").secondary_categories == ()


def test_location_and_employer_are_structurally_impossible_inputs() -> None:
    parameters = inspect.signature(classify_fine_categories).parameters
    assert set(parameters) == {"title", "description", "qualification"}
    assert "location" not in parameters
    assert "organization" not in parameters


@pytest.mark.parametrize("location", ["Casablanca", "Paris", "London", "New York", "Remote"])
def test_geography_cannot_change_the_fine_category(location: str) -> None:
    """The coarse classifier accepts a location; the fine result must be identical anyway."""
    coarse = classify_opportunity("Data Scientist", "Build predictive models.", location=location)
    result = classify_fine_categories(
        "Data Scientist", "Build predictive models.", qualification=coarse.qualification
    )
    assert result == fine("Data Scientist", "Build predictive models.")
    assert result.primary_category is FineCategory.DATA_SCIENCE


def test_the_fine_version_is_distinct_from_the_coarse_classifier_version() -> None:
    """Phase 8A.2 changed observable fine rules, so the fine version had to move.

    The pin was ``fine-data-ai-rules-v1`` until the single-concept fallback and
    the concrete NLP/computer-vision concepts changed observable output. The two
    versions stay independent: each moves only when its own rules change.
    """
    from services.collector.qualification import CLASSIFIER_VERSION

    assert FINE_CLASSIFIER_VERSION == "fine-data-ai-rules-v2"
    assert CLASSIFIER_VERSION == "qualification-rules-v2"
    assert FINE_CLASSIFIER_VERSION != CLASSIFIER_VERSION


def test_every_fine_category_appears_exactly_once_in_the_precedence() -> None:
    assert set(FINE_CATEGORY_PRECEDENCE) == set(FineCategory)
    assert len(FINE_CATEGORY_PRECEDENCE) == len(FineCategory)
    assert FINE_CATEGORY_PRECEDENCE[-1] is FineCategory.OTHER


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("Senior Software Engineer, Data Platform", FineCategory.DATA_ENGINEERING),
        ("Software Engineer, Data Infrastructure", FineCategory.DATA_ENGINEERING),
        ("Software Engineer, Frontier AI Infrastructure", FineCategory.MLOPS),
        ("AI Infrastructure Engineer, Model Serving Platform", FineCategory.MLOPS),
        ("ML Research Engineer, ML Systems", FineCategory.MLOPS),
        ("ML Systems Engineer, Robotics", FineCategory.MLOPS),
        ("Senior Software Engineer, GenAI", FineCategory.GENERATIVE_AI),
        ("Forward Deployed Engineer, GenAI", FineCategory.GENERATIVE_AI),
        ("Software Engineer, Enterprise AI", FineCategory.ARTIFICIAL_INTELLIGENCE),
        ("Director of Engineering, Physical AI", FineCategory.ARTIFICIAL_INTELLIGENCE),
        ("Senior Machine Learning Research Engineer", FineCategory.MACHINE_LEARNING),
        ("Machine Learning Fellow", FineCategory.MACHINE_LEARNING),
        ("CUS - Postdoctoral Researcher in Spatial Data Science", FineCategory.DATA_SCIENCE),
        ("AI Builder Intern", FineCategory.ARTIFICIAL_INTELLIGENCE),
    ],
)
def test_real_corpus_structural_titles_keep_a_stable_fine_category(
    title: str, category: FineCategory,
) -> None:
    assert fine(title).primary_category is category


@pytest.mark.parametrize(
    "title",
    [
        "Solutions Engineer, Enterprise", "Software Engineer, Platform",
        "DevOps Engineer, Infrastructure & Security", "Business Analyst Intern",
        "AgBS - Post-Doctoral in Modeling and Crop Yield Prediction in Africa",
    ],
)
def test_real_corpus_known_false_positives_gain_no_fine_category(title: str) -> None:
    assert fine(title).primary_category is None


# --------------------------------------------------------------------------
# Phase 8A.2: fine-category calibration against the real operational corpus.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "primary", "secondaries"),
    [
        (
            "Data Science Intern - GenAI",
            FineCategory.GENERATIVE_AI, (FineCategory.DATA_SCIENCE,),
        ),
        ("Director, AI & Data Science", FineCategory.DATA_SCIENCE, ()),
        ("Data Analytics Manager", FineCategory.DATA_ANALYTICS, ()),
        ("Tech Lead - GenAI", FineCategory.GENERATIVE_AI, ()),
        ("Data Architect", FineCategory.DATA_ENGINEERING, ()),
    ],
)
def test_recovered_real_corpus_titles_receive_a_supported_fine_category(
    title: str, primary: FineCategory, secondaries: tuple[FineCategory, ...],
) -> None:
    """The five coarse false negatives now reach the fine classifier as well.

    "Data Architect" is a fine DATA_ENGINEERING role phrase for the same reason
    it is a coarse one: the architect designs the storage and pipeline layer.
    """
    result = fine(title)
    assert result.primary_category is primary
    assert result.secondary_categories == secondaries
    assert result.evidence


@pytest.mark.parametrize(
    "title",
    [
        "AI Advisory Consultant", "AI Advisory Principal", "AI Strategy Consultant, Frontier Tech",
        "Senior Consultant (m/f/d) Data & AI Strategy",
    ],
)
def test_data_ai_advisory_roles_stay_other_without_concrete_sub_domain_work(title: str) -> None:
    """Advisory is not a supported fine sub-domain, and "AI" in a title is not evidence.

    These are qualified Data/AI opportunities with no supported technical
    sub-domain, which is exactly what OTHER states.
    """
    coarse = classify_opportunity(title, "Advise executives on their AI roadmap.")
    result = classify_fine_categories(
        title, "Advise executives on their AI roadmap.", qualification=coarse.qualification
    )
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is FineCategory.OTHER
    assert result.secondary_categories == ()


def test_a_qualified_technical_role_with_scattered_concepts_is_no_longer_other() -> None:
    """The coarse gate counts concepts across domains; the fine threshold counts within one.

    A real technical role could therefore be proven Data/AI and still evidence
    no single fine category, landing in OTHER — which claims no sub-domain
    applies. The concepts that did match are weaker evidence than two, but they
    are evidence, and they are more faithful than that claim.
    """
    title, description = (
        "Forward Deployed Software Engineer, Public Sector",
        "Own model deployment for customer systems and run experimentation on rollouts.",
    )
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.CORE_TARGET
    assert result.primary_category is FineCategory.MLOPS
    assert result.secondary_categories == (FineCategory.DATA_SCIENCE,)
    assert all(item.kind is EvidenceKind.CONCRETE_CONCEPT for item in result.evidence)
    assert [item.signal for item in result.evidence] == ["model deployment", "experimentation"]


def test_the_single_concept_fallback_can_only_ever_replace_other() -> None:
    """It fires only where there is no title evidence and nothing met the threshold."""
    # Title evidence exists: one stray concept stays invisible, as before.
    titled = fine("Data Engineer", "We occasionally do model serving.")
    assert titled.primary_category is FineCategory.DATA_ENGINEERING
    assert titled.secondary_categories == ()
    # A category met the threshold: the categories below it stay invisible too.
    threshold = fine(
        "Software Engineer",
        "Build model serving and model deployment, and occasionally object detection.",
    )
    assert threshold.primary_category is FineCategory.MLOPS
    assert threshold.secondary_categories == ()


def test_a_qualified_role_naming_no_concrete_concept_still_becomes_other() -> None:
    assert fine("Data Governance Analyst").primary_category is FineCategory.OTHER
    assert fine(
        "Data Governance Analyst", "We are an AI company building the future."
    ).primary_category is FineCategory.OTHER


def test_an_unqualified_role_is_never_rescued_by_the_fallback() -> None:
    """The fallback runs behind the coarse gate, exactly like every other fine rule."""
    coarse = classify_opportunity("Software Engineer", "We sometimes do model serving.")
    result = fine("Software Engineer", "We sometimes do model serving.")
    assert coarse.qualification is Qualification.UNCERTAIN
    assert result.primary_category is None
    assert result.evidence == ()


@pytest.mark.parametrize(
    ("title", "description", "category"),
    [
        (
            "Research Scientist",
            "Research natural language processing, named entity recognition and "
            "text classification for document understanding.",
            FineCategory.NLP,
        ),
        (
            "Software Engineer",
            "Build computer vision pipelines covering object detection and image segmentation.",
            FineCategory.COMPUTER_VISION,
        ),
    ],
)
def test_explicit_nlp_and_vision_descriptions_produce_those_categories(
    title: str, description: str, category: FineCategory,
) -> None:
    """The real audit produced no NLP or COMPUTER_VISION primary, for two reasons.

    The coarse concept table held neither the concrete NLP nor the concrete
    vision work concepts, so such a description could not clear the coarse gate;
    and the fine concept tables did not treat "natural language processing" or
    "computer vision" as concepts at all, only as title context. Both are fixed
    here, and both remain two-concept rules.
    """
    result = fine(title, description)
    assert result.primary_category is category
    assert len(result.evidence) >= 2
    assert all(item.kind is EvidenceKind.CONCRETE_CONCEPT for item in result.evidence)


@pytest.mark.parametrize(
    ("title", "category"),
    [("NLP Engineer", FineCategory.NLP), ("Computer Vision Engineer", FineCategory.COMPUTER_VISION)],
)
def test_explicit_nlp_and_vision_titles_are_unaffected_by_the_calibration(
    title: str, category: FineCategory,
) -> None:
    assert fine(title).primary_category is category


def test_a_lone_vision_sentence_cannot_reclassify_an_unrelated_qualified_role() -> None:
    """One incidental mention is a mention. The role keeps its own category."""
    result = fine("Data Engineer", "Some teams here also work on object detection.")
    assert result.primary_category is FineCategory.DATA_ENGINEERING
    assert FineCategory.COMPUTER_VISION not in result.secondary_categories


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Data Science Intern - GenAI", "Work on generative AI and large language models."),
        ("Forward Deployed Software Engineer, Public Sector", "Own model deployment and experimentation."),
        ("Data Analytics Manager", "Own dbt models and Power BI dashboards."),
    ],
)
def test_calibrated_fine_results_are_byte_for_byte_reproducible(
    title: str, description: str,
) -> None:
    first, second = fine(title, description), fine(title, description)
    assert first == second
    assert first.evidence == second.evidence
    assert first.reasons == second.reasons
    assert first.secondary_categories == second.secondary_categories


# --------------------------------------------------------------------------
# Phase 8A.2 review correction: the one-concept fallback is technical-only.
#
# As first written the fallback fired for any qualified role, so a Data
# Governance Analyst mentioning "model serving" once became MLOPS and an AI
# Advisory Consultant mentioning RAG once became GENERATIVE_AI. One incidental
# technical concept must not convert a non-technical advisory or governance
# OTHER into a technical sub-domain.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "description", "primary", "secondaries"),
    [
        (
            "Forward Deployed Software Engineer, Public Sector",
            "Own model deployment for customer systems and run experimentation on rollouts.",
            FineCategory.MLOPS, (FineCategory.DATA_SCIENCE,),
        ),
        (
            "Senior Mission Software Engineer, Public Sector",
            "Own model serving for the platform and build data pipelines for mission data.",
            FineCategory.MLOPS, (FineCategory.DATA_ENGINEERING,),
        ),
        (
            "Senior Manager, Research Scientist",
            "Lead research using deep learning and build the data pipelines behind it.",
            FineCategory.MACHINE_LEARNING, (FineCategory.DATA_ENGINEERING,),
        ),
    ],
)
def test_the_fallback_still_serves_the_technical_roles_that_motivated_it(
    title: str, description: str, primary: FineCategory, secondaries: tuple[FineCategory, ...],
) -> None:
    """Concepts scattered one-per-category across a real technical role.

    The coarse gate counts strong concepts across domains and qualified these;
    the fine threshold counts within one category and reached none of them.
    """
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.CORE_TARGET
    assert result.primary_category is primary
    assert result.secondary_categories == secondaries
    assert result.reasons == (
        "qualified technical Data/AI role with no fine title evidence and no category "
        "reaching the description threshold; the single concrete concepts matched are "
        "more faithful than OTHER",
    )


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Data Governance Analyst", "We occasionally do model serving."),
        ("Data Governance Analyst", "Some dashboarding happens here."),
        ("Data Quality Analyst", "The team touches feature engineering now and then."),
    ],
)
def test_a_governance_role_is_not_reclassified_by_one_incidental_concept(
    title: str, description: str,
) -> None:
    """OTHER is the true statement: demonstrably Data/AI, no supported sub-domain."""
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is FineCategory.OTHER
    assert (result.secondary_categories, result.evidence) == ((), ())


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("AI Advisory Consultant", "We sometimes prototype with RAG."),
        ("AI Strategy Consultant, Frontier Tech", "Model deployment comes up occasionally."),
        ("AI Advisory Principal", "Clients ask about fine tuning from time to time."),
        ("Senior Consultant (m/f/d) Data & AI Strategy", "Model monitoring is on the roadmap."),
    ],
)
def test_an_advisory_role_is_not_reclassified_by_one_incidental_concept(
    title: str, description: str,
) -> None:
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is FineCategory.OTHER
    assert (result.secondary_categories, result.evidence) == ((), ())


@pytest.mark.parametrize(
    "title", ["Consultant Software Engineer, Delivery", "Software Engineer, Advisory Services"],
)
def test_an_advisory_marker_vetoes_the_fallback_even_beside_a_technical_family(
    title: str,
) -> None:
    """Advising on Data/AI is not building it, whatever else the title says.

    The coarse classifier caps exactly these titles at ADJACENT_TARGET for the
    same reason, and the fine fallback reads the same marker. Note that an
    explicit core role phrase outranks the cap on both sides: "Research Scientist,
    AI Strategy" is core, and it never reaches the fallback anyway because that
    phrase is fine title evidence.
    """
    description = "Own model deployment and run experimentation on client rollouts."
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is FineCategory.OTHER
    assert (result.secondary_categories, result.evidence) == ((), ())


def test_the_fallback_reuses_the_coarse_technical_vocabulary_not_a_second_list() -> None:
    """The predicate is the coarse classifier's own, so the two cannot drift.

    GENERIC_TECHNICAL_TITLES is deliberately *not* the source: it also holds
    "analyst" and "consultant", which are the two families this correction is
    about.
    """
    from services.collector.qualification.classifier import (
        advisory_role_families, technical_role_families,
    )
    from services.collector.qualification.taxonomy import GENERIC_TECHNICAL_TITLES

    assert technical_role_families("forward deployed software engineer public sector")
    assert technical_role_families("senior manager research scientist")
    assert technical_role_families("cus postdoctoral researcher in spatial data science")
    assert not technical_role_families("data governance analyst")
    assert not technical_role_families("ai advisory consultant")
    assert advisory_role_families("ai advisory consultant")
    assert "analyst" in GENERIC_TECHNICAL_TITLES and "consultant" in GENERIC_TECHNICAL_TITLES


@pytest.mark.parametrize(
    ("title", "description", "category"),
    [
        (
            "Data Governance Analyst",
            "Own the Power BI estate and the dashboarding standards.",
            FineCategory.BUSINESS_INTELLIGENCE,
        ),
        (
            "AI Advisory Consultant",
            "Own retrieval augmented generation and prompt engineering workstreams.",
            FineCategory.GENERATIVE_AI,
        ),
    ],
)
def test_the_normal_two_concept_rule_stays_unrestricted_for_every_qualified_role(
    title: str, description: str, category: FineCategory,
) -> None:
    """The chosen semantics, stated explicitly: the threshold rule is not technical-only.

    Two *independent* concrete concepts inside one category are the work being
    described, not a passing mention, and that is a far stronger claim than the
    single concept the fallback rests on. A governance analyst who owns the BI
    estate is doing business intelligence; an advisory consultant who owns RAG
    and prompt engineering workstreams is doing generative AI. Only the weaker
    one-concept fallback is restricted to technical role families.
    """
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is category
    assert len(result.evidence) >= 2
    assert result.reasons == (
        "no fine title evidence; concrete description concepts decide the primary category",
    )


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Software Engineer", "We sometimes do model serving."),
        ("Research Scientist", "The platform includes some object detection."),
        ("AI Product Manager", "Coordinate model serving work."),
        ("Recruiter - AI Division", "Our teams own model deployment."),
    ],
)
def test_an_unqualified_role_is_still_never_rescued_by_the_restricted_fallback(
    title: str, description: str,
) -> None:
    """The fallback runs behind the coarse gate; a technical family is not a bypass."""
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification in (Qualification.UNCERTAIN, Qualification.OUT_OF_SCOPE)
    assert result.primary_category is None
    assert (result.secondary_categories, result.evidence) == ((), ())


def test_the_two_description_rules_stay_distinct_on_the_same_technical_role() -> None:
    """One title, three descriptions, three different rules — and three reasons."""
    title = "Forward Deployed Software Engineer, Public Sector"
    threshold = fine(title, "Own model serving and model deployment for customer systems.")
    fallback = fine(title, "Own model deployment and run experimentation on rollouts.")
    assert threshold.primary_category is FineCategory.MLOPS
    assert threshold.secondary_categories == ()
    assert fallback.primary_category is FineCategory.MLOPS
    assert fallback.secondary_categories == (FineCategory.DATA_SCIENCE,)
    assert threshold.reasons != fallback.reasons


# --------------------------------------------------------------------------
# Phase 8A.2 second calibration pass: fine categories for the newly recovered
# real families. The fine tables mirror only the coarse rules that name a
# supported sub-domain; the cross-cutting ones stay OTHER.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "primary", "secondaries"),
    [
        ("Delivery AI Scientist", FineCategory.ARTIFICIAL_INTELLIGENCE, ()),
        ("Senior Delivery AI Scientist", FineCategory.ARTIFICIAL_INTELLIGENCE, ()),
        ("Senior, AI & Data Science", FineCategory.DATA_SCIENCE, ()),
        ("Director, AI & Agentic Engineering", FineCategory.GENERATIVE_AI, ()),
        ("Tech Lead Manager- MLRE, ML Systems", FineCategory.MLOPS, ()),
        ("Senior Full Stack Developer - GenAI Solutions", FineCategory.GENERATIVE_AI, ()),
        (
            "Assistant Professor in Data Science and Artificial Intelligence",
            FineCategory.ARTIFICIAL_INTELLIGENCE, (FineCategory.DATA_SCIENCE,),
        ),
    ],
)
def test_second_pass_recovered_titles_get_a_deterministic_supported_category(
    title: str, primary: FineCategory, secondaries: tuple[FineCategory, ...],
) -> None:
    """"ai scientist" and "agentic engineering" were mirrored into the fine tables.

    The professorship evidences both fields, and precedence decides:
    ARTIFICIAL_INTELLIGENCE primary, DATA_SCIENCE secondary.
    """
    result = fine(title)
    assert result.primary_category is primary
    assert result.secondary_categories == secondaries
    assert result.evidence


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Data Consultant", ""),
        ("Data Consultant Intern", "We occasionally do model serving for clients."),
        ("Data Consulting Manager", "Model deployment comes up sometimes."),
        ("Manager, AI & Data Consulting", "Clients ask about fine tuning."),
        ("AI Transformation Manager", "We sometimes touch model monitoring."),
        ("AI & Automation Intern", "Some model training happens here."),
    ],
)
def test_consulting_and_cross_cutting_families_stay_other(title: str, description: str) -> None:
    """Data consulting and AI transformation are not fine technical sub-domains.

    They are demonstrably Data/AI, which is exactly what OTHER states, and none
    of them may reach the one-concept fallback: the consulting titles carry an
    advisory marker and none of them matches a technical role family. They are
    never forced into ARTIFICIAL_INTELLIGENCE just because "AI" is in the title.
    """
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is FineCategory.OTHER
    assert (result.secondary_categories, result.evidence) == ((), ())


def test_the_normal_two_concept_rule_still_reaches_a_consulting_role() -> None:
    """The accepted correction is preserved exactly: only the weak rule is restricted."""
    title, description = (
        "Data Consultant", "Own the data pipelines and ETL for client warehouses.",
    )
    coarse = classify_opportunity(title, description)
    result = classify_fine_categories(title, description, qualification=coarse.qualification)
    assert coarse.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_category is FineCategory.DATA_ENGINEERING
    assert len(result.evidence) >= 2


@pytest.mark.parametrize(
    "title",
    [
        "Full Stack Developer", "Assistant Professor of Economics", "Automation Manager",
        "Digital Transformation Manager", "Senior Product Manager, Data Science",
        "Senior Marketing Manager, Generative AI", "Data Center Manager",
    ],
)
def test_second_pass_negatives_receive_no_fine_category(title: str) -> None:
    assert fine(title).primary_category is None
