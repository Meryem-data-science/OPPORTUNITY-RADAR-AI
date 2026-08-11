"""Closed taxonomies and centralized deterministic qualification signals."""

from enum import StrEnum


class Qualification(StrEnum):
    CORE_TARGET = "CORE_TARGET"
    ADJACENT_TARGET = "ADJACENT_TARGET"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    UNCERTAIN = "UNCERTAIN"


class Domain(StrEnum):
    DATA_ENGINEERING = "DATA_ENGINEERING"
    DATA_SCIENCE = "DATA_SCIENCE"
    MACHINE_LEARNING_AI = "MACHINE_LEARNING_AI"
    GENAI_LLM = "GENAI_LLM"
    MLOPS_ML_PLATFORM = "MLOPS_ML_PLATFORM"
    BI_ANALYTICS = "BI_ANALYTICS"
    DATA_QUALITY_GOVERNANCE = "DATA_QUALITY_GOVERNANCE"
    OTHER_DATA_AI = "OTHER_DATA_AI"
    NON_TARGET = "NON_TARGET"
    UNKNOWN = "UNKNOWN"


class OpportunityType(StrEnum):
    PFE = "PFE"
    INTERNSHIP = "INTERNSHIP"
    GRADUATE = "GRADUATE"
    APPRENTICESHIP = "APPRENTICESHIP"
    JOB = "JOB"
    UNKNOWN = "UNKNOWN"


class EmploymentType(StrEnum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    CONTRACT = "CONTRACT"
    TEMPORARY = "TEMPORARY"
    UNKNOWN = "UNKNOWN"


class ListingQuality(StrEnum):
    NORMAL_LISTING = "NORMAL_LISTING"
    POSSIBLE_NON_JOB_PAGE = "POSSIBLE_NON_JOB_PAGE"
    INSUFFICIENT_CONTENT = "INSUFFICIENT_CONTENT"


# Earlier entries win primary-domain ties. Every signal is a normalized phrase.
DOMAIN_PRECEDENCE = (
    Domain.GENAI_LLM,
    Domain.MLOPS_ML_PLATFORM,
    Domain.MACHINE_LEARNING_AI,
    Domain.DATA_SCIENCE,
    Domain.DATA_ENGINEERING,
    Domain.DATA_QUALITY_GOVERNANCE,
    Domain.BI_ANALYTICS,
    Domain.OTHER_DATA_AI,
)

# Complete role phrases remain the highest-confidence target evidence.
CORE_SIGNALS = {
    Domain.DATA_ENGINEERING: (
        "data engineer", "analytics engineer", "data platform engineer",
        "ingenieur data", "ingenieure data",
    ),
    Domain.DATA_SCIENCE: ("data scientist",),
    Domain.MACHINE_LEARNING_AI: (
        "machine learning engineer", "ml engineer", "ai engineer",
        "artificial intelligence engineer", "software engineer machine learning",
        "software engineer ai", "applied scientist machine learning",
        "applied scientist ai", "ml scientist", "research scientist machine learning",
        "research scientist ai", "nlp engineer", "computer vision engineer",
        "deep learning engineer", "ingenieur intelligence artificielle",
        "ingenieure intelligence artificielle", "ingenieur machine learning",
        "ingenieure machine learning",
    ),
    Domain.GENAI_LLM: (
        "generative ai engineer", "genai engineer", "llm engineer",
        "large language model engineer",
    ),
    Domain.MLOPS_ML_PLATFORM: (
        "mlops engineer", "ml platform engineer", "machine learning platform engineer",
    ),
}

# Structural title matching requires a role family plus domain context. This
# prevents AI, agent, or platform product wording from being sufficient alone.
TECHNICAL_ROLE_SIGNALS = (
    "software engineer", "infrastructure software engineer", "infrastructure engineer",
    "research engineer", "systems research engineer", "systems engineer",
    "solutions engineer", "research scientist", "forward deployed engineer",
    "forward deployed engineering", "agents engineer", "director of engineering", "fellow",
)

POSTDOC_ROLE_SIGNALS = ("postdoc", "post doc", "postdoctoral", "post doctoral")

DOMAIN_CONTEXT_SIGNALS = {
    Domain.DATA_ENGINEERING: (
        "data pipeline", "data pipelines", "etl", "elt", "data warehouse",
        "data lake", "lakehouse", "data platform", "data infrastructure",
        "spark", "dbt", "airflow", "kafka", "data orchestration", "streaming pipelines",
    ),
    Domain.DATA_SCIENCE: (
        "data science", "statistical modeling", "predictive modeling",
        "experimentation", "statistical analysis",
    ),
    Domain.MACHINE_LEARNING_AI: (
        "machine learning", "ml", "artificial intelligence", "deep learning",
        "computer vision", "natural language processing", "nlp", "model training",
        "model evaluation", "model development", "ai ml", "applied ai",
        "enterprise ai", "physical ai",
    ),
    Domain.GENAI_LLM: (
        "generative ai", "genai", "gen ai", "large language model",
        "large language models", "llm", "llms", "retrieval augmented generation",
        "rag", "foundation model", "foundation models", "ai agents",
        "agentic systems", "frontier agents", "agents",
    ),
    Domain.MLOPS_ML_PLATFORM: (
        "ml platform", "machine learning platform", "model serving", "serving platform",
        "sandbox platform", "inference platform", "model deployment", "model monitoring",
        "ml infrastructure", "machine learning infrastructure", "ai infrastructure",
        "frontier ai infrastructure", "ml systems", "machine learning systems", "feature store",
    ),
    Domain.BI_ANALYTICS: (
        "business intelligence", "power bi", "tableau", "dashboarding",
        "analytics reporting", "data visualization",
    ),
    Domain.DATA_QUALITY_GOVERNANCE: (
        "data quality", "data governance", "data lineage", "metadata management",
    ),
}

ADJACENT_SIGNALS = {
    Domain.BI_ANALYTICS: (
        "data analyst", "bi analyst", "business intelligence developer", "bi developer",
        "analytics analyst", "reporting analyst", "decision analyst",
        "analyste de donnees", "developpeur bi", "developpeuse bi",
    ),
    Domain.DATA_QUALITY_GOVERNANCE: (
        "data quality engineer", "data quality analyst", "data governance analyst",
        "data governance engineer", "data management analyst", "data management engineer",
    ),
    Domain.OTHER_DATA_AI: (
        "ai advisory consultant", "ai strategy consultant", "ai advisory principal",
    ),
}

# These are title role families, never description keywords. They take
# precedence even when a title also contains AI, GenAI, or data-platform terms.
NON_TARGET_ROLE_SIGNALS = (
    "data center technician", "data centre technician", "data center operations manager",
    "data centre operations manager", "account executive", "sales manager",
    "sales representative", "sales enablement", "business development", "recruiter",
    "recruiting coordinator", "talent acquisition", "human resources", "hr manager",
    "marketing manager", "content manager", "customer success", "legal counsel",
    "general counsel", "lead counsel", "legal fellow", "finance manager", "finance systems",
    "finance fellow",
    "financial accountant", "corporate accountant", "accountant", "payroll manager",
    "office administrator", "operations associate", "operations program manager",
    "operations manager", "product manager", "product management", "product strategy",
    "program manager", "chief of staff", "communications manager",
    "communications senior manager", "field marketing", "marketing and events",
    "strategic projects lead", "technical writer", "executive assistant",
    "support specialist", "support team lead", "proposals manager", "engagement manager",
    "engagement management", "store manager", "retail associate", "graphic designer",
    "field technician", "hardware technician",
)

GENERIC_TECHNICAL_TITLES = (
    "software engineer", "research scientist", "analyst", "consultant", "engineer",
    "ingenieur", "ingenieure",
)

PFE_SIGNALS = ("pfe", "projet de fin d etudes", "projet fin d etudes")
INTERNSHIP_SIGNALS = ("internship", "intern", "stage", "stagiaire", "trainee")
GRADUATE_SIGNALS = (
    "graduate program", "graduate programme", "new graduate", "recent graduate program",
    "graduate scheme", "graduate data engineer",
)
APPRENTICESHIP_SIGNALS = (
    "apprenticeship", "apprentice", "alternance", "alternant", "alternante",
)
DESCRIPTION_PFE_SIGNALS = ("projet de fin d etudes", "projet fin d etudes", "stage pfe")

EMPLOYMENT_SIGNALS = {
    EmploymentType.FULL_TIME: ("full time", "temps plein"),
    EmploymentType.PART_TIME: ("part time", "temps partiel"),
    EmploymentType.CONTRACT: (
        "contract", "contractor", "freelance", "fixed term contract", "cdd",
    ),
    EmploymentType.TEMPORARY: ("temporary", "temporaire", "interim"),
}

GENERIC_CAREERS_TITLES = ("careers", "career opportunities", "carrieres", "rejoignez nous")
GENERIC_JOBS_TITLES = ("jobs", "job openings", "open positions", "offres d emploi")
