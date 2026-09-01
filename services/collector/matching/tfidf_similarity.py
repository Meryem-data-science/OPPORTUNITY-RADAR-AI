"""Deterministic, corpus-fitted TF-IDF cosine similarity for natural text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

import sklearn
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .models import MatchingInput, MatchingOpportunityInput, MatchingProfileInput
from .tfidf_fingerprint import semantic_corpus_fingerprint, tfidf_model_fingerprint

SEMANTIC_DOCUMENT_VERSION = "semantic-doc-v1"
TFIDF_VERSION = "tfidf-v1"

TFIDF_VECTORIZER_CONFIG: dict[str, object] = {
    "analyzer": "word",
    "lowercase": False,
    "ngram_range": (1, 1),
    "min_df": 1,
    "max_df": 1.0,
    "max_features": None,
    "binary": False,
    "use_idf": True,
    "smooth_idf": True,
    "sublinear_tf": False,
    "norm": "l2",
    "strip_accents": None,
    "stop_words": None,
    "token_pattern": r"(?u)\b\w\w+\b",
}


class SemanticSimilarityInputError(ValueError):
    """Raised when a semantic corpus or pair cannot be evaluated safely."""


class SemanticSimilarityStatus(str, Enum):
    AVAILABLE = "available"
    EMPTY_PROFILE_DOCUMENT = "empty_profile_document"
    EMPTY_OPPORTUNITY_DOCUMENT = "empty_opportunity_document"


@dataclass(frozen=True)
class ProfileSemanticDocument:
    profile_id: int
    text: str
    semantic_document_version: str = SEMANTIC_DOCUMENT_VERSION


@dataclass(frozen=True)
class OpportunitySemanticDocument:
    opportunity_id: int
    text: str
    semantic_document_version: str = SEMANTIC_DOCUMENT_VERSION


@dataclass(frozen=True)
class SemanticOverlapTerm:
    term: str
    contribution: float


@dataclass(frozen=True)
class SemanticSimilarityResult:
    profile_id: int
    opportunity_id: int
    status: SemanticSimilarityStatus
    similarity: float | None
    overlap_terms: tuple[SemanticOverlapTerm, ...]
    shared_term_count: int
    profile_feature_count: int
    opportunity_feature_count: int
    corpus_fingerprint: str
    model_fingerprint: str
    corpus_document_count: int
    vocabulary_size: int
    semantic_document_version: str
    tfidf_version: str
    sklearn_version: str


@dataclass(frozen=True)
class FittedTfidfCorpus:
    documents: tuple[OpportunitySemanticDocument, ...]
    corpus_fingerprint: str
    model_fingerprint: str
    document_count: int
    vocabulary_size: int
    tfidf_version: str
    semantic_document_version: str
    sklearn_version: str
    _vectorizer: TfidfVectorizer = field(repr=False, compare=False)
    _opportunity_matrix: csr_matrix = field(repr=False, compare=False)


def _normalize_fragments(fragments: Sequence[str | None]) -> str:
    normalized: list[str] = []
    for fragment in fragments:
        if fragment is None:
            continue
        value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", fragment).strip())
        if value:
            normalized.append(value.casefold())
    return "\n".join(normalized)


def build_profile_semantic_document(
    profile: MatchingProfileInput,
) -> ProfileSemanticDocument:
    fragments: list[str | None] = []
    for experience in profile.experiences:
        fragments.extend((experience.role_text, experience.description_text))
    for project in profile.projects:
        fragments.extend((project.title_text, project.description_text))
    for education in profile.educations:
        fragments.extend((education.program_text, education.description_text))
    if profile.career_objectives is not None:
        fragments.extend(profile.career_objectives.objectives)
    return ProfileSemanticDocument(profile.profile_id, _normalize_fragments(fragments))


def build_opportunity_semantic_document(
    opportunity: MatchingOpportunityInput,
) -> OpportunitySemanticDocument:
    return OpportunitySemanticDocument(
        opportunity.opportunity_id,
        _normalize_fragments((opportunity.canonical_title, opportunity.description)),
    )


def fit_tfidf_corpus(
    opportunities: Sequence[MatchingOpportunityInput],
) -> FittedTfidfCorpus:
    if not opportunities:
        raise SemanticSimilarityInputError("opportunity corpus must not be empty")
    documents = tuple(build_opportunity_semantic_document(item) for item in opportunities)
    ids = [document.opportunity_id for document in documents]
    if len(ids) != len(set(ids)):
        raise SemanticSimilarityInputError("duplicate opportunity_id in semantic corpus")
    documents = tuple(sorted(documents, key=lambda item: (item.text, item.opportunity_id)))
    vectorizer = TfidfVectorizer(**TFIDF_VECTORIZER_CONFIG)
    try:
        matrix = vectorizer.fit_transform(document.text for document in documents).tocsr()
    except ValueError as exc:
        if "empty vocabulary" in str(exc):
            raise SemanticSimilarityInputError(
                "opportunity corpus contains no exploitable tokens"
            ) from exc
        raise
    corpus_fingerprint = semantic_corpus_fingerprint(documents)
    model_fingerprint = tfidf_model_fingerprint(
        corpus_fingerprint=corpus_fingerprint,
        tfidf_version=TFIDF_VERSION,
        semantic_document_version=SEMANTIC_DOCUMENT_VERSION,
        sklearn_version=sklearn.__version__,
        vectorizer_config=TFIDF_VECTORIZER_CONFIG,
    )
    return FittedTfidfCorpus(
        documents=documents,
        corpus_fingerprint=corpus_fingerprint,
        model_fingerprint=model_fingerprint,
        document_count=len(documents),
        vocabulary_size=len(vectorizer.vocabulary_),
        tfidf_version=TFIDF_VERSION,
        semantic_document_version=SEMANTIC_DOCUMENT_VERSION,
        sklearn_version=sklearn.__version__,
        _vectorizer=vectorizer,
        _opportunity_matrix=matrix,
    )


def _stable_float(value: float) -> float:
    if -1e-12 < value < 0:
        value = 0.0
    if 1 < value < 1 + 1e-12:
        value = 1.0
    if not 0.0 <= value <= 1.0:
        raise ArithmeticError("cosine value is outside its numeric bounds")
    return round(value, 12)


def _result(
    *, corpus: FittedTfidfCorpus, profile_id: int, row: int,
    profile_vector: csr_matrix, similarities: Any, empty_profile: bool,
) -> SemanticSimilarityResult:
    document = corpus.documents[row]
    opportunity_vector = corpus._opportunity_matrix.getrow(row)
    profile_features = int(profile_vector.count_nonzero())
    opportunity_features = int(opportunity_vector.count_nonzero())
    if empty_profile:
        status = SemanticSimilarityStatus.EMPTY_PROFILE_DOCUMENT
        similarity = None
        overlaps: tuple[SemanticOverlapTerm, ...] = ()
    elif opportunity_features == 0:
        status = SemanticSimilarityStatus.EMPTY_OPPORTUNITY_DOCUMENT
        similarity = None
        overlaps = ()
    else:
        status = SemanticSimilarityStatus.AVAILABLE
        similarity = _stable_float(float(similarities[row]))
        products = profile_vector.multiply(opportunity_vector).tocoo()
        names = corpus._vectorizer.get_feature_names_out()
        overlaps = tuple(sorted(
            (SemanticOverlapTerm(str(names[column]), _stable_float(float(value)))
             for column, value in zip(products.col, products.data, strict=True)
             if value > 0),
            key=lambda item: (-item.contribution, item.term),
        ))
    return SemanticSimilarityResult(
        profile_id=profile_id, opportunity_id=document.opportunity_id,
        status=status, similarity=similarity, overlap_terms=overlaps,
        shared_term_count=len(overlaps), profile_feature_count=profile_features,
        opportunity_feature_count=opportunity_features,
        corpus_fingerprint=corpus.corpus_fingerprint,
        model_fingerprint=corpus.model_fingerprint,
        corpus_document_count=corpus.document_count,
        vocabulary_size=corpus.vocabulary_size,
        semantic_document_version=corpus.semantic_document_version,
        tfidf_version=corpus.tfidf_version, sklearn_version=corpus.sklearn_version,
    )


def score_profile_against_tfidf_corpus(
    profile: MatchingProfileInput, corpus: FittedTfidfCorpus,
) -> tuple[SemanticSimilarityResult, ...]:
    document = build_profile_semantic_document(profile)
    profile_vector = corpus._vectorizer.transform([document.text]).tocsr()
    similarities = cosine_similarity(
        profile_vector, corpus._opportunity_matrix, dense_output=True
    )[0]
    results = tuple(
        _result(corpus=corpus, profile_id=profile.profile_id, row=row,
                profile_vector=profile_vector, similarities=similarities,
                empty_profile=not document.text)
        for row in range(corpus.document_count)
    )
    return tuple(sorted(results, key=lambda item: item.opportunity_id))


def score_matching_input_semantic_similarity(
    matching_input: MatchingInput, corpus: FittedTfidfCorpus,
) -> SemanticSimilarityResult:
    expected = build_opportunity_semantic_document(matching_input.opportunity)
    matching_rows = [
        row for row, document in enumerate(corpus.documents)
        if document.opportunity_id == expected.opportunity_id
    ]
    if not matching_rows:
        raise SemanticSimilarityInputError("opportunity_id is absent from fitted corpus")
    if corpus.documents[matching_rows[0]] != expected:
        raise SemanticSimilarityInputError("opportunity semantic snapshot does not match corpus")
    return next(
        result for result in score_profile_against_tfidf_corpus(
            matching_input.profile, corpus
        ) if result.opportunity_id == expected.opportunity_id
    )
