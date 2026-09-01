from dataclasses import replace

import pytest

from services.collector.matching import (
    MatchingInput,
    MatchingOpportunityInput,
    MatchingProfileInput,
    SemanticSimilarityInputError,
    SemanticSimilarityStatus,
    build_opportunity_semantic_document,
    build_profile_semantic_document,
    fit_tfidf_corpus,
    score_matching_input_semantic_similarity,
    score_profile_against_tfidf_corpus,
)
from services.collector.matching.models import (
    MatchingCareerObjectives,
    MatchingEducation,
    MatchingExperience,
    MatchingProfileSkill,
    MatchingProject,
)


def opportunity(identity: int, title: str, description: str | None = None):
    return MatchingOpportunityInput(identity, title, description, None)


def profile(identity: int = 1, text: str | None = "python data pipelines"):
    experiences = () if text is None else (MatchingExperience(text, None, "v1"),)
    return MatchingProfileInput(identity, experiences=experiences)


def test_document_sources_normalization_and_structured_skills_exclusion():
    value = MatchingProfileInput(
        9,
        skills=(MatchingProfileSkill("python", "Python", ("v1",)),),
        experiences=(MatchingExperience("  DATA\tEngineer ", "Café", "v1"),),
        projects=(MatchingProject("Ｐipeline", " SQL  work ", "v1"),),
        educations=(MatchingEducation("MSc", "AI", "v1"),),
        career_objectives=MatchingCareerObjectives(("Lead teams",), "v1"),
    )
    assert build_profile_semantic_document(value).text == (
        "data engineer\ncafé\npipeline\nsql work\nmsc\nai\nlead teams"
    )
    assert build_profile_semantic_document(
        MatchingProfileInput(10, skills=value.skills)
    ).text == ""
    first = opportunity(1, " Data Scientist ", "Build\tmodels")
    altered = replace(first, opportunity_id=2, remote_type="remote")
    assert build_opportunity_semantic_document(first).text == "data scientist\nbuild models"
    assert build_opportunity_semantic_document(first).text == build_opportunity_semantic_document(altered).text


def test_exact_partial_zero_and_explanations():
    corpus = fit_tfidf_corpus((
        opportunity(1, "python data pipelines"), opportunity(2, "marketing sales")
    ))
    exact, zero = score_profile_against_tfidf_corpus(profile(), corpus)
    assert exact.similarity == 1.0
    assert {term.term for term in exact.overlap_terms} == {"python", "data", "pipelines"}
    assert zero.similarity == 0.0
    partial = score_profile_against_tfidf_corpus(profile(text="python"), corpus)[0]
    assert 0.0 < partial.similarity < 1.0


def test_order_independence_and_profile_never_affects_fit():
    items = (opportunity(2, "marketing sales"), opportunity(1, "python data"))
    one, two = fit_tfidf_corpus(items), fit_tfidf_corpus(tuple(reversed(items)))
    assert one.corpus_fingerprint == two.corpus_fingerprint
    assert one.model_fingerprint == two.model_fingerprint
    assert one._vectorizer.vocabulary_ == two._vectorizer.vocabulary_
    assert score_profile_against_tfidf_corpus(profile(text="unseen python"), one) == score_profile_against_tfidf_corpus(profile(text="unseen python"), two)
    before = dict(one._vectorizer.vocabulary_)
    score_profile_against_tfidf_corpus(profile(text="neverincorpus"), one)
    assert one._vectorizer.vocabulary_ == before


def test_empty_and_invalid_inputs():
    with pytest.raises(SemanticSimilarityInputError, match="must not be empty"):
        fit_tfidf_corpus(())
    with pytest.raises(SemanticSimilarityInputError, match="exploitable"):
        fit_tfidf_corpus((opportunity(1, "!"),))
    with pytest.raises(SemanticSimilarityInputError, match="duplicate"):
        fit_tfidf_corpus((opportunity(1, "python"), opportunity(1, "sql")))
    corpus = fit_tfidf_corpus((opportunity(1, "python"), opportunity(2, "!")))
    empty_profile = score_profile_against_tfidf_corpus(profile(text=None), corpus)
    assert all(result.status is SemanticSimilarityStatus.EMPTY_PROFILE_DOCUMENT for result in empty_profile)
    assert all(result.similarity is None for result in empty_profile)
    results = score_profile_against_tfidf_corpus(profile(text="unknownword"), corpus)
    assert results[0].status is SemanticSimilarityStatus.AVAILABLE
    assert results[0].similarity == 0.0
    assert results[1].status is SemanticSimilarityStatus.EMPTY_OPPORTUNITY_DOCUMENT
    assert results[1].similarity is None


def test_pair_requires_identity_and_snapshot_alignment():
    item = opportunity(1, "python", "data")
    corpus = fit_tfidf_corpus((item,))
    matching = MatchingInput(profile(), item)
    assert score_matching_input_semantic_similarity(matching, corpus).similarity > 0
    with pytest.raises(SemanticSimilarityInputError, match="absent"):
        score_matching_input_semantic_similarity(
            MatchingInput(profile(), replace(item, opportunity_id=99)), corpus
        )
    with pytest.raises(SemanticSimilarityInputError, match="snapshot"):
        score_matching_input_semantic_similarity(
            MatchingInput(profile(), replace(item, description="changed")), corpus
        )


def test_all_natural_profile_fields_and_no_structured_double_counting():
    natural = MatchingProfileInput(
        1,
        experiences=(MatchingExperience("data", "pipelines", "v1"),),
        projects=(MatchingProject("python", "models", "v1"),),
        educations=(MatchingEducation("analytics", "science", "v1"),),
        career_objectives=MatchingCareerObjectives(("leadership",), "v1"),
    )
    skilled = replace(natural, profile_id=2, skills=(MatchingProfileSkill("sql", "SQL", ("v1",)),))
    corpus = fit_tfidf_corpus((opportunity(1, "data pipelines python models analytics science leadership"),))
    assert build_profile_semantic_document(natural).text == build_profile_semantic_document(skilled).text
    left = score_profile_against_tfidf_corpus(natural, corpus)[0]
    right = score_profile_against_tfidf_corpus(skilled, corpus)[0]
    assert replace(left, profile_id=2) == right
