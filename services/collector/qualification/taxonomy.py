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


# Earlier entries win primary-domain ties. A signal is a normalized phrase.
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

ADJACENT_SIGNALS = {
    Domain.BI_ANALYTICS: (
        "data analyst", "bi analyst", "business intelligence developer",
        "bi developer", "analytics analyst", "reporting analyst",
        "decision analyst", "analyste de donnees", "developpeur bi",
        "developpeuse bi",
    ),
    Domain.DATA_QUALITY_GOVERNANCE: (
        "data quality engineer", "data quality analyst", "data governance analyst",
        "data governance engineer", "data management analyst",
        "data management engineer",
    ),
}

EXCLUSION_SIGNALS = (
    "data center technician", "data centre technician", "data center operations manager",
    "data centre operations manager", "account executive", "sales manager", "sales representative",
    "business development", "recruiter", "talent acquisition", "human resources",
    "marketing manager", "content manager", "customer success", "legal counsel",
    "finance manager", "financial accountant", "accountant", "office administrator",
    "operations manager", "store manager", "retail associate", "graphic designer",
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
APPRENTICESHIP_SIGNALS = ("apprenticeship", "apprentice", "alternance", "alternant", "alternante")

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
