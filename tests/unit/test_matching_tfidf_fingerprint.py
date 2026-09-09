from dataclasses import replace

import pytest

from services.collector.matching import (
    SEMANTIC_BINDING_VERSION,
    MatchingOpportunityInput,
    MatchingProfileInput,
    OpportunitySemanticDocument,
    canonical_semantic_binding_payload,
    canonical_semantic_corpus_payload,
    fit_tfidf_corpus,
    score_profile_against_tfidf_corpus,
    semantic_binding_fingerprint,
    semantic_corpus_fingerprint,
    semantic_similarity_fingerprint,
    tfidf_model_fingerprint,
)
from services.collector.matching.fingerprint import canonical_json
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


# --------------------------------------------------------------------------
# the identity-aware binding, and why it is a second fingerprint rather than a
# change to the first
# --------------------------------------------------------------------------

DOC_A = OpportunitySemanticDocument(1, "python data science")
DOC_B = OpportunitySemanticDocument(2, "airflow data engineering")
#: The same two texts, exchanged between the same two postings.
SWAPPED = (
    OpportunitySemanticDocument(1, DOC_B.text),
    OpportunitySemanticDocument(2, DOC_A.text),
)


def test_the_binding_fingerprint_ignores_the_order_it_was_given():
    assert semantic_binding_fingerprint((DOC_A, DOC_B)) == semantic_binding_fingerprint(
        (DOC_B, DOC_A)
    )


def test_the_binding_fingerprint_sees_documents_exchanged_between_ids():
    """The whole reason this fingerprint exists."""
    assert semantic_binding_fingerprint((DOC_A, DOC_B)) != semantic_binding_fingerprint(
        SWAPPED
    )


def test_the_corpus_fingerprint_deliberately_does_not_see_that_swap():
    """Its blindness is the contract, and it is why the binding was added.

    The TF-IDF model is fitted over the multiset of texts, so a swap produces
    the same model — the corpus fingerprint is right to say so. What changes is
    which percentile belongs to which posting, which is a question about
    identity and is answered one fingerprint over.
    """
    assert semantic_corpus_fingerprint((DOC_A, DOC_B)) == semantic_corpus_fingerprint(
        SWAPPED
    )
    assert canonical_semantic_corpus_payload((DOC_A, DOC_B)) == (
        canonical_semantic_corpus_payload(SWAPPED)
    )
    # And the corpus payload still carries no id at all.
    assert "bindings" not in canonical_semantic_corpus_payload((DOC_A, DOC_B))


def test_the_binding_fingerprint_sees_one_document_changing_under_one_id():
    edited = (replace(DOC_A, text="python data science and mlops"), DOC_B)
    assert semantic_binding_fingerprint((DOC_A, DOC_B)) != semantic_binding_fingerprint(
        edited
    )


def test_the_binding_payload_is_ordered_by_id_and_carries_no_raw_text():
    payload = canonical_semantic_binding_payload((DOC_B, DOC_A))
    assert payload["semantic_binding_version"] == SEMANTIC_BINDING_VERSION
    assert payload["semantic_document_version"] == DOC_A.semantic_document_version
    assert [item["opportunity_id"] for item in payload["bindings"]] == [1, 2]
    assert all(len(item["document_hash"]) == 64 for item in payload["bindings"])
    # Provenance is not a second copy of the corpus.
    for document in (DOC_A, DOC_B):
        assert document.text not in canonical_json(payload)


def test_a_duplicated_opportunity_id_is_refused_rather_than_deduplicated():
    with pytest.raises(ValueError, match="duplicate opportunity_id"):
        semantic_binding_fingerprint((DOC_A, replace(DOC_B, opportunity_id=1)))


def test_mixed_semantic_document_versions_are_refused():
    with pytest.raises(ValueError, match="exactly one version"):
        semantic_binding_fingerprint(
            (DOC_A, replace(DOC_B, semantic_document_version="semantic-document-v0"))
        )


def test_an_empty_corpus_is_refused_the_same_way_both_fingerprints_refuse_it():
    for digest in (semantic_binding_fingerprint, semantic_corpus_fingerprint):
        with pytest.raises(ValueError, match="exactly one version"):
            digest(())


def test_the_binding_is_computed_from_the_documents_the_corpus_was_fitted_over():
    """`build_matching_assessments` reuses `corpus.documents`; so does this."""
    corpus = fit_tfidf_corpus((opp(1, DOC_A.text), opp(2, DOC_B.text)))
    assert semantic_binding_fingerprint(corpus.documents) == (
        semantic_binding_fingerprint((DOC_A, DOC_B))
    )
    assert corpus.corpus_fingerprint == semantic_corpus_fingerprint((DOC_A, DOC_B))
