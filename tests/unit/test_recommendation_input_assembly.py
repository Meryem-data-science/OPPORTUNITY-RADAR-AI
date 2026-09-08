"""The readiness preflight: what stops a cohort before anything is assessed.

The per-opportunity branches are exercised against a real SQLite database in
`tests/integration/test_recommendation_sqlite.py`; what is covered here are the
whole-cohort refusals, which short-circuit before any row is read.
"""

from types import SimpleNamespace

import pytest

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    MatchingReadError,
)
from services.recommendation import (
    RECOMMENDATION_INPUT_ASSEMBLY_VERSION,
    RecommendationReadinessIssueCode,
    RecommendationReadinessStatus,
    assemble_recommendation_inputs,
)

PROFILE_ID = 7


class Connection:
    """Answers only the one query the short-circuit branches actually reach."""

    def __init__(self, owner=(41,)):
        self.owner = owner

    def execute(self, sql, parameters=()):
        if "FROM profiles" in sql:
            return SimpleNamespace(fetchone=lambda: self.owner)
        raise AssertionError(f"unexpected query: {sql}")


def run(*assessments, status="READY", selected=(1,), **overrides):
    values = dict(
        run_id=9,
        persistence_version=MATCHING_PERSISTENCE_VERSION,
        selection_version=MATCHING_SELECTION_VERSION,
        matching_engine_version=MATCHING_ENGINE_VERSION,
        matching_rules_version=MATCHING_RULES_VERSION,
        semantic_percentile_version=SEMANTIC_PERCENTILE_VERSION,
        assessments=assessments,
    )
    values.update(overrides)
    current = SimpleNamespace(**values) if status == "READY" else None
    return SimpleNamespace(status=status, current_run=current)


def invoke(monkeypatch, *, connection=None, snapshot=None, selected=(1,)):
    monkeypatch.setattr(
        "services.recommendation.input_assembly.read_current_matching",
        lambda *_: snapshot if snapshot is not None else run(),
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.select_matching_opportunity_ids",
        lambda *_: selected,
    )
    return assemble_recommendation_inputs(connection or Connection(), PROFILE_ID)


def codes(result):
    return [issue.code for issue in result.issues]


def test_an_unknown_profile_is_incomplete_and_yields_nothing(monkeypatch):
    result = invoke(monkeypatch, connection=Connection(owner=None))
    assert result.status is RecommendationReadinessStatus.INCOMPLETE
    assert codes(result) == [RecommendationReadinessIssueCode.PROFILE_NOT_FOUND]
    assert result.records == ()
    assert result.assembly_version == RECOMMENDATION_INPUT_ASSEMBLY_VERSION
    assert result.user_id is None


@pytest.mark.parametrize("owner", [(0,), (None,), ("41",), (True,)])
def test_an_invalid_profile_owner_is_refused(monkeypatch, owner):
    result = invoke(monkeypatch, connection=Connection(owner=owner))
    assert codes(result) == [RecommendationReadinessIssueCode.PROFILE_OWNER_INVALID]


def test_an_unreadable_matching_snapshot_is_reported_not_recomputed(monkeypatch):
    def explode(*_):
        raise MatchingReadError("assessment count mismatch")

    monkeypatch.setattr(
        "services.recommendation.input_assembly.read_current_matching", explode
    )
    monkeypatch.setattr(
        "services.recommendation.input_assembly.select_matching_opportunity_ids",
        lambda *_: (1,),
    )
    result = assemble_recommendation_inputs(Connection(), PROFILE_ID)
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_NOT_READY]
    assert "assessment count mismatch" in result.issues[0].message


@pytest.mark.parametrize("status", ["NOT_SYNCED", "EMPTY"])
def test_a_matching_state_that_is_not_ready_stops_the_cohort(monkeypatch, status):
    result = invoke(monkeypatch, snapshot=run(status=status))
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_NOT_READY]
    assert result.user_id == 41 and result.matching_run_id is None


@pytest.mark.parametrize(
    "field",
    [
        "persistence_version",
        "selection_version",
        "matching_engine_version",
        "matching_rules_version",
        "semantic_percentile_version",
    ],
)
def test_any_stale_matching_version_stops_the_cohort(monkeypatch, field):
    result = invoke(monkeypatch, snapshot=run(**{field: "something-else-v9"}))
    assert codes(result) == [RecommendationReadinessIssueCode.MATCHING_VERSION_STALE]
    assert field in result.issues[0].message
    assert result.matching_run_id == 9


def test_a_cohort_that_drifted_from_the_current_selection_is_refused(monkeypatch):
    snapshot = run(SimpleNamespace(opportunity_id=1))
    result = invoke(monkeypatch, snapshot=snapshot, selected=(1, 2))
    assert codes(result) == [RecommendationReadinessIssueCode.STALE_MATCHING_COHORT]
    assert result.records == ()
