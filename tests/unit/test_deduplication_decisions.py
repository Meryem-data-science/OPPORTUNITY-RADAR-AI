"""Unit tests for the cross-source duplicate review registry."""

import json

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.deduplication.audit import AuditOpportunity, compare_pair
from services.collector.deduplication.decisions import (
    CONFIRMED_DUPLICATE,
    NOT_DUPLICATE,
    POSSIBLE_DUPLICATE,
    DecisionError,
    canonicalize_pair,
    confirm_pair,
    get_decision,
    reject_pair,
    stage_candidate,
)


def item(identifier: int, source: str, title: str = "Data Analyst Intern") -> AuditOpportunity:
    url = f"https://{source}.invalid/{identifier}"
    return AuditOpportunity(identifier, title, "Acme Inc", "Paris", None, "2026-01-01", "2026-01-01", "2026-01-01", "2026-01-01", url, None, None, (source,), (url,), (), ())


@pytest.fixture
def connection(tmp_path):
    connection = connect_database(tmp_path / "decisions.db")
    apply_migrations(connection)
    connection.executemany("INSERT INTO sources (id, type, status) VALUES (?, 'test', 'active')", [("a",), ("b",)])
    for identifier in range(1, 5):
        connection.execute("""INSERT INTO opportunities
            (id, canonical_title, organization, discovered_at, first_seen_at, last_seen_at, source_url, status)
            VALUES (?, 'Data Analyst Intern', 'Acme Inc', '2026-01-01', '2026-01-01', '2026-01-01', ?, 'visible')""", (identifier, f"https://x.invalid/{identifier}"))
    connection.executemany("INSERT INTO opportunity_sources (opportunity_id, source_id, source_url, discovered_at) VALUES (?, ?, ?, '2026-01-01')", [(1, "a", "https://a/1"), (2, "b", "https://b/2"), (3, "a", "https://a/3"), (4, "a", "https://a/4")])
    connection.commit()
    yield connection
    connection.close()


def candidate(a=1, b=2, title="Data Analyst Intern"):
    result = compare_pair(item(a, "a"), item(b, "b", title))
    assert result is not None
    return result


def test_canonicalization_and_invalid_ids() -> None:
    assert canonicalize_pair(20, 5) == (5, 20)
    with pytest.raises(DecisionError, match="different"):
        canonicalize_pair(5, 5)
    with pytest.raises(DecisionError, match="positive"):
        canonicalize_pair(0, 2)


@pytest.mark.parametrize("title", ["Data Analyst Intern", "Intern Data Analyst"])
def test_strong_and_possible_candidates_are_staged(connection, title) -> None:
    decision = stage_candidate(connection, candidate(title=title))
    assert decision is not None
    assert decision.status == POSSIBLE_DUPLICATE


def test_weak_is_not_staged(connection) -> None:
    weak = candidate(title="Data Analyst Trainee")
    assert weak.classification == "WEAK_CANDIDATE"
    assert stage_candidate(connection, weak) is None
    assert get_decision(connection, 1, 2) is None


def test_rescan_refreshes_evidence_without_duplicate_row(connection) -> None:
    stage_candidate(connection, candidate(), "2026-01-01T00:00:00+00:00")
    refreshed = stage_candidate(connection, candidate(title="Intern Data Analyst"), "2026-02-01T00:00:00+00:00")
    assert refreshed is not None
    assert refreshed.audit_classification == "POSSIBLE_CANDIDATE"
    assert refreshed.last_detected_at.startswith("2026-02-01")
    assert connection.execute("SELECT COUNT(*) FROM deduplication_decisions").fetchone() == (1,)
    assert json.loads(connection.execute("SELECT reasons_json FROM deduplication_decisions").fetchone()[0]) == list(refreshed.reasons)


@pytest.mark.parametrize(("review", "expected"), [(confirm_pair, CONFIRMED_DUPLICATE), (reject_pair, NOT_DUPLICATE)])
def test_human_decision_survives_rescan(connection, review, expected) -> None:
    stage_candidate(connection, candidate())
    reviewed = review(connection, 2, 1, "checked")
    assert reviewed.status == expected
    assert reviewed.reviewed_at and reviewed.review_note == "checked"
    assert stage_candidate(connection, candidate()).status == expected


def test_decision_requires_staged_cross_source_pair(connection) -> None:
    with pytest.raises(DecisionError, match="no staged"):
        confirm_pair(connection, 1, 2)
    with pytest.raises(DecisionError, match="not cross-source"):
        reject_pair(connection, 3, 4)
    same_source_candidate = compare_pair(item(3, "a"), item(4, "b"))
    assert same_source_candidate is not None
    with pytest.raises(DecisionError, match="not cross-source"):
        stage_candidate(connection, same_source_candidate)
