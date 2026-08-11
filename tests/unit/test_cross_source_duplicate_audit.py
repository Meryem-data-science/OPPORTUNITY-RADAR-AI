"""Unit tests for deterministic cross-source duplicate audit heuristics."""

import pytest

from services.collector.deduplication.audit import (
    POSSIBLE_CANDIDATE,
    STRONG_CANDIDATE,
    AuditOpportunity,
    audit_opportunities,
    compare_pair,
    normalize_location,
    normalize_organization,
    normalize_title,
)


def opportunity(identifier: int, source: str, **values) -> AuditOpportunity:
    defaults = {
        "canonical_title": "Data Analyst Intern",
        "organization": "Acme, Inc.",
        "location": "Paris, France",
        "description": None,
        "published_at": "2026-01-01T00:00:00+00:00",
        "discovered_at": "2026-01-02T00:00:00+00:00",
        "first_seen_at": "2026-01-02T00:00:00+00:00",
        "last_seen_at": "2026-01-02T00:00:00+00:00",
        "source_url": f"https://{source}.invalid/{identifier}",
        "application_url": None,
        "canonical_url": None,
        "sources": (source,),
        "source_urls": (f"https://{source}.invalid/{identifier}",),
        "source_application_urls": (),
        "source_canonical_urls": (),
    }
    defaults.update(values)
    return AuditOpportunity(id=identifier, **defaults)


@pytest.mark.parametrize(
    ("normalizer", "left", "right"),
    [
        (normalize_title, "  DATA—Analyst  ", "data analyst"),
        (normalize_organization, "Café,  DATA!", "Café data"),
        (normalize_location, " Paris, France ", "paris france"),
    ],
)
def test_conservative_normalization_handles_case_space_punctuation_and_unicode(
    normalizer, left, right
) -> None:
    assert normalizer(left) == normalizer(right)
    assert normalizer(None) == ""


def test_exact_title_and_organization_across_sources_is_strong_and_explainable() -> None:
    candidate = compare_pair(opportunity(1, "greenhouse"), opportunity(2, "linkedin"))
    assert candidate is not None
    assert candidate.classification == STRONG_CANDIDATE
    assert candidate.title_normalized_exact
    assert candidate.organization_normalized_exact
    assert candidate.reasons
    assert candidate.opportunity_a.canonical_title == "Data Analyst Intern"


def test_reordered_title_has_useful_similarity_without_exact_match() -> None:
    candidate = compare_pair(
        opportunity(1, "greenhouse"),
        opportunity(2, "linkedin", canonical_title="Intern - Data Analyst"),
    )
    assert candidate is not None
    assert candidate.title_similarity == 1.0
    assert not candidate.title_normalized_exact
    assert candidate.classification == POSSIBLE_CANDIDATE


def test_organization_formatting_difference_is_normalized() -> None:
    candidate = compare_pair(
        opportunity(1, "a", organization="Acme, Inc."),
        opportunity(2, "b", organization="  ACME INC "),
    )
    assert candidate is not None
    assert candidate.organization_normalized_exact


def test_generic_titles_at_different_organizations_are_not_strong() -> None:
    candidate = compare_pair(
        opportunity(1, "a", canonical_title="Engineer", organization="Northwind"),
        opportunity(2, "b", canonical_title="Engineer", organization="Contoso"),
    )
    assert candidate is None or candidate.classification != STRONG_CANDIDATE


def test_shared_company_canonical_url_does_not_make_different_jobs_strong() -> None:
    shared_url = "https://company.invalid/careers"
    candidate = compare_pair(
        opportunity(
            1,
            "a",
            canonical_title="Data Analyst",
            canonical_url=shared_url,
            source_canonical_urls=(shared_url,),
        ),
        opportunity(
            2,
            "b",
            canonical_title="Senior Backend Engineer",
            canonical_url=shared_url,
            source_canonical_urls=(shared_url,),
        ),
    )
    assert candidate is None or candidate.classification != STRONG_CANDIDATE


def test_shared_generic_application_url_does_not_make_different_jobs_strong() -> None:
    shared_url = "https://company.invalid/apply"
    candidate = compare_pair(
        opportunity(
            1,
            "a",
            canonical_title="Data Analyst",
            application_url=shared_url,
            source_application_urls=(shared_url,),
        ),
        opportunity(
            2,
            "b",
            canonical_title="Senior Backend Engineer",
            application_url=shared_url,
            source_application_urls=(shared_url,),
        ),
    )
    assert candidate is None or candidate.classification != STRONG_CANDIDATE


def test_shared_url_reinforces_existing_high_text_match_and_is_explained() -> None:
    shared_url = "https://company.invalid/jobs/data-analyst-intern"
    candidate = compare_pair(
        opportunity(
            1,
            "a",
            application_url=shared_url,
            source_application_urls=(shared_url,),
        ),
        opportunity(
            2,
            "b",
            canonical_title="Intern - Data Analyst",
            application_url=shared_url,
            source_application_urls=(shared_url,),
        ),
    )
    assert candidate is not None
    assert candidate.classification == STRONG_CANDIDATE
    assert candidate.shared_application_url
    assert "application URL is shared" in candidate.reasons


def test_missing_location_is_unknown_not_incompatible() -> None:
    candidate = compare_pair(
        opportunity(1, "a", location=None), opportunity(2, "b")
    )
    assert candidate is not None
    assert candidate.location_signal == "UNKNOWN"


def test_same_source_pair_is_not_reported() -> None:
    report = audit_opportunities([opportunity(1, "same"), opportunity(2, "same")])
    assert report.cross_source_pairs_examined == 0
    assert report.candidates == ()


def test_structured_report_exposes_signals_and_original_values() -> None:
    report = audit_opportunities([opportunity(1, "a"), opportunity(2, "b")])
    payload = report.to_dict()
    candidate = payload["candidates"][0]
    assert candidate["title_similarity"] == 1.0
    assert candidate["location_signal"] == "EXACT"
    assert candidate["reasons"]
    assert candidate["opportunity_a"]["organization"] == "Acme, Inc."
    assert "source_external_id" in report.limitations[0]
