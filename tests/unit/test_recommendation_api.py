"""Phase 11.1A: unit coverage for the read-only Recommendation public surface.

Nothing here opens a database. The Recommendation read model and persistence
audit are replaced by stubs returning their *own* frozen dataclasses, filled
with hand-built values, and the connection is a fake that answers exactly the
statements a read-only surface may issue and fails the test on anything else.

What is under test is the adapter alone: the server-side profile, the
query-only guard, the fail-closed integrity gate, the enrichment through the
existing Phase 8 decoder and link-priority helpers, and above all that the
persisted ranking, the persisted score and the persisted reason partition reach
the response exactly as they were read.

Every version string below is a TEST ONLY literal no build ships, so a value
that reaches the response can only have been copied from what was read.
"""

import ast
from pathlib import Path
from types import MappingProxyType

from fastapi.testclient import TestClient
import pytest

from services.api import main
from services.api import recommendation as recommendation_api
from services.api.recommendation import (
    PUBLIC_RECOMMENDATION_ERROR,
    RecommendationApiReadError,
    RecommendationResponse,
    read_recommendation_surface,
)
from services.collector.config import DatabaseBackend, load_settings
from services.recommendation.input_assembly import (
    RecommendationReadinessIssue,
    RecommendationReadinessIssueCode,
)
from services.recommendation.models import RecommendationDisposition
from services.recommendation.persistence_audit import (
    RecommendationPersistenceAuditError,
    RecommendationPersistenceAuditIssue,
    RecommendationPersistenceAuditReport,
)
from services.recommendation.read_model import (
    RecommendationAssessmentReadModel,
    RecommendationProfileReadModel,
    RecommendationReadError,
    RecommendationRunReadModel,
)

PROFILE_ID = 7
RUN_ID = 5
PERSISTENCE_VERSION = "TEST-ONLY-persistence-v0"
ASSEMBLY_VERSION = "TEST-ONLY-assembly-v0"
ENGINE_VERSION = "TEST-ONLY-engine-v0"
RULES_VERSION = "TEST-ONLY-rules-v0"
AUDIT_VERSION = "TEST-ONLY-audit-v0"
AUDIT_FINGERPRINT = "d" * 64
FINE_VERSION = "TEST-ONLY-fine-v0"

RECOMMENDED = RecommendationDisposition.RECOMMENDED
UNCERTAIN = RecommendationDisposition.UNCERTAIN
OUTSIDE = RecommendationDisposition.OUTSIDE_PREFERENCES

RUN_FIELDS = (
    "run_id",
    "created_at",
    "assessment_count",
    "persistence_version",
    "input_assembly_version",
    "recommendation_engine_version",
    "recommendation_rules_version",
    "source_matching_run_id",
    "source_matching_run_fingerprint",
    "batch_fingerprint",
    "run_fingerprint",
)


# --------------------------------------------------------------------------
# read-model and audit values, built from their own dataclasses
# --------------------------------------------------------------------------


def digest(seed: int) -> str:
    return f"{seed:064x}"


def freeze(value):
    """The shape the read model hands out: mapping proxies and tuples."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value):
    if isinstance(value, MappingProxyType):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def payload(disposition, score, coverage, strengths=(), gaps=(), unknowns=()):
    return {
        "versions": {
            "recommendation_engine": ENGINE_VERSION,
            "recommendation_rules": RULES_VERSION,
        },
        "result": {
            "recommendation_score": score,
            "recommendation_evidence_coverage": coverage,
            "disposition": disposition.value,
            "strengths": list(strengths),
            "confirmed_gaps": list(gaps),
            "unknowns": list(unknowns),
        },
    }


def assessment(
    opportunity_id,
    rank,
    disposition=RECOMMENDED,
    score=0.5,
    coverage=1.0,
    *,
    strengths=(),
    gaps=(),
    unknowns=(),
    raw_payload=None,
):
    body = (
        payload(disposition, score, coverage, strengths, gaps, unknowns)
        if raw_payload is None
        else raw_payload
    )
    return RecommendationAssessmentReadModel(
        opportunity_id=opportunity_id,
        rank_position=rank,
        disposition=disposition,
        recommendation_score=score,
        evidence_coverage=coverage,
        assessment_fingerprint=digest(opportunity_id),
        created_at="2026-09-14 00:00:00",
        assessment_payload=freeze(body),
    )


def ready_model(assessments, *, history_count=1):
    run = RecommendationRunReadModel(
        run_id=RUN_ID,
        profile_id=PROFILE_ID,
        source_matching_run_id=3,
        persistence_version=PERSISTENCE_VERSION,
        input_assembly_version=ASSEMBLY_VERSION,
        recommendation_engine_version=ENGINE_VERSION,
        recommendation_rules_version=RULES_VERSION,
        source_matching_run_fingerprint=digest(900),
        batch_fingerprint=digest(901),
        run_fingerprint=digest(902),
        assessment_count=len(assessments),
        created_at="2026-09-14 00:00:01",
        batch_payload=freeze(
            {
                "assessment_count": len(assessments),
                "ranked_assessment_fingerprints": [
                    item.assessment_fingerprint for item in assessments
                ],
            }
        ),
        assessments=tuple(assessments),
    )
    return RecommendationProfileReadModel(
        PROFILE_ID,
        "READY",
        PERSISTENCE_VERSION,
        ASSEMBLY_VERSION,
        RUN_ID,
        run,
        history_count,
        (),
    )


def not_synced_model():
    return RecommendationProfileReadModel(
        PROFILE_ID, "NOT_SYNCED", None, None, None, None, 0, ()
    )


def incomplete_model(issues, *, history_count=0):
    return RecommendationProfileReadModel(
        PROFILE_ID,
        "INCOMPLETE",
        PERSISTENCE_VERSION,
        ASSEMBLY_VERSION,
        None,
        None,
        history_count,
        tuple(issues),
    )


def audit_report(status, current_run_id=None, *, ok=True, issues=()):
    return RecommendationPersistenceAuditReport(
        audit_version=AUDIT_VERSION,
        profile_id=PROFILE_ID,
        status=status,
        current_run_id=current_run_id,
        run_count=0,
        audited_run_count=0,
        assessment_count=0,
        ok=ok,
        issues=tuple(issues),
        runs=(),
        audit_fingerprint=AUDIT_FINGERPRINT,
    )


# --------------------------------------------------------------------------
# a connection that can only be read
# --------------------------------------------------------------------------


class Cursor:
    def __init__(self, row=None, rows=()):
        self._row = row
        self._rows = list(rows)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


FORBIDDEN_OPENINGS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")


class FakeConnection:
    """Answers only what a read-only surface may ask; a write fails the test."""

    def __init__(self, *, opportunities=(), observations=(), query_only=1):
        self.opportunities = {row[0]: row for row in opportunities}
        self.observations = list(observations)
        self.query_only = query_only
        self.statements = []
        self.in_transaction = False
        self.closed = False

    def execute(self, statement, parameters=()):
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        upper = normalized.upper()
        assert not upper.startswith(FORBIDDEN_OPENINGS), normalized
        assert "BEGIN IMMEDIATE" not in upper, normalized
        assert "BEGIN EXCLUSIVE" not in upper, normalized
        if upper == "PRAGMA QUERY_ONLY = ON":
            return Cursor()
        if upper == "PRAGMA QUERY_ONLY":
            return Cursor(row=(self.query_only,))
        if upper in ("BEGIN", "BEGIN DEFERRED"):
            self.in_transaction = True
            return Cursor()
        if upper == "ROLLBACK":
            self.in_transaction = False
            return Cursor()
        if upper.startswith("SELECT") and "FROM OPPORTUNITY_SOURCES" in upper:
            return Cursor(
                rows=[row for row in self.observations if row[0] in parameters]
            )
        if upper.startswith("SELECT") and "FROM OPPORTUNITIES" in upper:
            return Cursor(
                rows=[
                    self.opportunities[item]
                    for item in sorted(set(parameters))
                    if item in self.opportunities
                ]
            )
        raise AssertionError(f"unexpected statement: {normalized}")

    def close(self):
        self.closed = True

    def metadata_reads(self):
        return [item for item in self.statements if "FROM opportunit" in item]


UNCLASSIFIED_COLUMNS = (None, None, None, None, None, None)


def opportunity_row(
    opportunity_id,
    *,
    title=None,
    organization="TEST ONLY Org",
    location=None,
    source_url=None,
    application_url=None,
    canonical_url=None,
    fine=UNCLASSIFIED_COLUMNS,
):
    """id, title, org, location, last_seen_at, source/application/canonical url,
    then the coarse qualification and the five persisted fine columns."""
    return (
        opportunity_id,
        title or f"TEST ONLY role {opportunity_id}",
        organization,
        location,
        "2026-09-13T00:00:00+00:00",
        source_url or f"https://example.invalid/source/{opportunity_id}",
        application_url,
        canonical_url,
        *fine,
    )


def rows_for(*ids):
    return [opportunity_row(item) for item in ids]


class Surface:
    """Wires the stubs into the adapter and records what it was asked."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.connections = []
        self.opened_paths = []
        self.read_calls = []
        self.audit_calls = []

    def configure(self, current, audit, connection=None):
        connection = connection if connection is not None else FakeConnection()

        def connect(path):
            self.opened_paths.append(path)
            self.connections.append(connection)
            return connection

        def read(conn, profile_id):
            self.read_calls.append((conn, profile_id, list(conn.statements)))
            if isinstance(current, BaseException):
                raise current
            return current

        def audit_history(conn, profile_id):
            self.audit_calls.append((conn, profile_id))
            if isinstance(audit, BaseException):
                raise audit
            return audit

        self.monkeypatch.setattr(recommendation_api, "connect_readonly_database", connect)
        self.monkeypatch.setattr(recommendation_api, "read_current_recommendation", read)
        self.monkeypatch.setattr(
            recommendation_api, "audit_recommendation_profile_history", audit_history
        )
        return connection


@pytest.fixture
def surface(monkeypatch, tmp_path):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(tmp_path / "never-opened.db"))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(PROFILE_ID))
    return Surface(monkeypatch)


def ready_surface(surface, assessments, **model_options):
    current = ready_model(assessments, **model_options)
    ids = [item.opportunity_id for item in assessments]
    connection = surface.configure(
        current, audit_report("READY", RUN_ID), FakeConnection(opportunities=rows_for(*ids))
    )
    return current, connection


def items_of(response):
    return response.model_dump()["current_run"]["items"]


def refused(connection=None):
    with pytest.raises(RecommendationApiReadError) as raised:
        read_recommendation_surface()
    assert str(raised.value) == PUBLIC_RECOMMENDATION_ERROR
    if connection is not None:
        assert connection.closed
    return raised.value


# --------------------------------------------------------------------------
# the profile is the server's
# --------------------------------------------------------------------------


def test_the_profile_comes_from_the_server_environment(surface):
    surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))

    response = read_recommendation_surface()

    assert response.profile_id == PROFILE_ID
    assert [call[1] for call in surface.read_calls] == [PROFILE_ID]
    assert [call[1] for call in surface.audit_calls] == [PROFILE_ID]


@pytest.mark.parametrize(
    "raw", [None, "", "abc", "0", "-3", "01", "+7", "7.0", "1_000", "true"]
)
def test_an_absent_invalid_or_non_canonical_profile_is_refused_before_any_read(
    surface, monkeypatch, raw
):
    surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))
    if raw is None:
        monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID")
    else:
        monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", raw)

    refused()

    # No database is opened, so no profile could have been picked out of one.
    assert surface.connections == []
    assert surface.read_calls == [] and surface.audit_calls == []


def test_the_route_ignores_a_client_supplied_profile_id(surface):
    surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))

    response = TestClient(main.app).get("/api/recommendation?profile_id=999")

    assert response.status_code == 200
    assert response.json()["profile_id"] == PROFILE_ID
    assert [call[1] for call in surface.read_calls] == [PROFILE_ID]


def test_the_route_declares_no_parameter_and_an_explicit_response_model():
    routes = [
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/api/recommendation"
    ]

    assert len(routes) == 1
    route = routes[0]
    assert route.methods == {"GET"}
    assert route.response_model is RecommendationResponse
    assert route.dependant.query_params == []
    assert route.dependant.path_params == []
    assert route.dependant.body_params == []


def test_a_surface_failure_is_a_503_carrying_only_the_public_sentence(monkeypatch):
    def fail():
        raise RecommendationApiReadError(PUBLIC_RECOMMENDATION_ERROR)

    monkeypatch.setattr(main, "read_recommendation_surface", fail)

    response = TestClient(main.app).get("/api/recommendation")

    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_RECOMMENDATION_ERROR}


# --------------------------------------------------------------------------
# query-only
# --------------------------------------------------------------------------


def test_query_only_is_enabled_and_verified_before_the_read_model_runs(surface):
    connection = surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))

    read_recommendation_surface()

    seen = surface.read_calls[0][2]
    assert "PRAGMA query_only = ON" in seen and "PRAGMA query_only" in seen
    assert seen.index("PRAGMA query_only = ON") < seen.index("PRAGMA query_only")
    assert connection.closed


def test_a_connection_that_does_not_confirm_query_only_is_refused(surface):
    connection = surface.configure(
        not_synced_model(), audit_report("NOT_SYNCED"), FakeConnection(query_only=0)
    )

    refused(connection)

    assert surface.read_calls == [] and surface.audit_calls == []


def test_the_configured_sqlite_path_is_opened_through_the_read_only_helper(
    surface, tmp_path
):
    surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))

    read_recommendation_surface()

    assert surface.opened_paths == [tmp_path / "never-opened.db"]


def test_a_non_sqlite_backend_is_refused_before_any_connection_or_read(
    surface, monkeypatch
):
    surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://test-only.invalid")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "TEST-ONLY-token")
    # The configuration itself is valid, so the refusal is the surface's own.
    assert load_settings().database_backend is DatabaseBackend.TURSO

    refused()
    response = TestClient(main.app).get("/api/recommendation")

    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_RECOMMENDATION_ERROR}
    assert "TEST-ONLY" not in response.text
    assert surface.connections == [] and surface.opened_paths == []
    assert surface.read_calls == [] and surface.audit_calls == []


# --------------------------------------------------------------------------
# NOT_SYNCED
# --------------------------------------------------------------------------


def test_not_synced_is_reported_as_itself_with_no_run_and_no_items(surface):
    connection = surface.configure(not_synced_model(), audit_report("NOT_SYNCED"))

    body = read_recommendation_surface().model_dump()

    assert body["status"] == "NOT_SYNCED"
    assert body["current_run"] is None
    assert body["readiness_issues"] == []
    assert body["history_count"] == 0
    assert body["persistence_version"] is None
    assert body["input_assembly_version"] is None
    assert body["integrity"] == {
        "ok": True,
        "audit_version": AUDIT_VERSION,
        "audit_fingerprint": AUDIT_FINGERPRINT,
    }
    # No run means nothing to enrich: not even a metadata query is issued.
    assert connection.metadata_reads() == []
    assert connection.closed


# --------------------------------------------------------------------------
# INCOMPLETE
# --------------------------------------------------------------------------

ISSUES = (
    RecommendationReadinessIssue(
        RecommendationReadinessIssueCode.MATCHING_VERSION_STALE,
        "TEST ONLY internal detail: cannot read matching_runs",
        None,
    ),
    RecommendationReadinessIssue(
        RecommendationReadinessIssueCode.OPPORTUNITY_MISSING,
        "TEST ONLY opportunity 42 was not found: sqlite3 said no",
        42,
    ),
)


def test_incomplete_exposes_each_issue_code_and_posting_in_stored_order(surface):
    connection = surface.configure(incomplete_model(ISSUES), audit_report("INCOMPLETE"))

    body = read_recommendation_surface().model_dump()

    assert body["status"] == "INCOMPLETE"
    assert body["current_run"] is None
    assert body["readiness_issues"] == [
        {"code": "MATCHING_VERSION_STALE", "opportunity_id": None},
        {"code": "OPPORTUNITY_MISSING", "opportunity_id": 42},
    ]
    assert body["persistence_version"] == PERSISTENCE_VERSION
    assert body["input_assembly_version"] == ASSEMBLY_VERSION
    assert connection.metadata_reads() == []


def test_incomplete_readiness_messages_never_reach_the_caller(surface):
    """The stored messages embed raw upstream error text; only codes are public."""
    surface.configure(incomplete_model(ISSUES), audit_report("INCOMPLETE"))

    response = TestClient(main.app).get("/api/recommendation")

    assert response.status_code == 200
    for leak in ("TEST ONLY", "sqlite3", "matching_runs", "message"):
        assert leak not in response.text, leak


def test_an_earlier_history_does_not_turn_incomplete_into_ready(surface):
    surface.configure(
        incomplete_model(ISSUES[:1], history_count=4), audit_report("INCOMPLETE")
    )

    body = read_recommendation_surface().model_dump()

    assert body["status"] == "INCOMPLETE"
    assert body["history_count"] == 4
    assert body["current_run"] is None


# --------------------------------------------------------------------------
# READY
# --------------------------------------------------------------------------


def ranked_run():
    """Rank order deliberately disagrees with score order and with id order."""
    return [
        assessment(30, 1, RECOMMENDED, 0.20, 0.5, strengths=("GEOGRAPHY_MATCHED",)),
        assessment(10, 2, UNCERTAIN, 0.95, 1.0, unknowns=("ELIGIBILITY_UNKNOWN",)),
        assessment(
            20, 3, OUTSIDE, 0.50, 0.7, gaps=("WORK_MODE_OUTSIDE_PREFERENCES",)
        ),
    ]


def test_ready_exposes_the_current_run_exactly_as_persisted(surface):
    current, _ = ready_surface(surface, ranked_run(), history_count=2)

    body = read_recommendation_surface().model_dump()

    run = body["current_run"]
    stored = current.current_run
    assert body["status"] == "READY"
    assert body["history_count"] == 2
    assert body["readiness_issues"] == []
    assert body["persistence_version"] == PERSISTENCE_VERSION
    assert {key: run[key] for key in RUN_FIELDS} == {
        key: getattr(stored, key) for key in RUN_FIELDS
    }
    assert run["assessment_count"] == len(run["items"]) == 3


def test_the_persisted_ranking_is_never_re_sorted(surface):
    ready_surface(surface, ranked_run())

    items = items_of(read_recommendation_surface())

    assert [item["rank_position"] for item in items] == [1, 2, 3]
    assert [item["opportunity_id"] for item in items] == [30, 10, 20]
    assert [item["recommendation"]["recommendation_score"] for item in items] == [
        0.20,
        0.95,
        0.50,
    ]
    # What a re-sort by score, or by id, would have produced instead.
    by_score = sorted(items, key=lambda item: -item["recommendation"]["recommendation_score"])
    assert [item["opportunity_id"] for item in by_score] == [10, 20, 30]
    assert [item["opportunity_id"] for item in items] != [10, 20, 30]


def test_each_item_carries_its_persisted_assessment_values(surface):
    assessments = ranked_run()
    ready_surface(surface, assessments)

    items = items_of(read_recommendation_surface())

    for item, stored in zip(items, assessments, strict=True):
        snapshot = item["recommendation"]
        assert item["opportunity_id"] == stored.opportunity_id
        assert item["rank_position"] == stored.rank_position
        assert item["opportunity"]["id"] == stored.opportunity_id
        assert snapshot["disposition"] == stored.disposition.value
        assert snapshot["recommendation_score"] == stored.recommendation_score
        assert snapshot["evidence_coverage"] == stored.evidence_coverage
        assert snapshot["assessment_fingerprint"] == stored.assessment_fingerprint
        assert snapshot["explanation"] == thaw(stored.assessment_payload)


# --------------------------------------------------------------------------
# absence is not zero, and unknown is not a gap
# --------------------------------------------------------------------------


def test_a_missing_score_stays_null_and_a_real_zero_stays_zero(surface):
    ready_surface(
        surface,
        [
            assessment(1, 1, UNCERTAIN, None, 0.0, unknowns=("NO_NUMERIC_EVIDENCE",)),
            assessment(2, 2, RECOMMENDED, 0.0, 0.4),
        ],
    )

    response = TestClient(main.app).get("/api/recommendation")

    assert response.status_code == 200
    first, second = [
        item["recommendation"] for item in response.json()["current_run"]["items"]
    ]
    assert "recommendation_score" in first
    assert first["recommendation_score"] is None
    assert first["evidence_coverage"] == 0.0
    assert second["recommendation_score"] == 0.0
    assert second["recommendation_score"] is not None


def test_the_reason_partition_is_copied_and_never_reclassified(surface):
    strengths = ("REQUIRED_SKILLS_ALL_CONFIRMED", "GEOGRAPHY_MATCHED")
    gaps = ("WORK_MODE_OUTSIDE_PREFERENCES",)
    unknowns = ("WORK_MODE_UNKNOWN", "ELIGIBILITY_UNKNOWN", "REQUIRED_SKILLS_NOT_ALL_CONFIRMED")
    ready_surface(
        surface,
        [
            assessment(
                1, 1, UNCERTAIN, 0.6, 0.8, strengths=strengths, gaps=gaps, unknowns=unknowns
            )
        ],
    )

    snapshot = items_of(read_recommendation_surface())[0]["recommendation"]

    # Exactly the stored lists, in the stored order.
    assert snapshot["strengths"] == list(strengths)
    assert snapshot["confirmed_gaps"] == list(gaps)
    assert snapshot["unknowns"] == list(unknowns)
    assert not set(snapshot["unknowns"]) & set(snapshot["confirmed_gaps"])
    assert snapshot["explanation"]["result"]["unknowns"] == list(unknowns)


def _without(key):
    body = payload(UNCERTAIN, 0.5, 1.0)
    del body["result"][key]
    return body


def _with_result(value):
    body = payload(UNCERTAIN, 0.5, 1.0)
    body["result"] = value
    return body


def _with_partition(key, value):
    body = payload(UNCERTAIN, 0.5, 1.0)
    body["result"][key] = value
    return body


BROKEN_PAYLOADS = {
    "no result section": {"versions": {}},
    "result is not an object": _with_result(["UNKNOWN"]),
    "no unknowns": _without("unknowns"),
    "no confirmed gaps": _without("confirmed_gaps"),
    "no strengths": _without("strengths"),
    "unknowns is text": _with_partition("unknowns", "ELIGIBILITY_UNKNOWN"),
    "a code that is not text": _with_partition("confirmed_gaps", [7]),
}


@pytest.mark.parametrize("broken", BROKEN_PAYLOADS.values(), ids=BROKEN_PAYLOADS.keys())
def test_a_payload_without_a_readable_reason_partition_refuses_the_surface(
    surface, broken
):
    _, connection = ready_surface(
        surface,
        [assessment(1, 1), assessment(2, 2, UNCERTAIN, raw_payload=broken)],
    )

    refused(connection)


# --------------------------------------------------------------------------
# metadata, fine classification and the original link
# --------------------------------------------------------------------------

OFFICIAL_APPLY = "https://boards.example.invalid/test-only/jobs/30/apply"
LINKEDIN_SOURCE = "https://www.linkedin.invalid/jobs/view/30"
LINKEDIN_APPLY = "https://www.linkedin.invalid/jobs/view/30/apply"
CORE_SECONDARY = '["MLOPS", "DATA_ENGINEERING", "NLP"]'
CORE_EVIDENCE = (
    '[{"category": "COMPUTER_VISION", "field": "TITLE", "kind": "ROLE_PHRASE",'
    ' "signal": "computer vision engineer"},'
    ' {"category": "MLOPS", "field": "TITLE", "kind": "CONTEXT_PHRASE",'
    ' "signal": "model serving"},'
    ' {"category": "DATA_ENGINEERING", "field": "DESCRIPTION",'
    ' "kind": "CONCRETE_CONCEPT", "signal": "airflow"},'
    ' {"category": "NLP", "field": "DESCRIPTION", "kind": "CONCRETE_CONCEPT",'
    ' "signal": "named entity recognition"}]'
)


def metadata_connection():
    return FakeConnection(
        opportunities=[
            opportunity_row(
                30,
                title="TEST ONLY Computer Vision Engineer",
                organization="TEST ONLY Vision Org",
                location="Casablanca",
                source_url=LINKEDIN_SOURCE,
                fine=(
                    "CORE_TARGET",
                    "COMPUTER_VISION",
                    CORE_SECONDARY,
                    CORE_EVIDENCE,
                    '["TEST ONLY core reason"]',
                    FINE_VERSION,
                ),
            ),
            opportunity_row(
                10,
                application_url="https://example.invalid/apply/10",
                fine=(
                    "ADJACENT_TARGET",
                    "OTHER",
                    "[]",
                    "[]",
                    '["TEST ONLY other reason"]',
                    FINE_VERSION,
                ),
            ),
            # A LEFT JOIN miss: no qualification row at all.
            opportunity_row(20),
            opportunity_row(
                40,
                fine=(
                    "UNCERTAIN",
                    None,
                    "[]",
                    "[]",
                    '["TEST ONLY not qualified"]',
                    FINE_VERSION,
                ),
            ),
        ],
        observations=[
            (30, 501, "gmail_linkedin_alert", LINKEDIN_APPLY, LINKEDIN_SOURCE, None),
            (
                30,
                502,
                "greenhouse",
                OFFICIAL_APPLY,
                "https://boards.example.invalid/test-only/jobs/30",
                None,
            ),
        ],
    )


def metadata_surface(surface):
    assessments = [assessment(30, 1), assessment(10, 2), assessment(20, 3), assessment(40, 4)]
    surface.configure(
        ready_model(assessments), audit_report("READY", RUN_ID), metadata_connection()
    )
    return {
        item["opportunity_id"]: item["opportunity"]
        for item in items_of(read_recommendation_surface())
    }


def test_title_organization_location_and_last_seen_are_the_persisted_values(surface):
    opportunities = metadata_surface(surface)

    core = opportunities[30]
    assert core["id"] == 30
    assert core["canonical_title"] == "TEST ONLY Computer Vision Engineer"
    assert core["organization"] == "TEST ONLY Vision Org"
    assert core["location"] == "Casablanca"
    assert core["last_seen_at"] == "2026-09-13T00:00:00+00:00"
    assert opportunities[20]["location"] is None


def test_the_original_url_is_chosen_by_the_existing_link_priority(surface, monkeypatch):
    calls = []
    real = recommendation_api.select_original_url

    def spy(observations, *, fallback):
        observations = list(observations)
        calls.append((observations, fallback))
        return real(observations, fallback=fallback)

    monkeypatch.setattr(recommendation_api, "select_original_url", spy)

    opportunities = metadata_surface(surface)

    # The official ATS observation beats the job board and the stored fallback.
    assert opportunities[30]["original_url"] == OFFICIAL_APPLY
    # No observation: the stored application link, preferred over the source.
    assert opportunities[10]["original_url"] == "https://example.invalid/apply/10"
    assert opportunities[20]["original_url"] == "https://example.invalid/source/20"
    assert len(calls) == 4
    by_fallback = {fallback: observations for observations, fallback in calls}
    assert [item.source_type for item in by_fallback[LINKEDIN_SOURCE]] == [
        "gmail_linkedin_alert",
        "greenhouse",
    ]


def test_the_fine_classification_is_decoded_by_the_existing_phase_8_helper(
    surface, monkeypatch
):
    calls = []
    real = recommendation_api.decode_fine_classification

    def spy(*columns):
        calls.append(columns)
        return real(*columns)

    monkeypatch.setattr(recommendation_api, "decode_fine_classification", spy)

    opportunities = metadata_surface(surface)

    assert len(calls) == 4
    core = opportunities[30]
    assert core["fine_primary_category"] == "COMPUTER_VISION"
    assert core["fine_secondary_categories"] == ["MLOPS", "DATA_ENGINEERING", "NLP"]
    assert [item["category"] for item in core["fine_category_evidence"]] == [
        "COMPUTER_VISION",
        "MLOPS",
        "DATA_ENGINEERING",
        "NLP",
    ]
    assert core["fine_reasons"] == ["TEST ONLY core reason"]
    assert core["fine_classifier_version"] == FINE_VERSION


def test_a_never_classified_posting_keeps_all_five_fine_fields_null(surface):
    never = metadata_surface(surface)[20]

    assert never["fine_primary_category"] is None
    assert never["fine_secondary_categories"] is None
    assert never["fine_category_evidence"] is None
    assert never["fine_reasons"] is None
    assert never["fine_classifier_version"] is None


def test_other_and_classified_without_a_category_stay_distinct_from_unclassified(
    surface,
):
    opportunities = metadata_surface(surface)

    other = opportunities[10]
    assert other["fine_primary_category"] == "OTHER"
    assert other["fine_secondary_categories"] == []
    assert other["fine_category_evidence"] == []
    assert other["fine_classifier_version"] == FINE_VERSION

    no_category = opportunities[40]
    assert no_category["fine_primary_category"] is None
    assert no_category["fine_secondary_categories"] == []
    assert no_category["fine_category_evidence"] == []
    assert no_category["fine_reasons"] == ["TEST ONLY not qualified"]
    assert no_category["fine_classifier_version"] == FINE_VERSION


def test_an_incoherent_persisted_fine_row_refuses_the_surface(surface):
    connection = FakeConnection(
        opportunities=[
            opportunity_row(
                1, fine=("UNCERTAIN", "OTHER", "[]", "[]", '["x"]', FINE_VERSION)
            )
        ]
    )
    surface.configure(
        ready_model([assessment(1, 1)]), audit_report("READY", RUN_ID), connection
    )

    refused(connection)


# --------------------------------------------------------------------------
# integrity
# --------------------------------------------------------------------------


def test_the_audit_identity_is_exposed_for_a_ready_run(surface):
    ready_surface(surface, ranked_run())

    body = read_recommendation_surface().model_dump()

    assert body["integrity"] == {
        "ok": True,
        "audit_version": AUDIT_VERSION,
        "audit_fingerprint": AUDIT_FINGERPRINT,
    }


def test_a_failed_audit_refuses_the_whole_surface(surface):
    finding = RecommendationPersistenceAuditIssue(
        "ASSESSMENT", "ASSESSMENT_FINGERPRINT_MISMATCH", RUN_ID, 30, "TEST ONLY detail"
    )
    connection = surface.configure(
        ready_model(ranked_run()),
        audit_report("READY", RUN_ID, ok=False, issues=(finding,)),
        FakeConnection(opportunities=rows_for(10, 20, 30)),
    )

    refused(connection)

    assert connection.metadata_reads() == []


@pytest.mark.parametrize(
    "failure",
    [
        RecommendationPersistenceAuditError("TEST ONLY cannot read matching_runs"),
        RuntimeError("TEST ONLY unexpected"),
    ],
    ids=["audit error", "unexpected error"],
)
def test_an_audit_that_cannot_run_refuses_the_surface(surface, failure):
    connection = surface.configure(ready_model(ranked_run()), failure)

    error = refused(connection)

    assert "TEST ONLY" not in str(error)


def test_a_read_model_refusal_refuses_the_surface_before_anything_else(surface):
    connection = surface.configure(
        RecommendationReadError("TEST ONLY run 5 has a non-contiguous ranking"),
        audit_report("READY", RUN_ID),
    )

    error = refused(connection)

    assert "TEST ONLY" not in str(error)
    assert surface.audit_calls == []
    assert connection.metadata_reads() == []


@pytest.mark.parametrize(
    "audit",
    [audit_report("NOT_SYNCED"), audit_report("INCOMPLETE"), audit_report("READY", 999)],
    ids=["not synced", "incomplete", "another run"],
)
def test_an_audit_describing_another_state_refuses_the_surface(surface, audit):
    connection = surface.configure(
        ready_model(ranked_run()), audit, FakeConnection(opportunities=rows_for(10, 20, 30))
    )

    refused(connection)


# --------------------------------------------------------------------------
# the ranking is indivisible
# --------------------------------------------------------------------------


def test_a_ranked_opportunity_without_metadata_refuses_the_whole_ranking(surface):
    connection = surface.configure(
        ready_model(ranked_run()),
        audit_report("READY", RUN_ID),
        FakeConnection(opportunities=rows_for(10, 30)),
    )

    refused(connection)


def test_missing_metadata_is_a_503_and_never_a_shorter_list(surface):
    surface.configure(
        ready_model(ranked_run()),
        audit_report("READY", RUN_ID),
        FakeConnection(opportunities=rows_for(10, 30)),
    )

    response = TestClient(main.app).get("/api/recommendation")

    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_RECOMMENDATION_ERROR}
    assert "items" not in response.text


# --------------------------------------------------------------------------
# architecture: the adapter can only read
# --------------------------------------------------------------------------

ADAPTER_SOURCE = Path(recommendation_api.__file__)

#: Every entry point that computes, synchronizes, classifies or stores. The
#: adapter must not even name one.
COMPUTING_NAMES = frozenset(
    {
        "build_recommendation_assessment",
        "build_recommendation_batch",
        "rank_recommendation_assessments",
        "assemble_recommendation_inputs",
        "store_recommendation_batch",
        "sync_recommendations",
        "recommendation_assessment_fingerprint",
        "recommendation_batch_fingerprint",
        "recommendation_run_fingerprint",
        "classify_fine_categories",
        "persist_qualifications",
        "sync_matching",
        "sync_priority",
    }
)


def adapter_tree():
    return ast.parse(ADAPTER_SOURCE.read_text(encoding="utf-8"))


def imported_modules(tree):
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    return modules


def test_the_adapter_imports_only_the_recommendation_read_model_and_audit():
    recommendation_imports = {
        module
        for module in imported_modules(adapter_tree())
        if module == "services.recommendation"
        or module.startswith("services.recommendation.")
    }

    assert recommendation_imports == {
        "services.recommendation.read_model",
        "services.recommendation.persistence_audit",
    }
    for forbidden in ("engine", "sync", "input_assembly", "persistence"):
        assert f"services.recommendation.{forbidden}" not in recommendation_imports


def test_the_adapter_never_names_a_computing_entry_point():
    names = set()
    for node in ast.walk(adapter_tree()):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])

    assert not names & COMPUTING_NAMES, names & COMPUTING_NAMES


def test_the_adapter_opens_sqlite_only_through_the_read_only_helper():
    """`connect_database` would create a missing file and its directory."""
    names = set()
    for node in ast.walk(adapter_tree()):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])

    assert "connect_readonly_database" in names
    for forbidden in (
        "connect_configured_database",
        "connect_database",
        "connect_turso",
        "connect",
    ):
        assert forbidden not in names, forbidden


def _literal_sql(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def test_every_statement_the_adapter_executes_is_a_read():
    statements = []
    called = set()
    for node in ast.walk(adapter_tree()):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            assert node.args, f"execute() with no SQL at line {node.lineno}"
            text = _literal_sql(node.args[0])
            assert text is not None, f"non-literal SQL at line {node.lineno}"
            statements.append(" ".join(text.split()).upper())

    assert statements, "no SQL found; the AST walk is not seeing the adapter"
    for statement in statements:
        assert statement.startswith(
            ("SELECT ", "PRAGMA QUERY_ONLY", "BEGIN", "ROLLBACK")
        ), statement
        if statement.startswith("BEGIN"):
            assert statement in ("BEGIN", "BEGIN DEFERRED"), statement
        for forbidden in (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "REPLACE ",
            "CREATE ",
            "DROP ",
            "ALTER ",
            "RECOMMENDATION_SCORE",
            "RANK_POSITION",
        ):
            assert forbidden not in statement, (forbidden, statement)
    # Nothing commits, and nothing re-orders what was read.
    for forbidden in ("commit", "executescript", "executemany", "sorted", "sort"):
        assert forbidden not in called, forbidden
