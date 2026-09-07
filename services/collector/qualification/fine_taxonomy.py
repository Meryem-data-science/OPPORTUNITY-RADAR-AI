"""Closed fine Data/AI taxonomy and its centralized deterministic signals.

This taxonomy answers a different question from :mod:`.taxonomy`. The coarse
``Qualification``/``Domain`` pair answers *is this opportunity Data/AI relevant*;
the categories below answer *which Data/AI sub-domain does it belong to*, and
only for opportunities the coarse classifier already qualified. The two
vocabularies are deliberately separate: ``Domain`` is consumed by persistence
and matching, and nothing here is allowed to change that contract.
"""

from enum import StrEnum


class FineCategory(StrEnum):
    DATA_SCIENCE = "DATA_SCIENCE"
    DATA_ANALYTICS = "DATA_ANALYTICS"
    DATA_ENGINEERING = "DATA_ENGINEERING"
    MACHINE_LEARNING = "MACHINE_LEARNING"
    ARTIFICIAL_INTELLIGENCE = "ARTIFICIAL_INTELLIGENCE"
    GENERATIVE_AI = "GENERATIVE_AI"
    NLP = "NLP"
    COMPUTER_VISION = "COMPUTER_VISION"
    BUSINESS_INTELLIGENCE = "BUSINESS_INTELLIGENCE"
    MLOPS = "MLOPS"
    #: Demonstrably Data/AI, but no supported fine category is evidenced.
    #: This is never "we do not know": an unqualified or uncertain opportunity
    #: receives no fine category at all rather than ``OTHER``.
    OTHER = "OTHER"


class EvidenceField(StrEnum):
    """Which classifier input produced a piece of fine evidence."""

    TITLE = "TITLE"
    DESCRIPTION = "DESCRIPTION"


class EvidenceKind(StrEnum):
    """How strong the matched signal is, in a closed vocabulary."""

    #: A complete role phrase, the highest-confidence fine evidence.
    ROLE_PHRASE = "ROLE_PHRASE"
    #: A sub-domain context phrase read from the title.
    CONTEXT_PHRASE = "CONTEXT_PHRASE"
    #: A concrete technical concept read from the description.
    CONCRETE_CONCEPT = "CONCRETE_CONCEPT"


# Earlier entries win the primary-category choice. Specific sub-domains are
# deliberately ordered above the broad ones they specialize, so a Generative AI
# or Computer Vision role is not flattened into MACHINE_LEARNING, and ML is
# ordered above ARTIFICIAL_INTELLIGENCE because "AI" is the vaguer of the two.
# DATA_ENGINEERING sits above BUSINESS_INTELLIGENCE and DATA_ANALYTICS because
# an analytics engineer is a pipeline role, and BUSINESS_INTELLIGENCE sits above
# DATA_ANALYTICS so an analyst title with explicit BI tooling is read as BI
# rather than as generic analytics. OTHER is last and is never evidenced: it is
# assigned only when a qualified opportunity produced no evidence at all.
FINE_CATEGORY_PRECEDENCE = (
    FineCategory.GENERATIVE_AI,
    FineCategory.NLP,
    FineCategory.COMPUTER_VISION,
    FineCategory.MLOPS,
    FineCategory.MACHINE_LEARNING,
    FineCategory.ARTIFICIAL_INTELLIGENCE,
    FineCategory.DATA_SCIENCE,
    FineCategory.DATA_ENGINEERING,
    FineCategory.BUSINESS_INTELLIGENCE,
    FineCategory.DATA_ANALYTICS,
    FineCategory.OTHER,
)

# Complete role phrases. A role phrase names the work the person will do, which
# is why it may be read from the title only. Bare "ai" and bare "ml" never
# appear anywhere in this module: they collide with ordinary product, sales and
# recruiting wording, and the coarse classifier already refuses them.
FINE_ROLE_SIGNALS: dict[FineCategory, tuple[str, ...]] = {
    FineCategory.DATA_SCIENCE: (
        "data scientist", "applied scientist data science", "scientifique des donnees",
    ),
    FineCategory.DATA_ANALYTICS: (
        "data analyst", "analytics analyst", "product analyst", "analyste de donnees",
    ),
    FineCategory.DATA_ENGINEERING: (
        "data engineer", "analytics engineer", "data platform engineer",
        "big data engineer", "etl developer", "etl engineer",
        "ingenieur data", "ingenieure data",
    ),
    FineCategory.MACHINE_LEARNING: (
        "machine learning engineer", "ml engineer", "machine learning scientist",
        "ml scientist", "applied scientist machine learning",
        "research scientist machine learning", "deep learning engineer",
        "software engineer machine learning", "ingenieur machine learning",
        "ingenieure machine learning",
    ),
    FineCategory.ARTIFICIAL_INTELLIGENCE: (
        "ai engineer", "artificial intelligence engineer", "ai builder",
        "applied scientist ai", "research scientist ai", "software engineer ai",
        "ingenieur intelligence artificielle", "ingenieure intelligence artificielle",
    ),
    FineCategory.GENERATIVE_AI: (
        "generative ai engineer", "genai engineer", "gen ai engineer", "llm engineer",
        "large language model engineer",
    ),
    FineCategory.NLP: (
        "nlp engineer", "nlp scientist", "natural language processing engineer",
        "computational linguist",
    ),
    FineCategory.COMPUTER_VISION: (
        "computer vision engineer", "computer vision scientist",
        "computer vision researcher",
    ),
    FineCategory.BUSINESS_INTELLIGENCE: (
        "bi analyst", "bi developer", "bi engineer", "business intelligence analyst",
        "business intelligence developer", "business intelligence engineer",
        "reporting analyst", "developpeur bi", "developpeuse bi",
    ),
    FineCategory.MLOPS: (
        "mlops engineer", "ml platform engineer", "machine learning platform engineer",
        "ml infrastructure engineer", "ml systems engineer",
        "machine learning operations engineer",
    ),
}

# Sub-domain context phrases. These qualify a role that the role table above did
# not name outright ("Senior Software Engineer, Data Platform"). "ai ml" appears
# under two categories on purpose: the phrase evidences both, and precedence
# then makes MACHINE_LEARNING primary and ARTIFICIAL_INTELLIGENCE secondary.
FINE_CONTEXT_SIGNALS: dict[FineCategory, tuple[str, ...]] = {
    FineCategory.DATA_SCIENCE: (
        "data science", "statistical modeling", "predictive modeling",
        "statistical analysis", "experimentation",
    ),
    FineCategory.DATA_ANALYTICS: (
        "data analytics", "analytical reporting", "analytics reporting",
        "product analytics",
    ),
    FineCategory.DATA_ENGINEERING: (
        "data pipeline", "data pipelines", "etl", "elt", "data warehouse",
        "data lake", "lakehouse", "data platform", "data infrastructure",
        "data orchestration", "streaming pipelines", "spark", "dbt", "airflow", "kafka",
    ),
    FineCategory.MACHINE_LEARNING: (
        "machine learning", "deep learning", "model training", "model evaluation",
        "model development", "reinforcement learning", "ai ml",
    ),
    FineCategory.ARTIFICIAL_INTELLIGENCE: (
        "artificial intelligence", "applied ai", "enterprise ai", "physical ai",
        "intelligence artificielle", "ai ml",
    ),
    FineCategory.GENERATIVE_AI: (
        "generative ai", "genai", "gen ai", "large language model",
        "large language models", "llm", "llms", "retrieval augmented generation",
        "rag", "foundation model", "foundation models", "ai agents", "agentic ai",
        "agentic systems", "frontier agents", "prompt engineering",
    ),
    FineCategory.NLP: (
        "nlp", "natural language processing", "text classification",
        "information extraction", "named entity recognition",
    ),
    FineCategory.COMPUTER_VISION: (
        "computer vision", "image recognition", "object detection",
        "image segmentation", "visual perception", "vision perception",
    ),
    FineCategory.BUSINESS_INTELLIGENCE: (
        "business intelligence", "power bi", "tableau", "looker", "qlik",
        "dashboarding", "data visualization",
    ),
    FineCategory.MLOPS: (
        "mlops", "ml platform", "machine learning platform", "model serving",
        "serving platform", "inference platform", "model deployment",
        "model monitoring", "ml infrastructure", "machine learning infrastructure",
        "ai infrastructure", "frontier ai infrastructure", "ml systems",
        "machine learning systems", "feature store",
    ),
}

# Concrete work a role performs, never company or product vocabulary. Aliases
# inside one concept collapse to a single piece of evidence, and broad labels
# such as "machine learning", "AI" or "LLM" deliberately do not appear here, so
# "We are an AI company building the future" evidences nothing at all.
FINE_DESCRIPTION_CONCEPTS: dict[FineCategory, dict[str, tuple[str, ...]]] = {
    FineCategory.DATA_SCIENCE: {
        "statistical_modeling": ("statistical modeling",),
        "predictive_modeling": ("predictive modeling",),
        "statistical_analysis": ("statistical analysis",),
        "experimentation": ("experimentation", "ab testing", "a b testing"),
        "hypothesis_testing": ("hypothesis testing",),
    },
    FineCategory.DATA_ANALYTICS: {
        "analytical_reporting": ("analytical reporting", "analytics reporting"),
        "cohort_analysis": ("cohort analysis",),
        "funnel_analysis": ("funnel analysis",),
        "kpi_reporting": ("kpi reporting", "kpi definition"),
    },
    FineCategory.DATA_ENGINEERING: {
        "data_pipelines": ("data pipeline", "data pipelines"),
        "etl_elt": ("etl", "elt"),
        "analytical_storage": ("data warehouse", "data lake", "lakehouse"),
        "data_ingestion": ("data ingestion",),
        "data_orchestration": ("data orchestration",),
        "streaming_pipelines": ("streaming pipelines",),
        "spark": ("spark",),
        "dbt": ("dbt",),
        "airflow": ("airflow",),
        "kafka": ("kafka",),
    },
    FineCategory.MACHINE_LEARNING: {
        "model_training": ("model training",),
        "model_evaluation": ("model evaluation",),
        "model_development": ("model development",),
        "deep_learning": ("deep learning",),
        "feature_engineering": ("feature engineering",),
        "hyperparameter_tuning": ("hyperparameter tuning",),
        "reinforcement_learning": ("reinforcement learning",),
    },
    FineCategory.ARTIFICIAL_INTELLIGENCE: {
        # Intentionally thin: concrete AI work that is not more specifically ML,
        # GenAI, NLP or CV is rare, and inventing concepts here would recreate
        # exactly the "AI is noisy" false positives this phase must avoid.
        "applied_ai": ("applied ai",),
        "ai_research": ("ai research",),
    },
    FineCategory.GENERATIVE_AI: {
        "retrieval_augmented_generation": ("retrieval augmented generation", "rag"),
        "agentic_systems": ("agentic systems", "ai agents", "agentic ai"),
        "prompt_engineering": ("prompt engineering",),
        "foundation_models": ("foundation model", "foundation models"),
        "model_fine_tuning": ("fine tuning", "instruction tuning"),
        "vector_search": ("vector database", "vector search"),
    },
    FineCategory.NLP: {
        "text_classification": ("text classification",),
        "information_extraction": ("information extraction",),
        "named_entity_recognition": ("named entity recognition",),
        "sentiment_analysis": ("sentiment analysis",),
        "machine_translation": ("machine translation",),
    },
    FineCategory.COMPUTER_VISION: {
        "object_detection": ("object detection",),
        "image_recognition": ("image recognition",),
        "image_classification": ("image classification",),
        "image_segmentation": ("image segmentation", "semantic segmentation"),
        "visual_perception": ("visual perception",),
        "optical_character_recognition": ("optical character recognition",),
    },
    FineCategory.BUSINESS_INTELLIGENCE: {
        "bi_tooling": ("power bi", "tableau", "looker", "qlik"),
        "dashboarding": ("dashboarding", "dashboards"),
        "semantic_layer": ("semantic layer",),
        "business_intelligence": ("business intelligence",),
    },
    FineCategory.MLOPS: {
        "model_serving": ("model serving",),
        "model_deployment": ("model deployment",),
        "model_monitoring": ("model monitoring",),
        "feature_store": ("feature store",),
        "ml_platform": ("ml platform", "machine learning platform"),
        "ml_infrastructure": ("ml infrastructure", "machine learning infrastructure"),
        "inference_platform": ("inference platform",),
        "model_registry": ("model registry",),
        "experiment_tracking": ("experiment tracking",),
    },
}

#: A description alone must show at least this many distinct concrete concepts
#: of one category before that category is evidenced. One concept is a mention;
#: two independent concepts are the work being described.
MINIMUM_DESCRIPTION_CONCEPTS = 2
