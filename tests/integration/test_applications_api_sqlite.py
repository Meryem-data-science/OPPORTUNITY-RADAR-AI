"""Integration coverage for the /api/applications surface, against real SQLite.

The API is exercised end to end: a real migrated database, a real profile, a
real opportunity, and FastAPI in front of them. What is asserted is what a
browser would actually receive — the status code, the body, and the state the
database is left in — rather than the internals underneath.
"""

import json

import pytest
from fastapi.testclient import TestClient

from services.api.applications import (
    PUBLIC_APPLICATION_ERROR,
    PUBLIC_APPLICATION_NOT_FOUND,
    PUBLIC_APPLICATION_REQUEST_ERROR,
    PUBLIC_OPPORTUNITY_NOT_FOUND,
)
from services.api.main import app
from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.digital_twin.repository import ensure_user_profile
from tests.integration.test_application_tracking_migration_sqlite import (
    migrations_below,
    opportunity,
    tracking_fixture,
)

DATABASE_NAME = "application-tracking.db"


def configure(monkeypatch, path, profile_id):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile_id))


def prepared(tmp_path, monkeypatch):
    connection, profile_id, other_profile_id, opportunity_id = tracking_fixture(
        tmp_path, DATABASE_NAME
    )
    path = tmp_path / DATABASE_NAME
    configure(monkeypatch, path, profile_id)
    return connection, profile_id, other_profile_id, opportunity_id, path


def events(connection, application_id):
    return connection.execute(
        "SELECT event_type, from_status, to_status FROM application_events"
        " WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()


def test_the_whole_tracking_flow_works_through_the_api(tmp_path, monkeypatch):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)

        assert client.get("/api/applications").json()["items"] == []

        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["created"] is True and body["changed"] is True
        application = body["application"]
        application_id = application["id"]
        assert application["status"] == "SAVED"
        assert application["submitted_at"] is None
        assert application["opportunity"]["original_url"] == (
            "https://boards.example.invalid/jobs/1"
        )
        assert [event["event_type"] for event in application["events"]] == [
            "APPLICATION_CREATED"
        ]

        repeated = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["created"] is False
        assert repeated.json()["changed"] is False

        prepare = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "PREPARE"},
        )
        assert prepare.status_code == 200
        assert prepare.json()["changed"] is True
        assert prepare.json()["application"]["status"] == "PREPARING"

        submitted = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "MARK_SUBMITTED"},
        ).json()["application"]
        assert submitted["status"] == "SUBMITTED"
        assert submitted["submitted_at"] is not None

        again = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "MARK_SUBMITTED"},
        ).json()
        assert again["changed"] is False
        assert again["application"]["submitted_at"] == submitted["submitted_at"]

        interview = client.patch(
            f"/api/applications/{application_id}/status",
            json={"status": "INTERVIEW"},
        )
        assert interview.status_code == 200
        assert interview.json()["application"]["status"] == "INTERVIEW"
        assert interview.json()["application"]["submitted_at"] == submitted["submitted_at"]

        tracked = client.patch(
            f"/api/applications/{application_id}",
            json={
                "notes": "Entretien prévu",
                "next_action": "Préparer les questions",
                "followup_date": "2026-04-01",
            },
        )
        assert tracked.status_code == 200
        assert tracked.json()["application"]["notes"] == "Entretien prévu"
        assert tracked.json()["application"]["followup_date"] == "2026-04-01"

        listing = client.get("/api/applications").json()
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == application_id
        assert listing["items"][0]["status"] == "INTERVIEW"

        detail = client.get(f"/api/applications/{application_id}").json()
        assert [event["event_type"] for event in detail["events"]] == [
            "APPLICATION_CREATED",
            "STATUS_CHANGED",
            "STATUS_CHANGED",
            "STATUS_CHANGED",
            "TRACKING_UPDATED",
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM applications"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_a_body_that_names_a_profile_is_refused(tmp_path, monkeypatch):
    connection, _, other_profile_id, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/applications",
            json={
                "opportunity_id": opportunity_id,
                "action": "SAVE",
                "profile_id": other_profile_id,
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == PUBLIC_APPLICATION_REQUEST_ERROR
        assert connection.execute(
            "SELECT COUNT(*) FROM applications"
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"opportunity_id": 1},
        {"action": "SAVE"},
        {"opportunity_id": 1, "action": "APPLY"},
        {"opportunity_id": 1, "action": "READY"},
        {"opportunity_id": 1, "action": ""},
        {"opportunity_id": 1, "action": None},
        {"opportunity_id": 0, "action": "SAVE"},
        {"opportunity_id": -1, "action": "SAVE"},
        {"opportunity_id": True, "action": "SAVE"},
        {"opportunity_id": "1", "action": "SAVE"},
        {"opportunity_id": 1.5, "action": "SAVE"},
        [],
        "SAVE",
    ],
)
def test_invalid_create_bodies_fail_closed(tmp_path, monkeypatch, body):
    connection, _, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        response = client.post("/api/applications", json=body)
        assert response.status_code == 400
        assert connection.execute(
            "SELECT COUNT(*) FROM applications"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_a_body_that_is_not_json_is_refused(tmp_path, monkeypatch):
    connection, _, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/applications",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == PUBLIC_APPLICATION_REQUEST_ERROR
    finally:
        connection.close()


def test_an_opportunity_that_does_not_exist_is_a_404(tmp_path, monkeypatch):
    connection, _, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/applications", json={"opportunity_id": 999, "action": "SAVE"}
        )
        assert response.status_code == 404
        assert response.json()["detail"] == PUBLIC_OPPORTUNITY_NOT_FOUND
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE id = 999"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_an_application_that_does_not_exist_is_a_404(tmp_path, monkeypatch):
    connection, _, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        for response in (
            client.get("/api/applications/999"),
            client.patch("/api/applications/999/status", json={"status": "SAVED"}),
            client.patch("/api/applications/999", json={"notes": "x"}),
        ):
            assert response.status_code == 404
            assert response.json()["detail"] == PUBLIC_APPLICATION_NOT_FOUND
    finally:
        connection.close()


def test_another_profiles_application_is_a_404(tmp_path, monkeypatch):
    connection, _, other_profile_id, opportunity_id, path = prepared(
        tmp_path, monkeypatch
    )
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]

        # The same server, now serving the other profile: the candidature it
        # does not own is simply not there.
        configure(monkeypatch, path, other_profile_id)
        assert client.get("/api/applications").json()["items"] == []
        assert client.get(f"/api/applications/{created['id']}").status_code == 404
        assert (
            client.patch(
                f"/api/applications/{created['id']}/status",
                json={"status": "PREPARING"},
            ).status_code
            == 404
        )
        assert (
            connection.execute(
                "SELECT status FROM applications WHERE id = ?", (created["id"],)
            ).fetchone()[0]
            == "SAVED"
        )
    finally:
        connection.close()


@pytest.mark.parametrize("refused", ["READY", "DISCOVERED"])
def test_ready_and_discovered_are_refused_by_the_api(tmp_path, monkeypatch, refused):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "PREPARE"},
        ).json()["application"]
        response = client.patch(
            f"/api/applications/{created['id']}/status", json={"status": refused}
        )
        assert response.status_code == 400
        assert refused in response.json()["detail"]
        assert events(connection, created["id"]) == [
            ("APPLICATION_CREATED", None, "PREPARING")
        ]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": "APPLIED"},
        {"status": ""},
        {"status": None},
        {"status": 3},
        {"status": "SAVED", "notes": "x"},
        {"application_status": "SAVED"},
    ],
)
def test_invalid_status_bodies_fail_closed(tmp_path, monkeypatch, body):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]
        response = client.patch(
            f"/api/applications/{created['id']}/status", json=body
        )
        assert response.status_code == 400
        assert len(events(connection, created["id"])) == 1
    finally:
        connection.close()


def test_a_status_that_contradicts_the_candidature_is_a_409(tmp_path, monkeypatch):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]

        never_sent = client.patch(
            f"/api/applications/{created['id']}/status", json={"status": "INTERVIEW"}
        )
        assert never_sent.status_code == 409

        client.patch(
            f"/api/applications/{created['id']}/status", json={"status": "SUBMITTED"}
        )
        regression = client.patch(
            f"/api/applications/{created['id']}/status", json={"status": "SAVED"}
        )
        assert regression.status_code == 409
        assert (
            connection.execute(
                "SELECT status FROM applications WHERE id = ?", (created["id"],)
            ).fetchone()[0]
            == "SUBMITTED"
        )
    finally:
        connection.close()


def test_an_action_on_a_concluded_candidature_is_a_409(tmp_path, monkeypatch):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "MARK_SUBMITTED"},
        ).json()["application"]
        client.patch(
            f"/api/applications/{created['id']}/status", json={"status": "REJECTED"}
        )
        response = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        )
        assert response.status_code == 409
        assert (
            connection.execute(
                "SELECT status FROM applications WHERE id = ?", (created["id"],)
            ).fetchone()[0]
            == "REJECTED"
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": "SAVED"},
        {"submitted_at": "2026-01-01 00:00:00"},
        {"profile_id": 2},
        {"opportunity_id": 2},
        {"id": 2},
        {"created_at": "2026-01-01 00:00:00"},
        {"notes": "ok", "status": "SAVED"},
        {"followup_date": "01/04/2026"},
        {"followup_date": "2026-04-31"},
        {"followup_date": 20260401},
        {"notes": "n" * 4001},
        {"next_action": "a" * 501},
        {"notes": 7},
    ],
)
def test_invalid_tracking_bodies_fail_closed(tmp_path, monkeypatch, body):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]
        response = client.patch(f"/api/applications/{created['id']}", json=body)
        assert response.status_code == 400
        stored = connection.execute(
            "SELECT status, notes, next_action, followup_date, submitted_at"
            " FROM applications WHERE id = ?",
            (created["id"],),
        ).fetchone()
        assert stored == ("SAVED", None, None, None, None)
        assert len(events(connection, created["id"])) == 1
    finally:
        connection.close()


def test_an_identical_tracking_patch_writes_nothing(tmp_path, monkeypatch):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]
        payload = {"notes": "Relancer", "followup_date": "2026-04-01"}
        first = client.patch(f"/api/applications/{created['id']}", json=payload).json()
        second = client.patch(f"/api/applications/{created['id']}", json=payload).json()
        assert first["changed"] is True
        assert second["changed"] is False
        assert second["application"]["updated_at"] == first["application"]["updated_at"]
        assert [event[0] for event in events(connection, created["id"])] == [
            "APPLICATION_CREATED",
            "TRACKING_UPDATED",
        ]
    finally:
        connection.close()


def test_a_tracking_field_can_be_cleared_through_the_api(tmp_path, monkeypatch):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]
        client.patch(
            f"/api/applications/{created['id']}",
            json={"notes": "temporaire", "next_action": "relancer"},
        )
        cleared = client.patch(
            f"/api/applications/{created['id']}", json={"notes": None}
        ).json()["application"]
        assert cleared["notes"] is None
        assert cleared["next_action"] == "relancer"
    finally:
        connection.close()


def test_a_private_note_never_comes_back_in_an_error(tmp_path, monkeypatch, caplog):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    secret = "Salaire négocié à un montant confidentiel"
    try:
        client = TestClient(app)
        created = client.post(
            "/api/applications",
            json={"opportunity_id": opportunity_id, "action": "SAVE"},
        ).json()["application"]
        with caplog.at_level("DEBUG"):
            client.patch(f"/api/applications/{created['id']}", json={"notes": secret})
            refused = client.patch(
                f"/api/applications/{created['id']}",
                json={"notes": secret + "!", "unknown": 1},
            )
        assert refused.status_code == 400
        assert secret not in refused.text
        assert secret not in caplog.text
        # The note itself is stored; only the reporting of it is refused.
        assert (
            connection.execute(
                "SELECT notes FROM applications WHERE id = ?", (created["id"],)
            ).fetchone()[0]
            == secret
        )
    finally:
        connection.close()


def test_a_missing_database_is_unavailable_and_is_never_created(tmp_path, monkeypatch):
    connection, profile_id, _, _, _ = prepared(tmp_path, monkeypatch)
    connection.close()
    missing = tmp_path / "nested" / "absent.db"
    configure(monkeypatch, missing, profile_id)
    client = TestClient(app)
    for response in (
        client.get("/api/applications"),
        client.get("/api/applications/1"),
        client.post("/api/applications", json={"opportunity_id": 1, "action": "SAVE"}),
        client.patch("/api/applications/1/status", json={"status": "SAVED"}),
        client.patch("/api/applications/1", json={"notes": "x"}),
    ):
        assert response.status_code == 503
        assert response.json()["detail"] == PUBLIC_APPLICATION_ERROR
    assert not missing.exists()
    assert not missing.parent.exists()


def test_a_database_that_stopped_below_0023_is_refused_not_crashed(
    tmp_path, monkeypatch
):
    path = tmp_path / "below.db"
    connection = connect_database(path)
    try:
        apply_migrations(connection, migrations_below(tmp_path, "0023"))
        profile = ensure_user_profile(connection, "below@example.invalid")
        opportunity(connection)
        connection.commit()
    finally:
        connection.close()
    configure(monkeypatch, path, profile.profile.id)
    client = TestClient(app)
    for response in (
        client.get("/api/applications"),
        client.post("/api/applications", json={"opportunity_id": 1, "action": "SAVE"}),
    ):
        assert response.status_code == 503
        assert response.json()["detail"] == PUBLIC_APPLICATION_ERROR


@pytest.mark.parametrize("value", [None, "", " ", "0", "-1", "abc", "1.0", "01", "1x"])
def test_a_misconfigured_profile_is_unavailable(tmp_path, monkeypatch, value):
    connection, _, _, _, _ = prepared(tmp_path, monkeypatch)
    connection.close()
    if value is None:
        monkeypatch.delenv("OPPORTUNITY_RADAR_PROFILE_ID", raising=False)
    else:
        monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", value)
    client = TestClient(app)
    assert client.get("/api/applications").status_code == 503
    assert (
        client.post(
            "/api/applications", json={"opportunity_id": 1, "action": "SAVE"}
        ).status_code
        == 503
    )


def test_no_response_ever_carries_an_internal_message(tmp_path, monkeypatch):
    connection, _, _, opportunity_id, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        responses = [
            client.post("/api/applications", json={"opportunity_id": 9, "action": "SAVE"}),
            client.post("/api/applications", json={"bogus": 1}),
            client.get("/api/applications/77"),
        ]
        for response in responses:
            payload = json.dumps(response.json())
            for leak in ("Traceback", "sqlite3", "SELECT", "services."):
                assert leak not in payload
    finally:
        connection.close()
