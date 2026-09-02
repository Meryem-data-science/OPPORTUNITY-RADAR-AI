"""Integration tests for the audited, immutable Priority API."""

from datetime import date
from hashlib import sha256
from types import SimpleNamespace

from fastapi.testclient import TestClient

import services.api.priority as priority_api
from services.api.main import app
from services.api.priority import PUBLIC_PRIORITY_ERROR, PriorityOpportunityResponse
from services.collector.matching import MatchLane
from services.eligibility import GlobalStatus
from services.priority import (
    PriorityAssessmentReadModel,
    PriorityCategory,
    PriorityProfileAuditStatus,
    PriorityProfileReadStatus,
    audit_current_priority,
    sync_priority,
)
from tests.integration.test_priority_persistence_sqlite import DAY, priority_fixture


def configure(monkeypatch, path, profile):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile))


def test_not_synced_is_public_and_read_only(tmp_path, monkeypatch):
    connection, identity, _, _ = priority_fixture(tmp_path)
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)
    before = sha256(path.read_bytes()).hexdigest()
    response = TestClient(app).get("/api/priority")
    assert (
        response.status_code == 200
        and response.json()["status"] == "NOT_SYNCED"
        and response.json()["current_run"] is None
    )
    assert sha256(path.read_bytes()).hexdigest() == before


def test_ready_exposes_complete_snapshot_order_and_payload(tmp_path, monkeypatch):
    connection, identity, ids, _ = priority_fixture(tmp_path, opportunities=3)
    sync_priority(connection, identity.profile_id, DAY)
    connection.execute(
        "UPDATE opportunities SET published_at='1999-01-01',deadline='1999-01-02' WHERE id=?",
        (ids[0],),
    )
    connection.commit()
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)
    before = sha256(path.read_bytes()).hexdigest()
    response = TestClient(app).get("/api/priority")
    assert response.status_code == 200
    body = response.json()
    run = body["current_run"]
    assert body["status"] == "READY" and body["integrity"] == {"ok": True}
    assert set(run["category_counts"]) == {
        "URGENT",
        "HIGH",
        "MEDIUM",
        "LOW",
        "IGNORE",
        "NONE",
    }
    assert (
        len(run["items"])
        == run["assessment_count"]
        == sum(run["category_counts"].values())
        == 3
    )
    assert run["items"] == sorted(run["items"], key=lambda x: x["opportunity_id"])
    first = next(x for x in run["items"] if x["opportunity_id"] == ids[0])
    assert (
        first["priority"]["published_at"] is None
        and first["priority"]["deadline"] is None
    )
    assert (
        first["priority"]["reason_codes"]
        == first["priority"]["explanation"]["result"]["reason_codes"]
    )
    assert first["opportunity"]["original_url"] == "https://example.invalid/0"
    assert sha256(path.read_bytes()).hexdigest() == before


def test_invalid_or_missing_profile_and_unknown_profile_fail_closed(
    tmp_path, monkeypatch
):
    connection, identity, _, _ = priority_fixture(tmp_path)
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)
    for value in (None, "01", "0", "true"):
        if value is None:
            monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID")
        else:
            monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", value)
        response = TestClient(app).get("/api/priority")
        assert response.status_code == 503 and response.json() == {
            "detail": PUBLIC_PRIORITY_ERROR
        }
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", "9999")
    assert TestClient(app).get("/api/priority").status_code == 503


def test_corrupt_priority_audit_fails_closed(tmp_path, monkeypatch):
    connection, identity, _, _ = priority_fixture(tmp_path)
    run = sync_priority(connection, identity.profile_id, DAY)
    path = tmp_path / "priority-persistence.db"
    configure(monkeypatch, path, identity.profile_id)
    connection.execute(
        "UPDATE priority_assessments SET assessment_fingerprint=? WHERE run_id=?",
        ("0" * 64, run.run_id),
    )
    connection.commit()
    response = TestClient(app).get("/api/priority")
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_PRIORITY_ERROR}
    connection.close()


def test_missing_opportunity_metadata_fails_closed_with_valid_priority_audit(
    tmp_path, monkeypatch
):
    connection, identity, ids, _ = priority_fixture(tmp_path)
    sync_priority(connection, identity.profile_id, DAY)
    path = tmp_path / "priority-persistence.db"
    configure(monkeypatch, path, identity.profile_id)

    assert (
        audit_current_priority(connection, identity.profile_id).status
        is PriorityProfileAuditStatus.READY
    )
    connection.commit()
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM opportunities WHERE id = ?", (ids[0],))
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    # Opportunity metadata is deliberately outside the cryptographic Priority
    # snapshot, so its removal must not make the Priority audit itself fail.
    assert (
        audit_current_priority(connection, identity.profile_id).status
        is PriorityProfileAuditStatus.READY
    )
    response = TestClient(app).get("/api/priority")
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_PRIORITY_ERROR}
    connection.close()


def test_ready_items_follow_category_score_and_opportunity_order(tmp_path, monkeypatch):
    connection, identity, _, _ = priority_fixture(tmp_path)
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)

    specifications = (
        (12, PriorityCategory.LOW, 0.99),
        (8, PriorityCategory.MEDIUM, 0.8),
        (3, PriorityCategory.MEDIUM, 0.8),
        (10, PriorityCategory.MEDIUM, 0.6),
        (6, PriorityCategory.MEDIUM, 0.0),
        (1, PriorityCategory.MEDIUM, None),
        (7, PriorityCategory.IGNORE, 0.9),
        (20, None, 1.0),
        (4, PriorityCategory.HIGH, 0.1),
        (2, PriorityCategory.URGENT, 0.05),
    )
    assessments = tuple(
        PriorityAssessmentReadModel(
            opportunity_id=opportunity_id,
            priority_score=score,
            priority_evidence_coverage=0.75,
            priority_category=category,
            eligibility_status=GlobalStatus.UNKNOWN,
            matching_lane=MatchLane.UNCERTAIN,
            assessment_fingerprint=f"{opportunity_id:x}".zfill(64),
            created_at="2026-09-02T00:00:00Z",
            assessment_payload={
                "dates": {"published_at": None, "deadline": None},
                "result": {
                    "deadline_status": "MISSING",
                    "reason_codes": [],
                    "hard_blocker": None,
                    "coverage_guardrail_applied": False,
                    "outside_preferences_cap_applied": False,
                    "urgent_promotion_applied": False,
                },
            },
        )
        for opportunity_id, category, score in specifications
    )
    run = SimpleNamespace(
        run_id=1,
        matching_run_id=1,
        evaluation_date=date(2026, 9, 2),
        created_at="2026-09-02T00:00:00Z",
        assessment_count=len(assessments),
        persistence_version="priority-persistence-v1",
        input_assembly_version="priority-input-assembly-v1",
        priority_engine_version="priority-engine-v1",
        priority_rules_version="priority-rules-v1",
        freshness_version="priority-freshness-v1",
        quality_version="priority-quality-v1",
        matching_run_fingerprint="a" * 64,
        run_fingerprint="b" * 64,
        assessments=assessments,
    )
    current = SimpleNamespace(
        profile_id=identity.profile_id,
        status=PriorityProfileReadStatus.READY,
        persistence_version=run.persistence_version,
        input_assembly_version=run.input_assembly_version,
        history_count=1,
        current_run=run,
    )
    audit = SimpleNamespace(status=PriorityProfileAuditStatus.READY, issues=())
    metadata = {
        opportunity_id: PriorityOpportunityResponse(
            id=opportunity_id,
            canonical_title=f"Role {opportunity_id}",
            organization="Org",
            location=None,
            last_seen_at="2026-09-02T00:00:00Z",
            original_url=f"https://example.invalid/{opportunity_id}",
        )
        for opportunity_id, _, _ in specifications
    }
    monkeypatch.setattr(priority_api, "read_current_priority", lambda *_: current)
    monkeypatch.setattr(priority_api, "audit_current_priority", lambda *_: audit)
    monkeypatch.setattr(priority_api, "_metadata", lambda *_: metadata)

    response = TestClient(app).get("/api/priority")

    assert response.status_code == 200
    items = response.json()["current_run"]["items"]
    assert [item["opportunity_id"] for item in items] == [
        2,  # URGENT precedes every other category despite its lower score.
        4,  # HIGH precedes MEDIUM.
        3,  # Same MEDIUM score: lower opportunity_id first.
        8,
        10,  # Lower numeric MEDIUM score follows higher scores.
        6,  # Numeric 0.0 is still ordered before None.
        1,  # None follows every numeric score in its category.
        12,  # LOW precedes IGNORE despite IGNORE's lower/higher score irrelevance.
        7,
        20,  # API-only NONE representation is last.
    ]
    assert items[-1]["priority"]["category"] is None
