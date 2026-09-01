"""Canonical fingerprints for the deterministic TF-IDF semantic layer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .tfidf_similarity import OpportunitySemanticDocument, SemanticSimilarityResult


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_semantic_corpus_payload(
    documents: Sequence[OpportunitySemanticDocument],
) -> dict[str, object]:
    """Return an ID-free, order-insensitive multiset representation."""
    versions = {document.semantic_document_version for document in documents}
    if len(versions) != 1:
        raise ValueError("semantic documents must use exactly one version")
    document_hashes = sorted(
        hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        for document in documents
    )
    return {
        "semantic_document_version": next(iter(versions)),
        "document_hashes": document_hashes,
    }


def semantic_corpus_fingerprint(
    documents: Sequence[OpportunitySemanticDocument],
) -> str:
    return _digest(canonical_semantic_corpus_payload(documents))


def tfidf_model_fingerprint(
    *,
    corpus_fingerprint: str,
    tfidf_version: str,
    semantic_document_version: str,
    sklearn_version: str,
    vectorizer_config: Mapping[str, object],
) -> str:
    return _digest(
        {
            "corpus_fingerprint": corpus_fingerprint,
            "tfidf_version": tfidf_version,
            "semantic_document_version": semantic_document_version,
            "sklearn_version": sklearn_version,
            "vectorizer_config": dict(vectorizer_config),
        }
    )


def canonical_semantic_similarity_payload(
    result: SemanticSimilarityResult,
) -> dict[str, object]:
    """Return semantic result content, deliberately excluding runtime IDs."""
    return {
        "status": result.status.value,
        "similarity": result.similarity,
        "overlap_terms": [
            {"term": overlap.term, "contribution": overlap.contribution}
            for overlap in result.overlap_terms
        ],
        "shared_term_count": result.shared_term_count,
        "profile_feature_count": result.profile_feature_count,
        "opportunity_feature_count": result.opportunity_feature_count,
        "corpus_fingerprint": result.corpus_fingerprint,
        "model_fingerprint": result.model_fingerprint,
        "semantic_document_version": result.semantic_document_version,
        "tfidf_version": result.tfidf_version,
        "sklearn_version": result.sklearn_version,
    }


def semantic_similarity_fingerprint(result: SemanticSimilarityResult) -> str:
    return _digest(canonical_semantic_similarity_payload(result))
