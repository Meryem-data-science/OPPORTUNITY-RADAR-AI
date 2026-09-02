"""Integration coverage for the audited, immutable Portfolio API."""

from hashlib import sha256

from fastapi.testclient import TestClient

from services.api.main import app
from services.api.portfolio import PUBLIC_PORTFOLIO_ERROR
from services.portfolio.audit import audit_current_portfolio
from tests.integration.test_portfolio_persistence_sqlite import (
    persist,
    portfolio_fixture,
)


def configure(monkeypatch, path, profile):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile))


def test_not_synced_is_public_and_byte_read_only(tmp_path, monkeypatch):
    connection, identity, _, _ = portfolio_fixture(tmp_path)
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)
    before = sha256(path.read_bytes()).hexdigest()
    response = TestClient(app).get("/api/portfolio")
    assert response.status_code == 200
    assert response.json()["status"] == "NOT_SYNCED"
    assert response.json()["current_run"] is None
    assert response.json()["integrity"] == {"ok": True}
    assert sha256(path.read_bytes()).hexdigest() == before


def test_ready_exposes_all_persisted_items_payload_provenance_and_urls(
    tmp_path, monkeypatch
):
    connection, identity, ids, arguments = portfolio_fixture(tmp_path, opportunities=3)
    stored = persist(connection, arguments)
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)
    before = sha256(path.read_bytes()).hexdigest()
    response = TestClient(app).get("/api/portfolio")
    assert response.status_code == 200
    body, run = response.json(), response.json()["current_run"]
    assert body["status"] == "READY" and body["integrity"] == {"ok": True}
    assert run["run_id"] == stored.run_id
    assert run["priority_run_id"] == arguments["priority_run_id"]
    assert run["matching_run_id"] == arguments["matching_run_id"]
    assert len(run["items"]) == run["assessment_count"] == 3
    assert [item["opportunity_id"] for item in run["items"]] == sorted(ids)
    assert run["included_count"] + run["excluded_count"] == 3
    assert sum(run["bucket_counts"].values()) == run["included_count"]
    first = run["items"][0]
    assert first["opportunity"]["original_url"] == "https://example.invalid/0"
    assert (
        first["portfolio"]["reason_codes"]
        == first["portfolio"]["explanation"]["result"]["reason_codes"]
    )
    assert sha256(path.read_bytes()).hexdigest() == before


def test_corrupt_audit_fails_closed(tmp_path, monkeypatch):
    connection, identity, ids, arguments = portfolio_fixture(tmp_path)
    stored = persist(connection, arguments)
    path = tmp_path / "priority-persistence.db"
    configure(monkeypatch, path, identity.profile_id)
    connection.execute(
        "UPDATE portfolio_assessments SET assessment_fingerprint=? WHERE run_id=?",
        ("0" * 64, stored.run_id),
    )
    connection.commit()
    response = TestClient(app).get("/api/portfolio")
    assert response.status_code == 503 and response.json() == {
        "detail": PUBLIC_PORTFOLIO_ERROR
    }
    connection.close()


def test_missing_metadata_fails_closed(tmp_path, monkeypatch):
    connection, identity, ids, arguments = portfolio_fixture(tmp_path)
    persist(connection, arguments)
    path = tmp_path / "priority-persistence.db"
    configure(monkeypatch, path, identity.profile_id)
    assert (
        audit_current_portfolio(connection, identity.profile_id).status.value == "READY"
    )
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DELETE FROM opportunities WHERE id=?", (ids[0],))
    connection.commit()
    assert TestClient(app).get("/api/portfolio").status_code == 503
    connection.close()


def test_invalid_profile_and_backend_fail_closed(tmp_path, monkeypatch):
    connection, identity, _, _ = portfolio_fixture(tmp_path)
    path = tmp_path / "priority-persistence.db"
    connection.close()
    configure(monkeypatch, path, identity.profile_id)
    for value in (None, "01", "0", "true"):
        if value is None:
            monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID")
        else:
            monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", value)
        response = TestClient(app).get("/api/portfolio")
        assert response.status_code == 503 and response.json() == {
            "detail": PUBLIC_PORTFOLIO_ERROR
        }
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(identity.profile_id))
    monkeypatch.setenv("DATABASE_BACKEND", "turso")
    assert TestClient(app).get("/api/portfolio").status_code == 503


def test_invalid_persisted_reason_codes_fail_closed(tmp_path, monkeypatch):
    connection, identity, _, arguments = portfolio_fixture(tmp_path)
    stored = persist(connection, arguments)
    path = tmp_path / "priority-persistence.db"
    configure(monkeypatch, path, identity.profile_id)
    connection.execute(
        "UPDATE portfolio_assessments SET assessment_payload_json='{}' WHERE run_id=?",
        (stored.run_id,),
    )
    connection.commit()
    assert TestClient(app).get("/api/portfolio").status_code == 503
    connection.close()
