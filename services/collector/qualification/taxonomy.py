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

# Whether a promoted domain is core or adjacent is decided once, here, so every
# generalized rule agrees with the explicit role tables below: the domains that
# CORE_SIGNALS names are core, the domains that ADJACENT_SIGNALS names are
# adjacent. A generalized rule can therefore never say CORE_TARGET about an
# analytics or governance domain that an explicit role phrase calls adjacent.
CORE_DOMAINS = (
    Domain.DATA_ENGINEERING, Domain.DATA_SCIENCE, Domain.MACHINE_LEARNING_AI,
    Domain.GENAI_LLM, Domain.MLOPS_ML_PLATFORM,
)
ADJACENT_DOMAINS = (
    Domain.BI_ANALYTICS, Domain.DATA_QUALITY_GOVERNANCE, Domain.OTHER_DATA_AI,
)

# Complete role phrases remain the highest-confidence target evidence.
CORE_SIGNALS = {
    Domain.DATA_ENGINEERING: (
        "data engineer", "analytics engineer", "data platform engineer",
        # "data architect" is an established, unambiguous data role name, and it
        # is listed for the same reason "data engineer" is: the bare word "data"
        # can never be a domain phrase, so the complete role phrase is the only
        # honest way to recognize it.
        "data architect", "big data architect",
        "ingenieur data", "ingenieure data",
    ),
    Domain.DATA_SCIENCE: ("data scientist",),
    Domain.MACHINE_LEARNING_AI: (
        "machine learning engineer", "ml engineer", "ai engineer", "ai builder",
        # "ai scientist" completes the scientist row: the table already names a
        # data scientist and an ML scientist, and the phrase is a job title in
        # its own right rather than a company's product vocabulary.
        "ai scientist", "artificial intelligence scientist",
        "artificial intelligence engineer", "software engineer machine learning",
        "software engineer ai", "applied scientist machine learning",
        "applied scientist ai", "ml scientist", "research scientist machine learning",
        "research scientist ai", "nlp engineer", "computer vision engineer",
        "deep learning engineer", "ingenieur intelligence artificielle",
        "ingenieure intelligence artificielle", "ingenieur machine learning",
        "ingenieure machine learning",
        # The French abbreviation of "intelligence artificielle", and only ever
        # inside a complete role phrase. The bare "ia" is never a signal: it is
        # a two-letter token that appears in ordinary prose and in product
        # wording, so a posting that merely mentions "IA" stays UNCERTAIN —
        # exactly as "AI" alone does on the English side, where the table lists
        # "ai engineer" and "software engineer ai" rather than "ai". These
        # phrases mirror, one for one, the English rows already present above;
        # `normalize_text` folds accents and punctuation, so "Ingénieur IA" and
        # "Software Engineer & IA" reach the table in this spelling.
        "ingenieur ia", "ingenieure ia",
        "developpeur ia", "developpeuse ia",
        "software engineer ia",
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
    "software engineer", "software developer", "full stack developer", "fullstack developer",
    "infrastructure software engineer", "infrastructure engineer",
    "research engineer", "systems research engineer", "systems engineer",
    "solutions engineer", "research scientist", "forward deployed engineer",
    "forward deployed engineering", "agents engineer", "director of engineering", "fellow",
)

POSTDOC_ROLE_SIGNALS = ("postdoc", "post doc", "postdoctoral", "post doctoral")

# Phase 8A.2 role families. Each tuple names the *shape* of a job and never its
# domain, so a family alone proves nothing: it becomes evidence only next to an
# explicit Data/AI domain phrase in EXPLICIT_TITLE_DOMAIN_SIGNALS, and only
# after NON_TARGET_ROLE_SIGNALS has had its say. That ordering is what keeps
# "Marketing Manager, AI Products" and "Product Manager, Gen AI" out while
# letting "Data Analytics Manager" and "Tech Lead - GenAI" in.
LEADERSHIP_ROLE_SIGNALS = (
    "head of", "director", "vp", "vice president", "chief", "manager",
    "lead", "tech lead", "technical lead", "team lead", "principal", "staff",
)

EARLY_CAREER_ROLE_SIGNALS = (
    "intern", "internship", "stage", "stagiaire", "trainee", "apprentice",
    "apprenticeship", "alternance", "alternant", "alternante", "graduate",
    "working student", "junior",
)

ARCHITECT_ROLE_SIGNALS = ("architect", "architecture")

# The pure seniority marker, carrying no role content at all. A field-track
# title can be nothing but a level and the field name ("Senior, AI & Data
# Science"), and this is the only way to read one. Kept apart from
# LEADERSHIP_ROLE_SIGNALS because a senior individual contributor is not a
# leader, and dependent on an explicit domain phrase exactly like every other
# family here: "Senior Product Manager, Data Science" is still excluded by its
# job family, and "Senior Software Engineer, Full-Stack" still evidences
# nothing. "junior" is deliberately absent: it already belongs to
# EARLY_CAREER_ROLE_SIGNALS, and every signal in EXPLICIT_DOMAIN_ROLE_FAMILIES
# must appear in exactly one family so a title never reports it twice.
SENIORITY_ROLE_SIGNALS = ("senior",)

# Academic role families. Phase 8 classifies what an opportunity is about, not
# whether it suits anyone, so a professorship in Data Science is a Data/AI
# opportunity. "assistant professor" and "associate professor" both contain
# "professor", so the bare noun covers the whole rank ladder; a professorship in
# any other field matches no domain phrase and stays UNCERTAIN.
ACADEMIC_ROLE_SIGNALS = ("professor", "lecturer")

# Advisory markers never promote a title on their own either, and they *cap* the
# outcome at ADJACENT_TARGET: advising on Data/AI is Data/AI-adjacent work, not
# Data/AI execution. An explicit core role phrase still wins over the cap.
ADVISORY_ROLE_SIGNALS = (
    "consultant", "consulting", "advisor", "adviser", "advisory", "strategist",
    "strategy",
)

# Every family that may combine with an EXPLICIT_TITLE_DOMAIN_SIGNALS phrase,
# in the order their matches are reported. Centralized so the classifier reads
# one list rather than a concatenation that grows with each calibration.
EXPLICIT_DOMAIN_ROLE_FAMILIES = (
    LEADERSHIP_ROLE_SIGNALS,
    EARLY_CAREER_ROLE_SIGNALS,
    ARCHITECT_ROLE_SIGNALS,
    SENIORITY_ROLE_SIGNALS,
    ACADEMIC_ROLE_SIGNALS,
)

DOMAIN_CONTEXT_SIGNALS = {
    Domain.DATA_ENGINEERING: (
        "data engineering", "data pipeline", "data pipelines", "etl", "elt",
        "data warehouse", "data lake", "lakehouse", "data platform", "data infrastructure",
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
        "agentic systems", "agentic engineering", "frontier agents", "agents",
    ),
    Domain.MLOPS_ML_PLATFORM: (
        "mlops", "ml platform", "machine learning platform", "model serving", "serving platform",
        "sandbox platform", "inference platform", "model deployment", "model monitoring",
        "ml infrastructure", "machine learning infrastructure", "ai infrastructure",
        "frontier ai infrastructure", "ml systems", "machine learning systems", "feature store",
    ),
    Domain.BI_ANALYTICS: (
        "data analytics", "business intelligence", "power bi", "tableau", "dashboarding",
        "analytics reporting", "data visualization",
    ),
    Domain.DATA_QUALITY_GOVERNANCE: (
        "data quality", "data governance", "data lineage", "metadata management",
    ),
    # "Data & AI", "Data/AI" and "Data, AI" all normalize to "data ai", and the
    # real corpus writes the pair in both orders. Each phrase names the field as
    # a whole rather than one sub-domain, which is exactly what OTHER_DATA_AI is
    # for. Every entry is a *compound*: bare "ai" and bare "data" stay powerless,
    # which is what keeps "Marketing Director, AI" and "Automation Manager" out.
    Domain.OTHER_DATA_AI: (
        "data ai", "data and ai", "ai data", "ai and data",
        "ai transformation", "ai automation",
    ),
}

# The high-specificity subset of DOMAIN_CONTEXT_SIGNALS: phrases that name the
# Data/AI field itself rather than describing a product built with it. Only
# these may combine with the broadened Phase 8A.2 role families, because those
# families are far wider than TECHNICAL_ROLE_SIGNALS. Vocabulary every AI
# company applies to every job -- bare "ai", "ml", "llm", "rag", "agents",
# "foundation models", "ai infrastructure" as a product noun -- is absent on
# purpose, so "Marketing Director, AI" evidences nothing at all.
# Every phrase here must also be a DOMAIN_CONTEXT_SIGNAL of the same domain;
# the classifier reads this table as a filter over already-matched context.
EXPLICIT_TITLE_DOMAIN_SIGNALS = {
    Domain.DATA_ENGINEERING: (
        "data engineering", "data pipeline", "data pipelines", "data warehouse",
        "data lake", "lakehouse", "data platform", "data infrastructure",
    ),
    Domain.DATA_SCIENCE: ("data science", "statistical modeling", "predictive modeling"),
    Domain.MACHINE_LEARNING_AI: (
        "machine learning", "artificial intelligence", "deep learning",
        "computer vision", "natural language processing", "nlp",
        "applied ai", "enterprise ai", "physical ai", "ai ml",
    ),
    Domain.GENAI_LLM: (
        "generative ai", "genai", "gen ai",
        "large language model", "large language models",
        # Naming the engineering discipline, unlike the bare product nouns
        # "agent", "agents" and "ai agents", which stay out of this table.
        "agentic engineering",
    ),
    Domain.MLOPS_ML_PLATFORM: (
        "mlops", "ml platform", "machine learning platform",
        "ml infrastructure", "machine learning infrastructure",
        # "ML Systems" names the discipline as specifically as "ML Platform"
        # does, and it was already trusted next to the technical families; broad
        # "systems" and "systems engineering" remain powerless.
        "ml systems", "machine learning systems",
    ),
    Domain.BI_ANALYTICS: ("data analytics", "business intelligence"),
    Domain.DATA_QUALITY_GOVERNANCE: ("data quality", "data governance"),
    Domain.OTHER_DATA_AI: (
        "data ai", "data and ai", "ai data", "ai and data",
        "ai transformation", "ai automation",
    ),
}

# Description-only promotion uses concepts rather than raw phrase counts.
# Aliases within one concept are one piece of evidence, and broad domain labels
# such as "machine learning", "GenAI", or "LLM" deliberately do not appear.
STRONG_DESCRIPTION_CONCEPTS = {
    Domain.DATA_ENGINEERING: {
        "data_pipelines": ("data pipeline", "data pipelines"),
        "etl_elt": ("etl", "elt"),
        "analytical_storage": ("data warehouse", "data lake", "lakehouse"),
        "spark": ("spark",),
        "dbt": ("dbt",),
        "airflow": ("airflow",),
        "kafka": ("kafka",),
        "data_orchestration": ("data orchestration",),
        "streaming_pipelines": ("streaming pipelines",),
    },
    Domain.DATA_SCIENCE: {
        "statistical_modeling": ("statistical modeling",),
        "predictive_modeling": ("predictive modeling",),
        "experimentation": ("experimentation",),
        "statistical_analysis": ("statistical analysis",),
    },
    Domain.MACHINE_LEARNING_AI: {
        "model_training": ("model training",),
        "model_evaluation": ("model evaluation",),
        "model_development": ("model development",),
        "deep_learning": ("deep learning",),
        # Concrete NLP and computer-vision work. A phrase like "object detection"
        # or "named entity recognition" describes what the role builds; it is not
        # the company vocabulary ("AI", "ML", "LLM") this table refuses. Without
        # these, an explicitly NLP or CV description could never clear the
        # two-concept bar and the role stayed UNCERTAIN.
        "computer_vision": ("computer vision",),
        "nlp": ("natural language processing", "nlp"),
        "text_classification": ("text classification",),
        "named_entity_recognition": ("named entity recognition",),
        "information_extraction": ("information extraction",),
        "sentiment_analysis": ("sentiment analysis",),
        "machine_translation": ("machine translation",),
        "object_detection": ("object detection",),
        "image_recognition": ("image recognition",),
        "image_classification": ("image classification",),
        "image_segmentation": ("image segmentation", "semantic segmentation"),
    },
    Domain.GENAI_LLM: {
        "retrieval_augmented_generation": ("retrieval augmented generation", "rag"),
        "agentic_systems": ("agentic systems",),
    },
    Domain.MLOPS_ML_PLATFORM: {
        "model_serving": ("model serving",),
        "model_deployment": ("model deployment",),
        "model_monitoring": ("model monitoring",),
        "feature_store": ("feature store",),
        "ml_platform": ("ml platform", "machine learning platform"),
        "ml_infrastructure": ("ml infrastructure", "machine learning infrastructure"),
    },
    Domain.DATA_QUALITY_GOVERNANCE: {
        "data_quality": ("data quality",),
        "data_governance": ("data governance",),
        "data_lineage": ("data lineage",),
        "metadata_management": ("metadata management",),
    },
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
        # A title that says "Data Consultant" or "Data Consulting" is not an
        # unknown generic consultant: it names Data advisory work outright. The
        # two phrases cover the whole real family on their own -- senior, junior
        # and intern variants, "Data Consulting Manager", and both orderings of
        # "AI & Data Consulting" -- without enumerating a single full title.
        "data consultant", "data consulting",
    ),
}

# These are title role families, never description keywords. They take
# precedence even when a title also contains AI, GenAI, or data-platform terms.
NON_TARGET_ROLE_SIGNALS = (
    "data center technician", "data centre technician", "data center operations manager",
    "data centre operations manager", "data center manager", "data centre manager", "account executive", "sales manager",
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
    # Commercial, marketing and people job families that the Phase 8A.2
    # leadership markers would otherwise expose next to an explicit Data/AI
    # phrase. Exclusion precedence is only ever strengthened, never relaxed.
    "account manager", "partner manager", "partnerships manager", "community manager",
    "brand manager", "marketing director", "marketing lead", "head of marketing",
    "sales director", "head of sales", "head of people", "people operations",
    "customer support",
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
