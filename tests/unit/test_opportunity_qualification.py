"""Representative deterministic qualification rule coverage."""

import inspect

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


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("AI Infrastructure Engineer, Model Serving Platform", Domain.MLOPS_ML_PLATFORM),
        ("AI Infrastructure Engineer, Sandbox Platform", Domain.MLOPS_ML_PLATFORM),
        ("AI Infrastructure Engineer, Serving Platform", Domain.MLOPS_ML_PLATFORM),
        ("Forward Deployed Engineer, GenAI", Domain.GENAI_LLM),
        ("Frontier Agents Engineer (Applied AI)", Domain.GENAI_LLM),
        ("Machine Learning Fellow", Domain.MACHINE_LEARNING_AI),
        ("Machine Learning Research Engineer, Agents - Enterprise GenAI", Domain.GENAI_LLM),
        ("ML Research Engineer, ML Systems", Domain.MLOPS_ML_PLATFORM),
        ("ML Systems Engineer, Robotics", Domain.MLOPS_ML_PLATFORM),
        ("Senior Machine Learning Research Engineer", Domain.MACHINE_LEARNING_AI),
        ("Senior Software Engineer, Data Platform", Domain.DATA_ENGINEERING),
        ("Staff Software Engineer, Data Platform", Domain.DATA_ENGINEERING),
        ("Software Engineer, Data Infrastructure", Domain.DATA_ENGINEERING),
        ("Senior Software Engineer, GenAI", Domain.GENAI_LLM),
        ("Software Engineer, Enterprise AI", Domain.MACHINE_LEARNING_AI),
        ("Software Engineer, Frontier AI Infrastructure", Domain.MLOPS_ML_PLATFORM),
        (
            "Staff Machine Learning Research Engineer, Agent Post-training - Enterprise GenAI",
            Domain.GENAI_LLM,
        ),
        ("Staff Infrastructure Software Engineer, Enterprise AI", Domain.MACHINE_LEARNING_AI),
        ("Director of Engineering, Physical AI", Domain.MACHINE_LEARNING_AI),
    ],
)
def test_real_corpus_structural_core_titles(title: str, domain: Domain) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is domain
    assert "technical role family + explicit Data/AI title context" in result.reasons
    assert result.matched_title_signals


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("CUS - Postdoctoral Researcher in Spatial Data Science", Domain.DATA_SCIENCE),
        ("GTI - Postdoctoral Researcher – AI/ML for Energy Systems", Domain.MACHINE_LEARNING_AI),
        (
            "CBS - Postdoctoral Position, Artificial Intelligence Applied to Multi-Omics Data Integration",
            Domain.MACHINE_LEARNING_AI,
        ),
    ],
)
def test_data_ai_postdoc_titles_are_core_jobs(title: str, domain: Domain) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is domain
    assert result.opportunity_type is OpportunityType.JOB


@pytest.mark.parametrize(
    "title",
    [
        "AgBS - Post-Doctoral in Modeling and Crop Yield Prediction in Africa",
        "CRSA - Postdoctoral Research Fellowship, Developing Advanced Weather Prediction Models",
        "COLCOM - Postdoctoral Researcher in Multimodal Crop Analysis & Fertilizer Optimization",
    ],
)
def test_generic_modeling_prediction_postdocs_remain_uncertain(title: str) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.UNCERTAIN
    assert result.opportunity_type is OpportunityType.JOB


@pytest.mark.parametrize("title", ["AI Advisory Consultant", "AI Strategy Consultant, Frontier Tech"])
def test_explicit_ai_advisory_roles_are_adjacent(title: str) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is Domain.OTHER_DATA_AI


@pytest.mark.parametrize(
    "title",
    [
        "[Annotations] Operations Associate",
        "[Annotations] Operations Program Manager",
        "Associate General Counsel, Commercial",
        "Chief of Staff, Public Sector Engineering & Security",
        "Communications Senior Manager, Corporate & Product (Enterprise)",
        "Director of Product Management, Forward Deployed & Strategy",
        "AI Product Manager (Coding/Multimodal)",
        "Product Manager, Gen AI",
        "Senior AI Product Manager, Code",
        "Director of Product Strategy, Physical AI",
        "Technical Program Manager, Gen AI Operations Planning",
        "Head of Field Marketing and Events, Gen AI",
        "Strategic Projects Lead, Generative AI",
        "Finance Systems & Automations Manager",
        "Finance Fellow - Human Frontier Collective",
        "Associate General Counsel, Commercial",
        "HR Manager",
        "Recruiting Coordinator, Contract",
        "University Recruiter, Contract",
        "Sales Enablement Lead",
        "Technical Writer",
        "Executive Assistant",
        "Support Specialist",
        "Proposals Manager",
        "Engagement Management Lead",
        "Business Development Representative, Partnerships (Physical AI)",
        "Enterprise Account Executive",
        "Senior Corporate Accountant",
    ],
)
def test_real_corpus_non_target_role_families_win_over_ai_context(title: str) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.OUT_OF_SCOPE
    assert result.primary_domain is Domain.NON_TARGET
    assert result.matched_exclusion_signals


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer", "Research Scientist", "Business Analyst Intern",
        "IT & Automation Intern - Unpaid Student Internship", "Portfolio Performance Intern",
        "Solutions Engineer, Enterprise", "Principal Solutions Engineer, Enterprise",
        "Software Engineer, Platform", "Senior Software Engineer, Full-Stack",
        "DevOps Engineer, Infrastructure & Security",
    ],
)
def test_real_corpus_generic_roles_stay_uncertain(title: str) -> None:
    assert classify_opportunity(title).qualification is Qualification.UNCERTAIN


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("AI Builder Intern", "New graduates may apply.", OpportunityType.INTERNSHIP),
        (
            "Business Development Representative, Partnerships (Physical AI)",
            "We hire interns and recent graduates.", OpportunityType.JOB,
        ),
        ("University Recruiter, Contract", "Support our internship program.", OpportunityType.JOB),
        ("Stage PFE Data Scientist", "Internship role.", OpportunityType.PFE),
        ("Alternance Data Engineer", "Graduate applicants welcome.", OpportunityType.APPRENTICESHIP),
        ("Graduate Data Engineer", "We hire interns.", OpportunityType.GRADUATE),
        ("Postdoctoral Researcher in Spatial Data Science", "", OpportunityType.JOB),
    ],
)
def test_title_first_opportunity_type_blocks_description_leakage(
    title: str, description: str, expected: OpportunityType,
) -> None:
    assert classify_opportunity(title, description).opportunity_type is expected


def test_description_pfe_is_the_narrow_high_confidence_fallback() -> None:
    result = classify_opportunity("Data Science Intern", "Stage PFE: projet de fin d'études.")
    assert result.opportunity_type is OpportunityType.PFE


def test_generic_software_role_requires_independent_description_context() -> None:
    promoted = classify_opportunity(
        "Software Engineer",
        "Develop machine learning systems, including model training and model evaluation.",
    )
    boilerplate = classify_opportunity(
        "Software Engineer", "We are an AI company building the future of technology."
    )
    assert promoted.qualification is Qualification.CORE_TARGET
    assert len(promoted.matched_description_signals) >= 2
    assert boilerplate.qualification is Qualification.UNCERTAIN


def test_generic_research_role_can_use_multiple_strong_description_signals() -> None:
    result = classify_opportunity(
        "Research Scientist", "Research deep learning, model training, and model evaluation methods."
    )
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.MACHINE_LEARNING_AI


@pytest.mark.parametrize("title", ["Product Manager", "Recruiter"])
def test_non_target_title_cannot_be_rescued_by_strong_ai_description(title: str) -> None:
    result = classify_opportunity(
        title, "Lead machine learning, model training, model evaluation, and generative AI programs."
    )
    assert result.qualification is Qualification.OUT_OF_SCOPE


def test_organization_cannot_participate_in_classification() -> None:
    assert "organization" not in inspect.signature(classify_opportunity).parameters
    title = "Senior Software Engineer, Data Platform"
    scale_ai_result = classify_opportunity(title, "Build data pipelines.")
    different_employer_result = classify_opportunity(title, "Build data pipelines.")
    assert scale_ai_result == different_employer_result
