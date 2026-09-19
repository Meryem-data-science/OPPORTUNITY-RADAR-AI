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
        "Software Engineer", "Build production model serving and model deployment systems."
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


# The French abbreviation of "intelligence artificielle". The rows below are the
# Phase 11.3A-R4A correction: a real Moroccan posting titled "Software Engineer &
# IA (Stage)" was read as UNCERTAIN while its English spelling qualified, so the
# CORE machine-learning row now names the French role phrases too — and only the
# phrases, never the bare token.
@pytest.mark.parametrize("title", [
    "Software Engineer & IA (Stage)",
    "Ingénieur IA",
    "Ingénieure IA",
    "Développeur IA",
    "Développeuse IA",
])
def test_a_french_ia_role_phrase_qualifies_exactly_like_its_english_spelling(title: str) -> None:
    result = classify_opportunity(title, "Stage. Missions générales.")

    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.MACHINE_LEARNING_AI


def test_the_english_ai_spelling_is_unchanged_by_the_french_rows() -> None:
    english = classify_opportunity("Software Engineer & AI (Stage)", "Stage.")
    french = classify_opportunity("Software Engineer & IA (Stage)", "Stage.")

    assert english.qualification is french.qualification is Qualification.CORE_TARGET
    assert english.primary_domain is french.primary_domain is Domain.MACHINE_LEARNING_AI
    assert "software engineer ai" in english.matched_title_signals
    assert "software engineer ia" in french.matched_title_signals


@pytest.mark.parametrize("title, description", [
    # A media role that merely works with AI tooling is not a Data/AI job family.
    ("AI Film maker & Content Creator (Stage)", "Création de contenu vidéo."),
    ("Vidéaste & Photographe | Spécialiste IA & Création de Contenu", "Photographie et vidéo."),
    # Prose containing the abbreviation, with no Data/AI role phrase at all.
    ("Chargé de communication", "Nous utilisons l'IA et l'IA générative pour nos campagnes."),
    ("Assistant administratif", "Le service IA est au deuxième étage."),
])
def test_the_bare_french_abbreviation_never_qualifies_on_its_own(title: str, description: str) -> None:
    result = classify_opportunity(title, description)

    assert result.qualification is Qualification.UNCERTAIN
    assert result.primary_domain is Domain.UNKNOWN


def test_no_bare_two_letter_abbreviation_is_a_signal_anywhere() -> None:
    """`ia` alone collides with ordinary prose, exactly as bare `ai` does."""
    from services.collector.qualification.taxonomy import (
        ADJACENT_SIGNALS, CORE_SIGNALS, DOMAIN_CONTEXT_SIGNALS,
    )

    for table in (CORE_SIGNALS, ADJACENT_SIGNALS, DOMAIN_CONTEXT_SIGNALS):
        for signals in table.values():
            assert "ia" not in signals
            assert "ai" not in signals


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


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        (
            "Senior Data Engineer",
            "Mentor students during their projet de fin d'études and PFE internships.",
            OpportunityType.JOB,
        ),
        (
            "Alternance Data Engineer",
            "Projet de fin d'études opportunities are also available elsewhere.",
            OpportunityType.APPRENTICESHIP,
        ),
        (
            "Graduate Data Engineer",
            "We also host stage PFE students.",
            OpportunityType.GRADUATE,
        ),
    ],
)
def test_description_pfe_only_upgrades_an_explicit_internship_title(
    title: str, description: str, expected: OpportunityType,
) -> None:
    assert classify_opportunity(title, description).opportunity_type is expected


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


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("DevOps Engineer, Infrastructure & Security", "Work with machine learning (ML)."),
        (
            "Senior Full-Stack Software Engineer, (Forward Deployed), GPS",
            "Our products use Gen AI and LLMs.",
        ),
        (
            "Solutions Engineer, Enterprise",
            "Support Generative AI, LLMs, agents, and machine learning customers.",
        ),
        (
            "Software Engineer, Identity",
            "Identity products use generative AI, LLMs, and model evaluation.",
        ),
    ],
)
def test_broad_ai_vocabulary_does_not_promote_generic_roles(
    title: str, description: str,
) -> None:
    result = classify_opportunity(title, description)
    assert result.qualification is Qualification.UNCERTAIN
    assert result.primary_domain is Domain.UNKNOWN


@pytest.mark.parametrize(
    ("description", "domain"),
    [
        ("Implement model training and model evaluation.", Domain.MACHINE_LEARNING_AI),
        ("Implement model serving and model deployment.", Domain.MLOPS_ML_PLATFORM),
    ],
)
def test_two_distinct_strong_concepts_promote_generic_software_role(
    description: str, domain: Domain,
) -> None:
    result = classify_opportunity("Software Engineer", description)
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is domain
    assert "strong description concepts" in result.reasons[0]


@pytest.mark.parametrize(
    "description",
    [
        "Prototype retrieval augmented generation (RAG).",
        "Apply machine learning (ML).",
    ],
)
def test_aliases_of_one_concept_cannot_satisfy_description_promotion(description: str) -> None:
    result = classify_opportunity("Software Engineer", description)
    assert result.qualification is Qualification.UNCERTAIN
    assert result.primary_domain is Domain.UNKNOWN


def test_strong_evidence_controls_primary_domain_not_broad_context() -> None:
    result = classify_opportunity(
        "Software Engineer",
        "Build agents while implementing data pipelines, ETL, and data governance.",
    )
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.DATA_ENGINEERING
    assert Domain.GENAI_LLM in result.matched_domains


def test_ai_builder_intern_is_a_narrow_explicit_core_title() -> None:
    result = classify_opportunity("AI Builder Intern", "New graduates may apply.")
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.MACHINE_LEARNING_AI
    assert result.opportunity_type is OpportunityType.INTERNSHIP


# --------------------------------------------------------------------------
# Phase 8A.2: calibration against the real operational corpus.
#
# Every case below came from the read-only audit of the real SQLite corpus,
# where it was UNCERTAIN/UNKNOWN. None of these rules names an employer, a
# source, a location or an opportunity id: they combine a role family with an
# explicit Data/AI domain phrase, and they stay behind the exclusion table.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "qualification", "domain"),
    [
        ("Data Science Intern - GenAI", Qualification.CORE_TARGET, Domain.GENAI_LLM),
        ("Director, AI & Data Science", Qualification.CORE_TARGET, Domain.DATA_SCIENCE),
        ("Data Analytics Manager", Qualification.ADJACENT_TARGET, Domain.BI_ANALYTICS),
        ("Tech Lead - GenAI", Qualification.CORE_TARGET, Domain.GENAI_LLM),
        ("Data Architect", Qualification.CORE_TARGET, Domain.DATA_ENGINEERING),
    ],
)
def test_real_corpus_false_negatives_are_recovered(
    title: str, qualification: Qualification, domain: Domain,
) -> None:
    """These five were UNCERTAIN in the real audit and are now decided.

    "Data Analytics Manager" lands on ADJACENT_TARGET rather than CORE_TARGET
    because its only explicit domain is analytics, and an explicit "Data Analyst"
    role phrase has always been adjacent. The generalized rule is not allowed to
    disagree with the explicit one.
    """
    result = classify_opportunity(title)
    assert result.qualification is qualification
    assert result.primary_domain is domain
    assert result.matched_title_signals


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("Data Science Intern - GenAI", Domain.GENAI_LLM),
        ("Director, AI & Data Science", Domain.DATA_SCIENCE),
        ("Data Analytics Manager", Domain.BI_ANALYTICS),
        ("Tech Lead - GenAI", Domain.GENAI_LLM),
        ("Data Engineering Manager", Domain.DATA_ENGINEERING),
        ("Head of Machine Learning", Domain.MACHINE_LEARNING_AI),
        ("Machine Learning Architect", Domain.MACHINE_LEARNING_AI),
        ("Graduate Data Science Analyst", Domain.DATA_SCIENCE),
    ],
)
def test_the_generalized_rule_is_a_role_family_plus_an_explicit_domain_phrase(
    title: str, domain: Domain,
) -> None:
    result = classify_opportunity(title)
    assert result.qualification in (Qualification.CORE_TARGET, Qualification.ADJACENT_TARGET)
    assert result.primary_domain is domain
    assert "role family + explicit Data/AI domain phrase in title" in result.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Senior Consultant (m/f/d) Data & AI Strategy",
        "Senior Consultant (Strategy & Data/AI Transformation)",
        "Data and AI Strategy Advisor",
    ],
)
def test_data_ai_advisory_and_strategy_roles_are_adjacent_rather_than_core(title: str) -> None:
    """Advising on Data/AI is Data/AI-adjacent work, never Data/AI execution."""
    result = classify_opportunity(title)
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is Domain.OTHER_DATA_AI


@pytest.mark.parametrize(
    "title",
    ["Machine Learning Consultant", "Data Science Strategist", "GenAI Advisory Lead"],
)
def test_the_advisory_cap_holds_even_over_a_core_data_ai_domain(title: str) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert "advisory/strategy role family: Data/AI advisory work is adjacent, not core" in (
        result.reasons
    )


def test_the_advisory_cap_also_applies_to_description_promotion() -> None:
    """The cap is about the role family in the title, not about which rule fired.

    A bare "Consultant" whose description describes the work was CORE_TARGET
    before this calibration. Reading the same advisory marker in both promoting
    rules is the conservative reading and keeps the two consistent.
    """
    result = classify_opportunity(
        "Consultant", "Implement model training and model evaluation pipelines."
    )
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is Domain.MACHINE_LEARNING_AI
    plain = classify_opportunity("Analyst", "Implement data pipelines and ETL jobs.")
    assert plain.qualification is Qualification.CORE_TARGET


def test_an_explicit_core_role_phrase_still_outranks_the_advisory_cap() -> None:
    """"Data Scientist" names the work; "Consultant" only names the arrangement."""
    result = classify_opportunity("Data Scientist Consultant")
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.DATA_SCIENCE


@pytest.mark.parametrize(
    "title",
    [
        "Manager", "Director", "Team Lead", "Intern", "Graduate Trainee",
        "Architect", "Consultant", "Principal", "Head of Delivery",
    ],
)
def test_a_role_family_alone_proves_nothing(title: str) -> None:
    assert classify_opportunity(title).qualification is Qualification.UNCERTAIN


@pytest.mark.parametrize(
    "title",
    [
        "Director, AI",
        "Manager, AI Agents",
        "Intern, LLM Products",
        "Head of Frontier Agents",
        "Technical Lead, RAG Products",
        "Architect, Foundation Models",
    ],
)
def test_broad_ai_vocabulary_is_not_an_explicit_title_domain_phrase(title: str) -> None:
    """Bare "AI", "LLM", "RAG", "agents" and "foundation models" name products.

    The broadened role families are far wider than the technical ones, so they
    may only combine with a phrase that names the field itself. Company and
    product vocabulary keeps evidencing nothing.
    """
    result = classify_opportunity(title)
    assert result.qualification is Qualification.UNCERTAIN
    assert result.primary_domain is Domain.UNKNOWN


@pytest.mark.parametrize(
    "title",
    [
        "Marketing Manager, Data Analytics",
        "Marketing Director, Generative AI",
        "Account Manager, Machine Learning Platform",
        "Product Manager, Data Science",
        "Recruiter, Machine Learning",
        "Program Manager, Data Engineering",
        "Engagement Manager, Artificial Intelligence",
        "Head of Field Marketing, Data Analytics",
    ],
)
def test_explicit_exclusions_still_win_over_a_role_family_and_an_explicit_domain(
    title: str,
) -> None:
    """The exclusion table is evaluated first and was only ever strengthened."""
    result = classify_opportunity(title)
    assert result.qualification is Qualification.OUT_OF_SCOPE
    assert result.primary_domain is Domain.NON_TARGET
    assert result.matched_exclusion_signals


@pytest.mark.parametrize(
    ("title", "qualification"),
    [
        ("Data Engineering Manager", Qualification.CORE_TARGET),
        ("Machine Learning Lead", Qualification.CORE_TARGET),
        ("Data Analytics Lead", Qualification.ADJACENT_TARGET),
        ("Business Intelligence Manager", Qualification.ADJACENT_TARGET),
        ("Data Governance Lead", Qualification.ADJACENT_TARGET),
        ("Data Quality Manager", Qualification.ADJACENT_TARGET),
    ],
)
def test_domain_family_decides_core_versus_adjacent_for_every_promotion(
    title: str, qualification: Qualification,
) -> None:
    assert classify_opportunity(title).qualification is qualification


def test_the_domain_families_partition_the_target_domains_as_the_role_tables_do() -> None:
    """The split is derived from the explicit tables, so it cannot drift from them."""
    from services.collector.qualification.taxonomy import (
        ADJACENT_DOMAINS, ADJACENT_SIGNALS, CORE_DOMAINS, CORE_SIGNALS, DOMAIN_PRECEDENCE,
    )

    assert set(CORE_DOMAINS) == set(CORE_SIGNALS)
    assert set(ADJACENT_DOMAINS) == set(ADJACENT_SIGNALS)
    assert set(CORE_DOMAINS).isdisjoint(ADJACENT_DOMAINS)
    assert set(CORE_DOMAINS) | set(ADJACENT_DOMAINS) == set(DOMAIN_PRECEDENCE)


def test_every_explicit_title_domain_phrase_is_also_a_context_signal() -> None:
    """The explicit table is read as a filter over matched context, never beside it."""
    from services.collector.qualification.taxonomy import (
        DOMAIN_CONTEXT_SIGNALS, EXPLICIT_TITLE_DOMAIN_SIGNALS,
    )

    for domain, signals in EXPLICIT_TITLE_DOMAIN_SIGNALS.items():
        assert set(signals) <= set(DOMAIN_CONTEXT_SIGNALS[domain])


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Research Scientist",
            "Research natural language processing, named entity recognition and "
            "text classification for document understanding.",
        ),
        (
            "Software Engineer",
            "Build computer vision pipelines covering object detection and image "
            "segmentation.",
        ),
    ],
)
def test_concrete_nlp_and_vision_work_can_promote_a_generic_technical_title(
    title: str, description: str,
) -> None:
    """Naming the work is stronger evidence than naming the technology.

    Before this calibration the coarse concept table held only "natural language
    processing" and "computer vision", so an explicitly NLP or CV description
    could never reach two distinct concepts and the role stayed UNCERTAIN. That
    is why the real audit produced no NLP or COMPUTER_VISION primary at all.
    """
    result = classify_opportunity(title, description)
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.MACHINE_LEARNING_AI
    assert len(result.matched_description_signals) >= 1


@pytest.mark.parametrize(
    "description",
    [
        "We sometimes touch natural language processing.",
        "The platform includes computer vision features.",
        "Our customers use object detection.",
    ],
)
def test_one_incidental_nlp_or_vision_sentence_still_promotes_nothing(description: str) -> None:
    result = classify_opportunity("Software Engineer", description)
    assert result.qualification is Qualification.UNCERTAIN
    assert result.primary_domain is Domain.UNKNOWN


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Data Science Intern - GenAI", "Work on generative AI and large language models."),
        ("Data Analytics Manager", "Own dbt models and Power BI dashboards."),
        ("Senior Consultant (m/f/d) Data & AI Strategy", "Advise on the AI roadmap."),
        ("Marketing Manager, AI Products", "Lead model training and model evaluation programs."),
    ],
)
def test_calibrated_results_are_byte_for_byte_reproducible(title: str, description: str) -> None:
    first, second = classify_opportunity(title, description), classify_opportunity(title, description)
    assert first == second
    assert first.reasons == second.reasons
    assert first.matched_title_signals == second.matched_title_signals
    assert first.matched_domains == second.matched_domains


@pytest.mark.parametrize("location", ["Casablanca", "Paris", "Berlin", "Remote", None])
def test_the_calibrated_rules_stay_geography_neutral(location: str | None) -> None:
    baseline = classify_opportunity("Data Analytics Manager")
    assert classify_opportunity("Data Analytics Manager", location=location) == baseline


# --------------------------------------------------------------------------
# Phase 8A.2 second calibration pass. A read-only audit of the real corpus
# after the first pass (394 active, 121 CORE / 16 ADJACENT / 85 OUT_OF_SCOPE /
# 172 UNCERTAIN) exposed a second set of title families whose titles assert
# Data/AI on their own. Each rule below is a generalized phrase or family; not
# one of them names an employer, a source or a full real title.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("title", ["AI Scientist", "Delivery AI Scientist", "Senior Delivery AI Scientist"])
def test_ai_scientist_completes_the_scientist_role_row(title: str) -> None:
    """The table already named a data scientist and an ML scientist.

    "Delivery" and "Senior Delivery" are ordinary title decoration; the rule is
    the complete role phrase "ai scientist" and nothing else.
    """
    result = classify_opportunity(title)
    assert result.qualification is Qualification.CORE_TARGET
    assert result.primary_domain is Domain.MACHINE_LEARNING_AI
    assert "explicit core Data/AI title signal" in result.reasons


@pytest.mark.parametrize(
    "title",
    [
        "Data Consultant", "Senior Data Consultant", "Junior Data Consultant",
        "Data Consultant Intern", "Intern - Data Consultant", "Data Consulting Manager",
        "Manager, AI & Data Consulting", "Senior, AI & Data Consulting",
        "Director, AI & Data Consulting",
    ],
)
def test_data_consulting_is_an_explicit_adjacent_data_ai_family(title: str) -> None:
    """"Data Consultant" is not an unknown generic consultant.

    Two normalized phrases — "data consultant" and "data consulting" — cover the
    whole real family, including both orderings of "AI & Data Consulting", with
    no full title enumerated. It is advisory work about data, so it is adjacent.
    """
    result = classify_opportunity(title)
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is Domain.OTHER_DATA_AI
    assert "explicit adjacent Data/AI title signal" in result.reasons


def test_an_explicit_core_role_phrase_still_wins_over_the_consulting_family() -> None:
    assert classify_opportunity("Data Scientist Consultant").qualification is (
        Qualification.CORE_TARGET
    )


@pytest.mark.parametrize(
    ("title", "qualification", "domain"),
    [
        ("Senior, AI & Data Science", Qualification.CORE_TARGET, Domain.DATA_SCIENCE),
        ("Director, AI & Agentic Engineering", Qualification.CORE_TARGET, Domain.GENAI_LLM),
        (
            "Tech Lead Manager- MLRE, ML Systems",
            Qualification.CORE_TARGET, Domain.MLOPS_ML_PLATFORM,
        ),
        (
            "Senior Full Stack Developer - GenAI Solutions",
            Qualification.CORE_TARGET, Domain.GENAI_LLM,
        ),
        ("AI Transformation Manager", Qualification.ADJACENT_TARGET, Domain.OTHER_DATA_AI),
        ("AI & Automation Intern", Qualification.ADJACENT_TARGET, Domain.OTHER_DATA_AI),
        (
            "Assistant Professor in Data Science and Artificial Intelligence",
            Qualification.CORE_TARGET, Domain.MACHINE_LEARNING_AI,
        ),
    ],
)
def test_second_pass_real_corpus_false_negatives_are_recovered(
    title: str, qualification: Qualification, domain: Domain,
) -> None:
    """Each recovered by a role family beside a phrase that names the field.

    "AI Transformation" and "AI & Automation" are cross-cutting rather than one
    technical sub-domain, so OTHER_DATA_AI makes them adjacent, which is the
    conservative reading.
    """
    result = classify_opportunity(title)
    assert result.qualification is qualification
    assert result.primary_domain is domain
    assert result.matched_title_signals


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("Director, ML Systems", Domain.MLOPS_ML_PLATFORM),
        ("Head of Machine Learning Systems", Domain.MLOPS_ML_PLATFORM),
        ("Manager, Agentic Engineering", Domain.GENAI_LLM),
        ("Software Developer, Data Platform", Domain.DATA_ENGINEERING),
        ("Fullstack Developer, GenAI", Domain.GENAI_LLM),
        ("Professor of Machine Learning", Domain.MACHINE_LEARNING_AI),
        ("Lecturer in Data Science", Domain.DATA_SCIENCE),
        ("Junior, Data Analytics", Domain.BI_ANALYTICS),
    ],
)
def test_the_new_families_generalize_beyond_the_real_titles_that_motivated_them(
    title: str, domain: Domain,
) -> None:
    result = classify_opportunity(title)
    assert result.qualification in (Qualification.CORE_TARGET, Qualification.ADJACENT_TARGET)
    assert result.primary_domain is domain


@pytest.mark.parametrize(
    "title",
    [
        "Full Stack Developer", "Software Developer", "Fullstack Developer",
        "Assistant Professor of Economics", "Professor of Economics",
        "Lecturer in Marketing", "Automation Manager", "Digital Transformation Manager",
        "Senior Associate", "Junior Analyst", "Systems Engineer",
        "Engineering Manager, Infrastructure", "GTM Architect", "Security Engineer",
        "Product Designer", "Field Engineer, Public Sector", "Deployment Strategist",
        "Senior Full-Stack Software Engineer, (Forward Deployed), GPS",
    ],
)
def test_the_new_families_prove_nothing_without_an_explicit_domain_phrase(title: str) -> None:
    """Each family is a role shape; the domain phrase is what makes it evidence.

    A developer, a professor, a seniority marker and a manager all stay unknown
    on their own, and broad "systems", "automation" and "transformation" wording
    is not a Data/AI phrase.
    """
    result = classify_opportunity(title)
    assert result.qualification is Qualification.UNCERTAIN
    assert result.primary_domain is Domain.UNKNOWN


@pytest.mark.parametrize(
    "title",
    [
        "Senior Product Manager, Data Science",
        "Senior Marketing Manager, Generative AI",
        "Senior Account Executive, AI",
        "Junior Recruiter, Machine Learning",
        "Professor of Practice, Product Management, AI",
        "Data Center Manager",
    ],
)
def test_exclusions_still_win_over_every_second_pass_family(title: str) -> None:
    """A seniority, academic or developer marker is not a route around the table.

    "Data Center Manager" completes the data-centre facilities family the table
    already held ("data center technician", "data center operations manager"):
    it is an infrastructure role, and the exclusion is only ever strengthened.
    """
    result = classify_opportunity(title)
    assert result.qualification is Qualification.OUT_OF_SCOPE
    assert result.primary_domain is Domain.NON_TARGET
    assert result.matched_exclusion_signals


@pytest.mark.parametrize(
    "title",
    ["Marketing Director, AI", "Director, AI", "Senior, AI", "Professor of AI Ethics Policy"],
)
def test_the_reverse_data_ai_ordering_never_makes_bare_ai_authoritative(title: str) -> None:
    """"ai data" and "ai and data" are compounds; "ai" alone stays powerless."""
    result = classify_opportunity(title)
    assert result.qualification in (Qualification.UNCERTAIN, Qualification.OUT_OF_SCOPE)
    assert result.primary_domain is not Domain.OTHER_DATA_AI


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("Manager, AI & Data Strategy", Domain.OTHER_DATA_AI),
        ("Manager, Data & AI Strategy", Domain.OTHER_DATA_AI),
        ("Head of AI and Data", Domain.OTHER_DATA_AI),
        ("Head of Data and AI", Domain.OTHER_DATA_AI),
    ],
)
def test_both_orderings_of_the_data_ai_compound_are_read_the_same_way(
    title: str, domain: Domain,
) -> None:
    result = classify_opportunity(title)
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is domain


def test_the_second_pass_families_are_centralized_and_ordered() -> None:
    """One list, so a future calibration adds a family rather than a concatenation."""
    from services.collector.qualification.taxonomy import (
        ACADEMIC_ROLE_SIGNALS, ARCHITECT_ROLE_SIGNALS, EARLY_CAREER_ROLE_SIGNALS,
        EXPLICIT_DOMAIN_ROLE_FAMILIES, LEADERSHIP_ROLE_SIGNALS, SENIORITY_ROLE_SIGNALS,
    )

    assert EXPLICIT_DOMAIN_ROLE_FAMILIES == (
        LEADERSHIP_ROLE_SIGNALS, EARLY_CAREER_ROLE_SIGNALS, ARCHITECT_ROLE_SIGNALS,
        SENIORITY_ROLE_SIGNALS, ACADEMIC_ROLE_SIGNALS,
    )
    assert "senior" not in LEADERSHIP_ROLE_SIGNALS
    assert classify_opportunity("Senior, AI & Data Science") == classify_opportunity(
        "Senior, AI & Data Science"
    )


def test_no_signal_appears_in_two_explicit_domain_role_families() -> None:
    """The classifier concatenates every family's matches into one evidence tuple.

    A signal listed in two families would therefore be reported twice for the
    same title. "junior" was, until it was left to EARLY_CAREER_ROLE_SIGNALS,
    where it belongs semantically as well as functionally.
    """
    from services.collector.qualification.taxonomy import (
        EARLY_CAREER_ROLE_SIGNALS, EXPLICIT_DOMAIN_ROLE_FAMILIES, SENIORITY_ROLE_SIGNALS,
    )

    signals = [signal for family in EXPLICIT_DOMAIN_ROLE_FAMILIES for signal in family]
    assert len(signals) == len(set(signals))
    assert "junior" in EARLY_CAREER_ROLE_SIGNALS
    assert "junior" not in SENIORITY_ROLE_SIGNALS


def test_an_early_career_field_track_title_reports_its_marker_once() -> None:
    result = classify_opportunity("Junior, Data Analytics")
    assert result.qualification is Qualification.ADJACENT_TARGET
    assert result.primary_domain is Domain.BI_ANALYTICS
    assert result.matched_title_signals.count("junior") == 1
    senior = classify_opportunity("Senior, AI & Data Science")
    assert senior.qualification is Qualification.CORE_TARGET
    assert senior.primary_domain is Domain.DATA_SCIENCE
    assert senior.matched_title_signals.count("senior") == 1
