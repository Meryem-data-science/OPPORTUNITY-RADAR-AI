"""Integration coverage for the Web Push opt-in API surface."""

import base64

import pytest
from fastapi.testclient import TestClient

from services.api.main import app
from services.api.push import (
    PUBLIC_PUSH_CONFLICT_ERROR,
    PUBLIC_PUSH_ERROR,
    PUBLIC_PUSH_REQUEST_ERROR,
    VAPID_PUBLIC_KEY_VARIABLE,
)
from services.notifications import subscribe_push_subscription
from tests.integration.test_push_subscriptions_sqlite import (
    AUTH,
    ENDPOINT,
    MALFORMED_AUTH,
    MALFORMED_P256DH,
    P256DH,
    push_fixture,
)

VAPID_PUBLIC_KEY = (
    base64.urlsafe_b64encode(b"\x04" + bytes(range(64))).decode().rstrip("=")
)


def configure(monkeypatch, path, profile_id):
    monkeypatch.setenv("OPPORTUNITY_RADAR_ENV", "test")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", str(profile_id))
    monkeypatch.delenv(VAPID_PUBLIC_KEY_VARIABLE, raising=False)


def prepared(tmp_path, monkeypatch):
    connection, profile_id, other_profile_id = push_fixture(tmp_path)
    path = tmp_path / "push-subscriptions.db"
    configure(monkeypatch, path, profile_id)
    return connection, profile_id, other_profile_id, path


def body(endpoint=ENDPOINT, p256dh=P256DH, auth=AUTH):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


def stored(connection):
    return connection.execute(
        "SELECT profile_id, endpoint, status FROM push_subscriptions ORDER BY id"
    ).fetchall()


def test_config_reports_absent_present_and_malformed_keys(tmp_path, monkeypatch):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    connection.close()
    client = TestClient(app)

    response = client.get("/api/push/config")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "vapid_public_key": None}

    monkeypatch.setenv(VAPID_PUBLIC_KEY_VARIABLE, "   ")
    assert client.get("/api/push/config").json() == {
        "configured": False,
        "vapid_public_key": None,
    }

    for malformed in (
        "not-a-key",
        VAPID_PUBLIC_KEY[:-4],
        "A" * 200,
        VAPID_PUBLIC_KEY + "==",
    ):
        monkeypatch.setenv(VAPID_PUBLIC_KEY_VARIABLE, malformed)
        assert client.get("/api/push/config").json() == {
            "configured": False,
            "vapid_public_key": None,
        }

    monkeypatch.setenv(VAPID_PUBLIC_KEY_VARIABLE, VAPID_PUBLIC_KEY)
    assert client.get("/api/push/config").json() == {
        "configured": True,
        "vapid_public_key": VAPID_PUBLIC_KEY,
    }


def test_config_never_exposes_a_private_key_variable(tmp_path, monkeypatch):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    connection.close()
    monkeypatch.setenv(VAPID_PUBLIC_KEY_VARIABLE, VAPID_PUBLIC_KEY)
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key-must-never-be-served")
    payload = TestClient(app).get("/api/push/config")
    assert set(payload.json()) == {"configured", "vapid_public_key"}
    assert "private-key-must-never-be-served" not in payload.text


def test_subscribe_persists_once_and_is_idempotent(tmp_path, monkeypatch):
    connection, profile_id, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        response = client.post("/api/push/subscriptions", json=body())
        assert response.status_code == 201
        assert response.json() == {"status": "ACTIVE"}
        assert stored(connection) == [(profile_id, ENDPOINT, "ACTIVE")]

        again = client.post("/api/push/subscriptions", json=body())
        assert again.status_code == 201 and again.json() == {"status": "ACTIVE"}
        assert stored(connection) == [(profile_id, ENDPOINT, "ACTIVE")]
    finally:
        connection.close()


def test_subscribe_response_never_echoes_the_credentials(tmp_path, monkeypatch):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post("/api/push/subscriptions", json=body())
        assert set(response.json()) == {"status"}
        for secret in (ENDPOINT, P256DH, AUTH):
            assert secret not in response.text
    finally:
        connection.close()


def test_unsubscribe_revokes_and_is_idempotent(tmp_path, monkeypatch):
    connection, profile_id, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        client.post("/api/push/subscriptions", json=body())
        response = client.request(
            "DELETE", "/api/push/subscriptions", json={"endpoint": ENDPOINT}
        )
        assert response.status_code == 200 and response.json() == {"status": "REVOKED"}
        assert stored(connection) == [(profile_id, ENDPOINT, "REVOKED")]

        repeat = client.request(
            "DELETE", "/api/push/subscriptions", json={"endpoint": ENDPOINT}
        )
        assert repeat.status_code == 200 and repeat.json() == {"status": "REVOKED"}
        assert stored(connection) == [(profile_id, ENDPOINT, "REVOKED")]

        unknown = client.request(
            "DELETE",
            "/api/push/subscriptions",
            json={"endpoint": "https://push.example.invalid/unknown"},
        )
        assert unknown.status_code == 200 and unknown.json() == {"status": "REVOKED"}
        assert stored(connection) == [(profile_id, ENDPOINT, "REVOKED")]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"endpoint": ENDPOINT},
        {"keys": {"p256dh": P256DH, "auth": AUTH}},
        {"endpoint": "", "keys": {"p256dh": P256DH, "auth": AUTH}},
        {
            "endpoint": "http://push.example.invalid/x",
            "keys": {"p256dh": P256DH, "auth": AUTH},
        },
        {"endpoint": ENDPOINT, "keys": {"p256dh": P256DH}},
        {"endpoint": ENDPOINT, "keys": {"p256dh": P256DH, "auth": AUTH, "extra": "x"}},
        {"endpoint": ENDPOINT, "keys": {"p256dh": "", "auth": AUTH}},
        {"endpoint": ENDPOINT, "keys": [P256DH, AUTH]},
        {
            "endpoint": ENDPOINT,
            "keys": {"p256dh": P256DH, "auth": AUTH},
            "profile_id": 2,
        },
        {
            "endpoint": ENDPOINT,
            "keys": {"p256dh": P256DH, "auth": AUTH},
            "expirationTime": None,
        },
        {
            "endpoint": ["https://push.example.invalid/x"],
            "keys": {"p256dh": P256DH, "auth": AUTH},
        },
        [ENDPOINT],
        "endpoint",
    ],
)
def test_invalid_subscribe_bodies_fail_closed(tmp_path, monkeypatch, payload):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post("/api/push/subscriptions", json=payload)
        assert response.status_code == 400
        assert response.json() == {"detail": PUBLIC_PUSH_REQUEST_ERROR}
        assert stored(connection) == []
    finally:
        connection.close()


def test_a_body_that_is_not_json_fails_closed(tmp_path, monkeypatch):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post(
            "/api/push/subscriptions",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json() == {"detail": PUBLIC_PUSH_REQUEST_ERROR}
        assert stored(connection) == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"endpoint": ""},
        {"endpoint": "http://push.example.invalid/x"},
        {"endpoint": ENDPOINT, "keys": {"p256dh": P256DH, "auth": AUTH}},
        {"endpoint": None},
    ],
)
def test_invalid_unsubscribe_bodies_fail_closed(tmp_path, monkeypatch, payload):
    connection, profile_id, _, _ = prepared(tmp_path, monkeypatch)
    try:
        subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        response = TestClient(app).request(
            "DELETE", "/api/push/subscriptions", json=payload
        )
        assert response.status_code == 400
        assert response.json() == {"detail": PUBLIC_PUSH_REQUEST_ERROR}
        assert stored(connection) == [(profile_id, ENDPOINT, "ACTIVE")]
    finally:
        connection.close()


def test_an_endpoint_owned_by_another_profile_is_refused(tmp_path, monkeypatch):
    connection, _, other_profile_id, path = prepared(tmp_path, monkeypatch)
    try:
        subscribe_push_subscription(
            connection,
            profile_id=other_profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        client = TestClient(app)
        response = client.post("/api/push/subscriptions", json=body())
        assert response.status_code == 409
        assert response.json() == {"detail": PUBLIC_PUSH_CONFLICT_ERROR}
        revoke = client.request(
            "DELETE", "/api/push/subscriptions", json={"endpoint": ENDPOINT}
        )
        assert revoke.status_code == 409
        assert stored(connection) == [(other_profile_id, ENDPOINT, "ACTIVE")]
    finally:
        connection.close()


@pytest.mark.parametrize("profile", ["", "0", "-1", "1.0", "abc", "01"])
def test_a_misconfigured_profile_is_unavailable(tmp_path, monkeypatch, profile):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("OPPORTUNITY_RADAR_PROFILE_ID", profile)
        response = TestClient(app).post("/api/push/subscriptions", json=body())
        assert response.status_code == 503
        assert response.json() == {"detail": PUBLIC_PUSH_ERROR}
        assert stored(connection) == []
    finally:
        connection.close()


def test_a_missing_database_is_unavailable_and_is_never_created(tmp_path, monkeypatch):
    connection, profile_id, _, _ = prepared(tmp_path, monkeypatch)
    connection.close()
    missing = tmp_path / "absent" / "push.db"
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(missing))
    response = TestClient(app).post("/api/push/subscriptions", json=body())
    assert response.status_code == 503
    assert response.json() == {"detail": PUBLIC_PUSH_ERROR}
    assert not missing.exists() and not missing.parent.exists()


def test_a_database_without_migration_0018_is_unavailable(tmp_path, monkeypatch):
    connection, profile_id, _, path = prepared(tmp_path, monkeypatch)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version = '0018'")
        connection.commit()
        response = TestClient(app).post("/api/push/subscriptions", json=body())
        assert response.status_code == 503
        assert response.json() == {"detail": PUBLIC_PUSH_ERROR}
    finally:
        connection.close()


def test_the_push_surface_sends_no_notification(tmp_path, monkeypatch):
    """The push surface stores an opt-in and nothing else: no event, no delivery.

    Phase 5.3B owns the notification policy tables and Phase 5.3C1 the delivery
    ones. Opting in and out must not put anything in either set, and must not
    deliver anything either.
    """
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        client = TestClient(app)
        client.post("/api/push/subscriptions", json=body())
        client.request("DELETE", "/api/push/subscriptions", json={"endpoint": ENDPOINT})
        notification_tables = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if "notification" in row[0] or "outbox" in row[0]
        )
        assert notification_tables == [
            "notification_delivery_batches",
            "notification_delivery_targets",
            "notification_events",
            "notification_outbox",
            "notification_policy_state",
        ]
        assert all(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in notification_tables
        )
    finally:
        connection.close()


@pytest.mark.parametrize("p256dh", MALFORMED_P256DH)
def test_a_malformed_p256dh_is_refused_before_any_write(tmp_path, monkeypatch, p256dh):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post(
            "/api/push/subscriptions", json=body(p256dh=p256dh)
        )
        assert response.status_code == 400
        assert response.json() == {"detail": PUBLIC_PUSH_REQUEST_ERROR}
        assert stored(connection) == []
    finally:
        connection.close()


@pytest.mark.parametrize("auth", MALFORMED_AUTH)
def test_a_malformed_auth_secret_is_refused_before_any_write(
    tmp_path, monkeypatch, auth
):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    try:
        response = TestClient(app).post("/api/push/subscriptions", json=body(auth=auth))
        assert response.status_code == 400
        assert response.json() == {"detail": PUBLIC_PUSH_REQUEST_ERROR}
        assert stored(connection) == []
    finally:
        connection.close()


def test_a_refused_credential_never_appears_in_the_response_or_the_logs(
    tmp_path, monkeypatch, caplog
):
    connection, _, _, _ = prepared(tmp_path, monkeypatch)
    secret = "BN" + "s3cr3tvalue" + "a" * 74
    try:
        with caplog.at_level("DEBUG"):
            response = TestClient(app).post(
                "/api/push/subscriptions",
                json=body(p256dh=secret, auth="c" * 21),
            )
        assert response.status_code == 400
        emitted = "\n".join(
            [record.getMessage() for record in caplog.records]
            + [str(record.__dict__) for record in caplog.records]
        )
        for value in (secret, "s3cr3tvalue", "c" * 21):
            assert value not in response.text
            assert value not in emitted
        assert stored(connection) == []
    finally:
        connection.close()
