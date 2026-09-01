from dataclasses import replace

from services.collector.matching import (
    MatchingOpportunityInput,
    MatchingProfileInput,
    OpportunitySemanticDocument,
    canonical_semantic_corpus_payload,
    fit_tfidf_corpus,
    score_profile_against_tfidf_corpus,
    semantic_corpus_fingerprint,
    semantic_similarity_fingerprint,
    tfidf_model_fingerprint,
)
from services.collector.matching.models import MatchingExperience
from services.collector.matching.tfidf_similarity import TFIDF_VECTORIZER_CONFIG


def opp(identity, text):
    return MatchingOpportunityInput(identity, text, None, None)


def test_corpus_fingerprint_ignores_order_and_ids_but_preserves_multiplicity():
    left = (OpportunitySemanticDocument(1, "python"), OpportunitySemanticDocument(2, "sql"))
    right = (OpportunitySemanticDocument(99, "sql"), OpportunitySemanticDocument(98, "python"))
    assert semantic_corpus_fingerprint(left) == semantic_corpus_fingerprint(right)
    assert canonical_semantic_corpus_payload(left)["document_hashes"] == canonical_semantic_corpus_payload(right)["document_hashes"]
    assert semantic_corpus_fingerprint(left) != semantic_corpus_fingerprint(left + (left[0],))


def test_model_fingerprint_captures_config_and_semantic_content():
    first = fit_tfidf_corpus((opp(1, "python data"),))
    same = fit_tfidf_corpus((opp(88, "python data"),))
    changed = fit_tfidf_corpus((opp(1, "python sql"),))
    assert first.model_fingerprint == same.model_fingerprint
    assert first.model_fingerprint != changed.model_fingerprint
    config = dict(TFIDF_VECTORIZER_CONFIG)
    config["binary"] = True
    altered = tfidf_model_fingerprint(
        corpus_fingerprint=first.corpus_fingerprint,
        tfidf_version=first.tfidf_version,
        semantic_document_version=first.semantic_document_version,
        sklearn_version=first.sklearn_version,
        vectorizer_config=config,
    )
    assert altered != first.model_fingerprint


def test_result_fingerprint_excludes_runtime_ids_and_changes_with_evidence():
    corpus = fit_tfidf_corpus((opp(1, "python data"),))
    first = score_profile_against_tfidf_corpus(
        MatchingProfileInput(1, experiences=(MatchingExperience("python", None, "v1"),)), corpus
    )[0]
    other_id = replace(first, profile_id=999, opportunity_id=888)
    assert semantic_similarity_fingerprint(first) == semantic_similarity_fingerprint(other_id)
    changed = score_profile_against_tfidf_corpus(
        MatchingProfileInput(1, experiences=(MatchingExperience("python data", None, "v1"),)), corpus
    )[0]
    assert semantic_similarity_fingerprint(first) != semantic_similarity_fingerprint(changed)
