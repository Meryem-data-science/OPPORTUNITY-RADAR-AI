"""Integration coverage for the audited, read-only matching API surface."""

from fastapi.testclient import TestClient

from services.api.main import app
from services.api.matching import PUBLIC_MATCHING_ERROR
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.matching import set_matching_state_empty, store_matching_batch
from services.digital_twin.repository import ensure_user_profile
from tests.integration.test_matching_read_audit import add_opportunity, make_batch


def _database(tmp_path):
    path = tmp_path / "matching-api.db"
    connection = connect_database(path)
    apply_migrations(connection)
    profile_id = ensure_user_profile(
        connection, "matching-api@example.invalid"
    ).profile_id
    connection.commit()
    return path, connection, profile_id


def _configure(monkeypatch, path, profile_id):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile_id))


def test_not_synced_and_empty_are_valid_audited_states(tmp_path, monkeypatch):
    path, connection, profile_id = _database(tmp_path)
    _configure(monkeypatch, path, profile_id)
    try:
        response = TestClient(app).get("/api/matching")
        assert response.status_code == 200
        assert response.json()["status"] == "NOT_SYNCED"
        assert response.json()["current_run"] is None
        set_matching_state_empty(
            connection, profile_id, selection_version="selection-test-v1"
        )
        response = TestClient(app).get("/api/matching")
        assert response.status_code == 200
        assert response.json()["status"] == "EMPTY"
        assert response.json()["current_run"] is None
        assert response.json()["integrity"]["ok"] is True
    finally:
        connection.close()


def test_ready_preserves_cohort_order_snapshot_and_live_metadata(tmp_path, monkeypatch):
    path, connection, profile_id = _database(tmp_path)
    ids = [add_opportunity(connection, value) for value in range(2)]
    connection.execute(
        "UPDATE opportunities SET canonical_title='Live title',organization='Live org',location='Remote',is_active=0,status='hidden' WHERE id=?",
        (ids[0],),
    )
    connection.commit()
    stored = store_matching_batch(
        connection,
        profile_id,
        make_batch(profile_id, list(reversed(ids))),
        selection_version="selection-test-v1",
    )
    before = connection.total_changes
    _configure(monkeypatch, path, profile_id)
    try:
        response = TestClient(app).get("/api/matching")
        assert response.status_code == 200
        payload = response.json()
        run = payload["current_run"]
        assert payload["status"] == "READY" and run["run_id"] == stored.run_id
        assert run["assessment_count"] == len(run["items"]) == 2
        assert [item["opportunity_id"] for item in run["items"]] == sorted(ids)
        assert set(run["lane_counts"]) == {
            "PRIMARY",
            "UNCERTAIN",
            "OUTSIDE_PREFERENCES",
        }
        hidden = run["items"][0]
        assert hidden["opportunity"]["canonical_title"] == "Live title"
        assert hidden["opportunity"]["organization"] == "Live org"
        assert (
            hidden["matching"]["evidence_coverage"]
            == hidden["matching"]["explanation"]["evidence_coverage"]
        )
        assert (
            hidden["matching"]["match_quality"]
            == hidden["matching"]["explanation"]["match_quality"]
        )
        assert "required_skill" in hidden["matching"]["explanation"]
        assert connection.total_changes == before
    finally:
        connection.close()


def test_invalid_profile_configuration_and_missing_schema_are_sanitized(
    tmp_path, monkeypatch
):
    path, connection, profile_id = _database(tmp_path)
    connection.close()
    _configure(monkeypatch, path, profile_id)
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", "0")
    response = TestClient(app).get("/api/matching")
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_MATCHING_ERROR}
    assert "profile" not in response.text

    empty = tmp_path / "empty.db"
    connect_database(empty).close()
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(empty))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", "1")
    response = TestClient(app).get("/api/matching")
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_MATCHING_ERROR}
    assert "SQL" not in response.text


def test_failed_persistence_audit_returns_only_public_error(tmp_path, monkeypatch):
    path, connection, profile_id = _database(tmp_path)
    opportunity_id = add_opportunity(connection, "audit-failure")
    connection.commit()
    stored = store_matching_batch(
        connection,
        profile_id,
        make_batch(profile_id, [opportunity_id]),
        selection_version="selection-test-v1",
    )
    connection.execute(
        "UPDATE matching_assessments SET assessment_fingerprint=? WHERE run_id=?",
        ("0" * 64, stored.run_id),
    )
    connection.commit()
    connection.close()
    _configure(monkeypatch, path, profile_id)

    response = TestClient(app).get("/api/matching")

    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_MATCHING_ERROR}
    assert "FINGERPRINT" not in response.text
