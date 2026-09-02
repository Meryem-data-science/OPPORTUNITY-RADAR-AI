from datetime import date
from types import SimpleNamespace

import pytest

from services.collector.matching import (
    MATCHING_ENGINE_VERSION,
    MATCHING_PERSISTENCE_VERSION,
    MATCHING_RULES_VERSION,
    MATCHING_SELECTION_VERSION,
    SEMANTIC_PERCENTILE_VERSION,
    MatchLane,
)
from services.collector.qualification.taxonomy import ListingQuality
from services.eligibility import ELIGIBILITY_ENGINE_VERSION, GlobalStatus
from services.priority.input_assembly import (
    PriorityReadinessIssueCode,
    PriorityReadinessStatus,
    assemble_priority_inputs,
    parse_persisted_date,
)

EVALUATION_DATE = date(2026, 9, 2)


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, owner=(41,), opportunities=None):
        self.owner = owner
        self.opportunities = opportunities or {}

    def execute(self, sql, parameters):
        if "FROM profiles" in sql:
            return Result(self.owner)
        if "FROM opportunities AS o" in sql:
            return Result(self.opportunities.get(parameters[0]))
        raise AssertionError(sql)


def assessment(opportunity_id=1, **overrides):
    values = dict(
        opportunity_id=opportunity_id,
        lane=MatchLane.PRIMARY.value,
        match_quality=0.8,
        evidence_coverage=1.0,
        assessment_fingerprint="a" * 64,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def matching(*assessments, status="READY", **overrides):
    run_values = dict(
        run_id=9,
        persistence_version=MATCHING_PERSISTENCE_VERSION,
        selection_version=MATCHING_SELECTION_VERSION,
        matching_engine_version=MATCHING_ENGINE_VERSION,
        matching_rules_version=MATCHING_RULES_VERSION,
        semantic_percentile_version=SEMANTIC_PERCENTILE_VERSION,
        assessments=assessments,
    )
    run_values.update(overrides)
    run = SimpleNamespace(**run_values) if status == "READY" else None
    return SimpleNamespace(status=status, current_run=run)


def opportunity(published_at=None, deadline=None, quality="NORMAL_LISTING"):
    return (
        "Title",
        "Organization",
        published_at,
        deadline,
        "https://source.invalid/1",
        None,
        None,
        quality,
    )


def eligibility(**overrides):
    values = dict(
        status=GlobalStatus.UNKNOWN,
        engine_version=ELIGIBILITY_ENGINE_VERSION,
        input_fingerprint="e" * 64,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def invoke(monkeypatch, *, connection=None, snapshot=None, selected=(1,), stored=None):
    connection = connection or Connection(opportunities={1: opportunity()})
    snapshot = snapshot or matching(assessment())
    seen = []
    monkeypatch.setattr(
        "services.priority.input_assembly.read_current_matching", lambda *_: snapshot
    )
    monkeypatch.setattr(
        "services.priority.input_assembly.select_matching_opportunity_ids",
        lambda *_: selected,
    )

    def read(_, user_id, opportunity_id):
        seen.append((user_id, opportunity_id))
        if stored is None:
            return eligibility()
        return stored() if callable(stored) else stored

    monkeypatch.setattr("services.priority.input_assembly.read_eligibility", read)
    return assemble_priority_inputs(connection, 7, EVALUATION_DATE), seen


def codes(result):
    return [issue.code for issue in result.issues]


def test_profile_not_found_returns_no_records():
    result = assemble_priority_inputs(Connection(owner=None), 7, EVALUATION_DATE)
    assert result.status is PriorityReadinessStatus.INCOMPLETE
    assert codes(result) == [PriorityReadinessIssueCode.PROFILE_NOT_FOUND]
    assert result.records == ()


def test_real_profile_owner_is_used_for_eligibility(monkeypatch):
    result, seen = invoke(monkeypatch)
    assert result.status is PriorityReadinessStatus.READY
    assert result.profile_id == 7 and result.user_id == 41
    assert seen == [(41, 1)]
    assert result.records[0].priority_input.eligibility.profile_id == 7


@pytest.mark.parametrize("status", ["NOT_SYNCED", "EMPTY"])
def test_matching_not_ready_states(monkeypatch, status):
    result, _ = invoke(monkeypatch, snapshot=matching(status=status), selected=())
    assert codes(result) == [PriorityReadinessIssueCode.MATCHING_NOT_READY]
    assert result.records == ()


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
def test_each_stale_matching_version_fails_closed(monkeypatch, field):
    result, _ = invoke(monkeypatch, snapshot=matching(assessment(), **{field: "old"}))
    assert codes(result) == [PriorityReadinessIssueCode.MATCHING_VERSION_STALE]
    assert result.records == ()


def test_stale_matching_cohort_fails_closed(monkeypatch):
    result, _ = invoke(monkeypatch, selected=(1, 2))
    assert codes(result) == [PriorityReadinessIssueCode.STALE_MATCHING_COHORT]
    assert result.records == ()


def test_persisted_unknown_is_valid_and_null_dates_have_no_fallback(monkeypatch):
    connection = Connection(opportunities={1: opportunity()})
    result, _ = invoke(monkeypatch, connection=connection, stored=eligibility())
    inputs = result.records[0].priority_input
    assert result.status is PriorityReadinessStatus.READY
    assert inputs.eligibility.status is GlobalStatus.UNKNOWN
    assert inputs.publication_quality is ListingQuality.NORMAL_LISTING
    assert inputs.published_at is None and inputs.deadline is None
    assert PriorityReadinessIssueCode.ELIGIBILITY_SNAPSHOT_MISSING not in codes(result)


def test_missing_eligibility_is_not_fabricated_as_unknown(monkeypatch):
    result, _ = invoke(monkeypatch, stored=lambda: None)
    assert codes(result) == [PriorityReadinessIssueCode.ELIGIBILITY_SNAPSHOT_MISSING]
    assert result.records == ()


@pytest.mark.parametrize("error", [ValueError("bad"), TypeError("bad")])
def test_invalid_eligibility_is_not_also_reported_missing(monkeypatch, error):
    monkeypatch.setattr(
        "services.priority.input_assembly.read_eligibility",
        lambda *_: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        "services.priority.input_assembly.read_current_matching",
        lambda *_: matching(assessment()),
    )
    monkeypatch.setattr(
        "services.priority.input_assembly.select_matching_opportunity_ids",
        lambda *_: (1,),
    )
    result = assemble_priority_inputs(
        Connection(opportunities={1: opportunity()}), 7, EVALUATION_DATE
    )
    assert codes(result) == [PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE]


@pytest.mark.parametrize(
    ("stored", "quality", "expected"),
    [
        (
            eligibility(engine_version="old"),
            "NORMAL_LISTING",
            PriorityReadinessIssueCode.ELIGIBILITY_VERSION_STALE,
        ),
        (
            eligibility(),
            None,
            PriorityReadinessIssueCode.QUALIFICATION_SNAPSHOT_MISSING,
        ),
        (eligibility(), "INVALID", PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE),
        (
            eligibility(input_fingerprint="bad"),
            "NORMAL_LISTING",
            PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE,
        ),
    ],
)
def test_invalid_or_missing_upstream_snapshots(monkeypatch, stored, quality, expected):
    result, _ = invoke(
        monkeypatch,
        connection=Connection(opportunities={1: opportunity(quality=quality)}),
        stored=stored,
    )
    assert expected in codes(result)
    assert result.records == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"lane": "INVALID"},
        {"match_quality": -0.1},
        {"match_quality": 1.1},
        {"match_quality": float("nan")},
        {"match_quality": float("inf")},
        {"match_quality": True},
        {"evidence_coverage": -0.1},
        {"evidence_coverage": 1.1},
        {"evidence_coverage": float("nan")},
        {"evidence_coverage": float("inf")},
        {"evidence_coverage": True},
        {"evidence_coverage": 0.0, "match_quality": 0.1},
        {"evidence_coverage": 0.5, "match_quality": None},
        {"assessment_fingerprint": ""},
        {"assessment_fingerprint": "z" * 64},
    ],
)
def test_invalid_matching_values_are_caught_during_preflight(monkeypatch, overrides):
    result, _ = invoke(monkeypatch, snapshot=matching(assessment(**overrides)))
    assert PriorityReadinessIssueCode.INVALID_UPSTREAM_VALUE in codes(result)
    assert result.records == ()


@pytest.mark.parametrize(
    ("published", "deadline", "expected"),
    [
        ("unknown", None, PriorityReadinessIssueCode.INVALID_PUBLISHED_AT),
        ("2026-09-03", None, PriorityReadinessIssueCode.INVALID_PUBLISHED_AT),
        (None, "yesterday", PriorityReadinessIssueCode.INVALID_DEADLINE),
    ],
)
def test_temporal_errors_fail_preflight(monkeypatch, published, deadline, expected):
    result, _ = invoke(
        monkeypatch,
        connection=Connection(opportunities={1: opportunity(published, deadline)}),
    )
    assert expected in codes(result)
    assert result.records == ()


def test_full_corpus_failure_returns_no_partial_records_and_orders_issues(monkeypatch):
    connection = Connection(
        opportunities={
            1: opportunity(deadline="bad"),
            2: opportunity(published_at="bad", quality=None),
        }
    )
    result, _ = invoke(
        monkeypatch,
        connection=connection,
        snapshot=matching(assessment(1), assessment(2)),
        selected=(1, 2),
    )
    assert result.status is PriorityReadinessStatus.INCOMPLETE
    assert result.records == ()
    assert [(issue.opportunity_id, issue.code.value) for issue in result.issues] == [
        (1, "INVALID_DEADLINE"),
        (2, "INVALID_PUBLISHED_AT"),
        (2, "QUALIFICATION_SNAPSHOT_MISSING"),
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("2026-09-02", date(2026, 9, 2)),
        ("2026-09-02T10:30:00", date(2026, 9, 2)),
        ("2026-09-03T00:30:00+02:00", date(2026, 9, 2)),
        ("2026-09-01T23:30:00-02:00", date(2026, 9, 2)),
    ],
)
def test_strict_persisted_date_parsing(raw, expected):
    assert parse_persisted_date(raw) == expected


@pytest.mark.parametrize("raw", ["", " ", "yesterday", "unknown", "2026-02-30"])
def test_invalid_persisted_dates_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_persisted_date(raw)
