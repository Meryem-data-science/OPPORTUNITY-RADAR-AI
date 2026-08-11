"""Representative deterministic qualification rule coverage."""

import pytest

from services.collector.qualification import (
    Domain, EmploymentType, OpportunityType, Qualification, classify_opportunity,
    normalize_text,
)


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("Data Engineer", Domain.DATA_ENGINEERING),
        ("Senior Data Engineer", Domain.DATA_ENGINEERING),
        ("Analytics Engineer", Domain.DATA_ENGINEERING),
        ("Data Scientist", Domain.DATA_SCIENCE),
        ("Machine Learning Engineer", Domain.MACHINE_LEARNING_AI),
        ("ML Engineer", Domain.MACHINE_LEARNING_AI),
        ("AI Engineer", Domain.MACHINE_LEARNING_AI),
        ("Generative AI Engineer", Domain.GENAI_LLM),
        ("LLM Engineer", Domain.GENAI_LLM),
        ("MLOps Engineer", Domain.MLOPS_ML_PLATFORM),
        ("Software Engineer, Machine Learning", Domain.MACHINE_LEARNING_AI),
        ("Ingénieur Data", Domain.DATA_ENGINEERING),
        ("Ingénieur Intelligence Artificielle", Domain.MACHINE_LEARNING_AI),
        ("Ingénieur Machine Learning", Domain.MACHINE_LEARNING_AI),
    ],
)
def test_core_titles(title: str, domain: Domain) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is domain
    assert result.matched_title_signals


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("Data Analyst", Domain.BI_ANALYTICS),
        ("BI Analyst", Domain.BI_ANALYTICS),
        ("Business Intelligence Developer", Domain.BI_ANALYTICS),
        ("Data Quality Analyst", Domain.DATA_QUALITY_GOVERNANCE),
        ("Data Governance Analyst", Domain.DATA_QUALITY_GOVERNANCE),
        ("Analyste de données", Domain.BI_ANALYTICS),
        ("Développeur BI", Domain.BI_ANALYTICS),
    ],
)
def test_adjacent_titles(title: str, domain: Domain) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is domain


@pytest.mark.parametrize(
    "title",
    [
        "Data Center Technician", "Data Center Operations Manager", "AI Account Executive",
        "AI Sales Manager", "Recruiter - AI Division", "Marketing Manager, AI Products",
        "Customer Success Manager - Data Platform",
    ],
)
def test_obvious_product_and_infrastructure_false_positives_are_out_of_scope(title: str) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.OUT_OF_SCOPE
    assert result.primary_domain is Domain.NON_TARGET
    assert result.matched_exclusion_signals


@pytest.mark.parametrize("title", ["Software Engineer", "Research Scientist", "Analyst", "Consultant", "Engineer"])
def test_ambiguous_titles_remain_uncertain(title: str) -> None:
    assert classify_opportunity(title).qualification is Qualification.UNCERTAIN


def test_strong_description_can_disambiguate_generic_technical_title() -> None:
    result = classify_opportunity(
        "Software Engineer", "Build services as a machine learning engineer on our ML platform engineer team."
    )
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.MLOPS_ML_PLATFORM
    assert len(result.matched_description_signals) == 2


def test_primary_domain_precedence_is_deterministic_and_all_domains_are_exposed() -> None:
    result = classify_opportunity("Data Engineer / LLM Engineer")
    assert result.primary_domain is Domain.GENAI_LLM
    assert result.matched_domains == (Domain.GENAI_LLM, Domain.DATA_ENGINEERING)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Stage PFE Data Scientist", OpportunityType.PFE),
        ("Data Engineer Intern", OpportunityType.INTERNSHIP),
        ("Stagiaire Data Analyst", OpportunityType.INTERNSHIP),
        ("Graduate Data Engineer", OpportunityType.GRADUATE),
        ("Alternance Data Engineer", OpportunityType.APPRENTICESHIP),
        ("Senior Data Engineer", OpportunityType.JOB),
    ],
)
def test_opportunity_type_precedence(title: str, expected: OpportunityType) -> None:
    assert classify_opportunity(title).opportunity_type is expected


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("This is a full-time role.", EmploymentType.FULL_TIME),
        ("Part-time position.", EmploymentType.PART_TIME),
        ("Freelance contract.", EmploymentType.CONTRACT),
        ("Fixed-term contract.", EmploymentType.CONTRACT),
        ("Temporary assignment.", EmploymentType.TEMPORARY),
        ("Join our engineering team.", EmploymentType.UNKNOWN),
    ],
)
def test_employment_type_is_conservative(description: str, expected: EmploymentType) -> None:
    assert classify_opportunity("Data Engineer", description).employment_type is expected


def test_conflicting_employment_signals_are_unknown() -> None:
    result = classify_opportunity("Data Engineer", "May be full-time or part-time.")
    assert result.employment_type is EmploymentType.UNKNOWN


def test_normalization_is_nfkc_casefolded_boundary_safe_and_explainable() -> None:
    assert normalize_text("  ＤＡＴＡ—Engineer\n") == "data engineer"
    assert classify_opportunity("Database Engineer").qualification is Qualification.UNCERTAIN
    french = classify_opportunity("INGÉNIEUR   INTELLIGENCE—ARTIFICIELLE")
    assert french.qualification is Qualification.CORE_TARGET
    assert french.matched_title_signals == ("ingenieur intelligence artificielle",)


@pytest.mark.parametrize("location", ["Casablanca", "Paris", "London", "New York", "Remote"])
def test_geography_never_changes_topical_qualification(location: str) -> None:
    result = classify_opportunity("Data Engineer", location=location)
    assert (result.qualification, result.primary_domain) == (
        Qualification.CORE_TARGET, Domain.DATA_ENGINEERING,
    )


def test_quality_flags_are_diagnostic_and_do_not_downgrade_qualification() -> None:
    result = classify_opportunity("Data Engineer", "", source_url="https://example.test/job")
    assert result.qualification is Qualification.CORE_TARGET
    assert "EMPTY_OR_NEAR_EMPTY_DESCRIPTION" in result.quality_flags
    assert "MISSING_APPLICATION_URL" in result.quality_flags
    assert "MISSING_CANONICAL_URL" in result.quality_flags
