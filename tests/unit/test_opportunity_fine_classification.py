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
    from services.collector.qualification import CLASSIFIER_VERSION

    assert FINE_CLASSIFIER_VERSION == "fine-data-ai-rules-v1"
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
