"""The closed catalogue of technologies 3.5B can recognise, in code.

**Nothing here is seeded into the database.** `migrations/0013` inserts no
`skills` row, and neither does applying it. A vocabulary row appears the first
time a real posting is found to *require* one of these terms, which is what
keeps `skills` a record of things observed rather than a list somebody typed.
Three hundred pre-inserted names would be three hundred rows nobody wrote, and
a later reader could not tell them apart from the ones a posting produced.

The catalogue is **global**, and deliberately not the profile's. A posting may
require a technology the candidate has never touched, and that difference is
the entire point of extracting requirements at all: an extractor that only
looked for the skills already on somebody's profile could never report one they
lack. No query below reads `profile_skills`, and none ever should.

Canonical names go through the Phase 3.4A normalizer — `normalize_skill` in
`services.digital_twin.skills.normalizer` — rather than through a second
normalizer written here. That is not code reuse for its own sake: the offer
side and the profile side have to compute the *same* `canonical_key` for the
same technology or they are not comparable at all, and two normalizers that
merely look alike are a bug waiting for the day they drift. `sklearn` and
`Scikit-learn` land on one key because that registry already says so, and `C`,
`C++` and `C#` stay three keys because it already refuses to strip punctuation.

An alias is a **spelling**, never a neighbour. `spark` is an alias of Apache
Spark because it is the same product written shorter; `PySpark` is not, because
it is a different one. Nothing in this file maps a technology to a related
technology, a job title to a technology, or a vague domain to a tool: "Data
Scientist" is a role, "Big Data" is a field, "team player" is not a technology,
and none of them appears below.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.digital_twin.skills.normalizer import normalize_skill

__all__ = [
    "SKILL_CATALOG",
    "SKILL_DEFINITIONS",
    "SkillCatalogError",
    "SkillDefinition",
    "SkillTerm",
]


class SkillCatalogError(RuntimeError):
    """Raised at import time when two catalogue entries collide."""


@dataclass(frozen=True)
class SkillDefinition:
    """One technology, and every spelling a posting may use for it."""

    canonical_name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillTerm:
    """A resolved catalogue entry: the shared vocabulary's key and name."""

    canonical_key: str
    canonical_name: str
    aliases: tuple[str, ...]


#: The catalogue. Broad enough for the roles this radar collects — Data
#: Analyst, Data Scientist, ML Engineer, AI Engineer, Data Engineer, Big Data,
#: Analytics, BI, MLOps and GenAI — and narrow enough that every entry is a
#: named tool, language, platform or technique somebody can actually require.
SKILL_DEFINITIONS: tuple[SkillDefinition, ...] = (
    # -- Programming languages ------------------------------------------------
    SkillDefinition("Python", ("python", "python3")),
    # Single letters are matched case-sensitively by the matcher; see there.
    SkillDefinition("R", ("R",)),
    SkillDefinition("SQL", ("sql",)),
    SkillDefinition("Scala", ("scala",)),
    SkillDefinition("Java", ("java",)),
    SkillDefinition("C++", ("c++", "cpp")),
    SkillDefinition("C#", ("c#", "c sharp", "csharp")),
    SkillDefinition("C", ("C",)),
    SkillDefinition("JavaScript", ("javascript", "js")),
    SkillDefinition("TypeScript", ("typescript",)),
    SkillDefinition("Go", ("Go", "Golang", "golang")),
    SkillDefinition("Rust", ("rust",)),
    SkillDefinition("MATLAB", ("matlab",)),
    SkillDefinition("SAS", ("SAS",)),
    SkillDefinition("Bash", ("bash",)),
    SkillDefinition("Shell", ("shell scripting", "shell")),
    SkillDefinition("VBA", ("vba",)),
    # -- Data science and machine learning ------------------------------------
    # `ML` and `AI` are the two abbreviations the Phase 3.4A alias registry
    # already resolves on the profile side; matched case-sensitively, so
    # ordinary prose cannot produce them.
    SkillDefinition("Machine Learning", ("machine learning", "apprentissage automatique", "ML")),
    SkillDefinition("Artificial Intelligence", ("artificial intelligence", "intelligence artificielle", "AI", "IA")),
    SkillDefinition("Deep Learning", ("deep learning", "apprentissage profond")),
    SkillDefinition("Statistics", ("statistics", "statistiques")),
    SkillDefinition("Statistical Modeling", ("statistical modeling", "statistical modelling")),
    SkillDefinition("Time Series", ("time series", "séries temporelles", "series temporelles")),
    SkillDefinition("A/B Testing", ("a/b testing", "ab testing")),
    SkillDefinition("Reinforcement Learning", ("reinforcement learning",)),
    SkillDefinition("Scikit-learn", ("scikit-learn", "scikit learn", "sklearn")),
    SkillDefinition("XGBoost", ("xgboost",)),
    SkillDefinition("LightGBM", ("lightgbm",)),
    SkillDefinition("CatBoost", ("catboost",)),
    SkillDefinition("TensorFlow", ("tensorflow",)),
    SkillDefinition("PyTorch", ("pytorch",)),
    SkillDefinition("Keras", ("keras",)),
    SkillDefinition("JAX", ("jax",)),
    # -- Generative AI and large language models ------------------------------
    SkillDefinition("Generative AI", ("generative ai", "genai", "ia générative", "ia generative")),
    SkillDefinition("Large Language Models", ("large language models", "large language model", "llms", "llm")),
    SkillDefinition("Transformers", ("transformers",)),
    SkillDefinition("Hugging Face", ("hugging face", "huggingface")),
    SkillDefinition("LangChain", ("langchain",)),
    SkillDefinition("LlamaIndex", ("llamaindex", "llama index")),
    SkillDefinition("Retrieval-Augmented Generation", ("retrieval-augmented generation", "retrieval augmented generation", "rag")),
    SkillDefinition("Prompt Engineering", ("prompt engineering",)),
    SkillDefinition("Vector Databases", ("vector databases", "vector database")),
    SkillDefinition("Pinecone", ("pinecone",)),
    SkillDefinition("FAISS", ("faiss",)),
    # -- Natural language processing and computer vision ----------------------
    SkillDefinition("Natural Language Processing", ("natural language processing", "nlp", "traitement du langage naturel")),
    SkillDefinition("Computer Vision", ("computer vision", "vision par ordinateur")),
    SkillDefinition("OpenCV", ("opencv",)),
    SkillDefinition("OCR", ("ocr", "optical character recognition")),
    SkillDefinition("spaCy", ("spacy",)),
    SkillDefinition("NLTK", ("nltk",)),
    # -- Data engineering and big data ----------------------------------------
    SkillDefinition("Apache Spark", ("apache spark", "spark")),
    SkillDefinition("PySpark", ("pyspark",)),
    SkillDefinition("Hadoop", ("hadoop", "apache hadoop")),
    SkillDefinition("Apache Kafka", ("apache kafka", "kafka")),
    SkillDefinition("Apache Flink", ("apache flink", "flink")),
    SkillDefinition("Apache Airflow", ("apache airflow", "airflow")),
    SkillDefinition("dbt", ("dbt",)),
    SkillDefinition("ETL", ("etl",)),
    SkillDefinition("ELT", ("elt",)),
    SkillDefinition("Data Modeling", ("data modeling", "data modelling", "modélisation de données")),
    SkillDefinition("Data Warehousing", ("data warehousing", "data warehouse")),
    SkillDefinition("Databricks", ("databricks",)),
    SkillDefinition("Snowflake", ("snowflake",)),
    SkillDefinition("BigQuery", ("bigquery", "big query")),
    SkillDefinition("Amazon Redshift", ("amazon redshift", "redshift")),
    SkillDefinition("Talend", ("talend",)),
    SkillDefinition("Apache NiFi", ("apache nifi", "nifi")),
    # -- Databases ------------------------------------------------------------
    SkillDefinition("PostgreSQL", ("postgresql", "postgres")),
    SkillDefinition("MySQL", ("mysql",)),
    SkillDefinition("SQL Server", ("sql server", "microsoft sql server", "mssql", "t-sql")),
    SkillDefinition("Oracle", ("oracle database", "pl/sql")),
    SkillDefinition("SQLite", ("sqlite",)),
    SkillDefinition("MongoDB", ("mongodb", "mongo db")),
    SkillDefinition("Redis", ("redis",)),
    SkillDefinition("Elasticsearch", ("elasticsearch", "elastic search")),
    SkillDefinition("Cassandra", ("apache cassandra", "cassandra")),
    SkillDefinition("Neo4j", ("neo4j",)),
    # -- Cloud ----------------------------------------------------------------
    SkillDefinition("AWS", ("aws", "amazon web services")),
    SkillDefinition("Azure", ("microsoft azure", "azure")),
    SkillDefinition("Google Cloud", ("google cloud platform", "google cloud", "gcp")),
    SkillDefinition("Amazon SageMaker", ("amazon sagemaker", "sagemaker")),
    SkillDefinition("Azure Machine Learning", ("azure machine learning", "azure ml")),
    SkillDefinition("Amazon S3", ("amazon s3", "s3")),
    # -- MLOps, DevOps, tooling ----------------------------------------------
    SkillDefinition("MLflow", ("mlflow",)),
    SkillDefinition("Kubeflow", ("kubeflow",)),
    SkillDefinition("MLOps", ("mlops",)),
    SkillDefinition("Docker", ("docker",)),
    SkillDefinition("Kubernetes", ("kubernetes", "k8s")),
    SkillDefinition("Terraform", ("terraform",)),
    SkillDefinition("Git", ("git",)),
    SkillDefinition("GitHub Actions", ("github actions",)),
    SkillDefinition("GitLab CI", ("gitlab ci", "gitlab ci/cd")),
    SkillDefinition("Jenkins", ("jenkins",)),
    SkillDefinition("CI/CD", ("ci/cd", "cicd")),
    SkillDefinition("Linux", ("linux",)),
    # -- Analytics and business intelligence ----------------------------------
    SkillDefinition("Power BI", ("power bi", "powerbi")),
    SkillDefinition("Tableau", ("tableau",)),
    SkillDefinition("Looker", ("looker",)),
    SkillDefinition("Qlik", ("qlik", "qlikview", "qlik sense")),
    SkillDefinition("Excel", ("excel", "microsoft excel")),
    SkillDefinition("Google Analytics", ("google analytics",)),
    SkillDefinition("Pandas", ("pandas",)),
    SkillDefinition("NumPy", ("numpy",)),
    SkillDefinition("SciPy", ("scipy",)),
    SkillDefinition("Matplotlib", ("matplotlib",)),
    SkillDefinition("Seaborn", ("seaborn",)),
    SkillDefinition("Plotly", ("plotly",)),
    SkillDefinition("Streamlit", ("streamlit",)),
    SkillDefinition("Dash", ("plotly dash",)),
    # -- Backend and data APIs ------------------------------------------------
    SkillDefinition("FastAPI", ("fastapi",)),
    SkillDefinition("Flask", ("flask",)),
    SkillDefinition("Django", ("django",)),
    SkillDefinition("REST APIs", ("rest apis", "rest api", "restful apis", "restful api")),
    SkillDefinition("GraphQL", ("graphql",)),
    SkillDefinition("gRPC", ("grpc",)),
)


def _resolve(definitions: tuple[SkillDefinition, ...]) -> tuple[SkillTerm, ...]:
    """Resolve every canonical name through the shared normalizer, once.

    Two collisions are hard failures at import time rather than a resolution
    order. Two entries resolving to one `canonical_key` would mean the
    catalogue holds one technology twice; one alias claimed by two entries would
    mean this file decides silently which technology a posting meant. Both are
    defects, and a defect that raises on import is a defect somebody fixes.
    """
    terms: list[SkillTerm] = []
    by_key: dict[str, str] = {}
    alias_owner: dict[str, str] = {}
    for definition in definitions:
        normalized = normalize_skill(definition.canonical_name)
        key = normalized.canonical_key
        if key in by_key:
            raise SkillCatalogError(
                f"catalogue key {key!r} is claimed by {by_key[key]!r} and "
                f"{definition.canonical_name!r}"
            )
        by_key[key] = definition.canonical_name
        aliases = tuple(dict.fromkeys(definition.aliases + (normalized.canonical_name,)))
        for alias in aliases:
            folded = " ".join(alias.split()).casefold()
            owner = alias_owner.get(folded)
            if owner is not None and owner != key:
                raise SkillCatalogError(
                    f"alias {alias!r} is claimed by two catalogue entries"
                )
            alias_owner[folded] = key
        terms.append(
            SkillTerm(
                canonical_key=key,
                canonical_name=normalized.canonical_name,
                aliases=aliases,
            )
        )
    return tuple(terms)


#: The catalogue, resolved once at import time.
SKILL_CATALOG: tuple[SkillTerm, ...] = _resolve(SKILL_DEFINITIONS)
