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


#: The shape of the identity-aware binding payload below. It is versioned
#: separately from the corpus payload because the two answer different
#: questions and may move independently.
SEMANTIC_BINDING_VERSION = "semantic-binding-v1"


def _document_hash(document: OpportunitySemanticDocument) -> str:
    return hashlib.sha256(document.text.encode("utf-8")).hexdigest()


def _one_version(documents: Sequence[OpportunitySemanticDocument]) -> str:
    versions = {document.semantic_document_version for document in documents}
    if len(versions) != 1:
        raise ValueError("semantic documents must use exactly one version")
    return next(iter(versions))


def canonical_semantic_corpus_payload(
    documents: Sequence[OpportunitySemanticDocument],
) -> dict[str, object]:
    """Return an ID-free, order-insensitive multiset representation.

    This answers exactly one question — *is this the same corpus content, and
    therefore the same fitted model?* — and it is deliberately blind to which
    document belongs to which posting, because the TF-IDF model is fitted over
    the multiset and nothing else.

    That blindness is a real gap on its own: two postings that swap texts leave
    this digest unchanged while every stored percentile ends up attached to the
    wrong opportunity. The gap is closed by
    `canonical_semantic_binding_payload` below, **not** by adding ids here — a
    corpus fingerprint that moved when an id moved would stop answering the
    question it exists for.
    """
    version = _one_version(documents)
    document_hashes = sorted(_document_hash(document) for document in documents)
    return {
        "semantic_document_version": version,
        "document_hashes": document_hashes,
    }


def semantic_corpus_fingerprint(
    documents: Sequence[OpportunitySemanticDocument],
) -> str:
    return _digest(canonical_semantic_corpus_payload(documents))


def canonical_semantic_binding_payload(
    documents: Sequence[OpportunitySemanticDocument],
) -> dict[str, object]:
    """Return which document belongs to which posting, in canonical id order.

    The complement of the corpus payload above, and the answer to the other
    question — *is each semantic document still attached to the same
    opportunity?* Where that one is an unordered multiset of texts, this is an
    ordered list of `(opportunity_id, document_hash)` pairs, so exchanging two
    postings' texts moves it even though the multiset is identical.

    Only the hash is carried, never the text: this payload is persisted as run
    provenance, and provenance is not a place to keep a copy of the corpus.

    A duplicate `opportunity_id` is refused rather than deduplicated — a binding
    that named a posting twice would not be a binding — and so is a corpus
    holding more than one semantic document version, for the same reason the
    corpus payload refuses it.
    """
    version = _one_version(documents)
    bindings = sorted(
        (
            {
                "opportunity_id": document.opportunity_id,
                "document_hash": _document_hash(document),
            }
            for document in documents
        ),
        key=lambda item: item["opportunity_id"],
    )
    identifiers = [item["opportunity_id"] for item in bindings]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("duplicate opportunity_id in semantic binding")
    return {
        "semantic_binding_version": SEMANTIC_BINDING_VERSION,
        "semantic_document_version": version,
        "bindings": bindings,
    }


def semantic_binding_fingerprint(
    documents: Sequence[OpportunitySemanticDocument],
) -> str:
    return _digest(canonical_semantic_binding_payload(documents))


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
