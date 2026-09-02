"""Integration tests for the audited, immutable Priority API."""

from hashlib import sha256
from fastapi.testclient import TestClient
from services.api.main import app
from services.api.priority import PUBLIC_PRIORITY_ERROR
from services.priority import sync_priority
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


def test_corrupt_audit_and_missing_metadata_never_expose_partial_snapshot(
    tmp_path, monkeypatch
):
    connection, identity, ids, _ = priority_fixture(tmp_path)
    run = sync_priority(connection, identity.profile_id, DAY)
    path = tmp_path / "priority-persistence.db"
    configure(monkeypatch, path, identity.profile_id)
    connection.execute(
        "UPDATE priority_assessments SET assessment_fingerprint=? WHERE run_id=?",
        ("0" * 64, run.run_id),
    )
    connection.commit()
    assert TestClient(app).get("/api/priority").status_code == 503
    connection.rollback()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM opportunities WHERE id=?", (ids[0],))
    connection.commit()
    assert TestClient(app).get("/api/priority").status_code == 503
    connection.close()
