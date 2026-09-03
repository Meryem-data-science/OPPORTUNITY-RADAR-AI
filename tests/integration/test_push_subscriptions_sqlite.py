"""Integration coverage for migration 0018 and push subscription persistence."""

import sqlite3

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY,
    apply_migrations,
    discover_migrations,
)
from services.digital_twin.repository import ensure_user_profile
from services.notifications import (
    AUTH_BYTE_LENGTH,
    MAX_ENDPOINT_LENGTH,
    MAX_KEY_LENGTH,
    P256DH_BYTE_LENGTH,
    PushSubscriptionError,
    PushSubscriptionOwnershipError,
    decode_base64url,
    subscribe_push_subscription,
    unsubscribe_push_subscription,
    valid_auth,
    valid_p256dh,
)

ENDPOINT = "https://push.example.invalid/subscription/abc"
OTHER_ENDPOINT = "https://push.example.invalid/subscription/def"
P256DH = "BN" + "a" * 85
AUTH = "c" * 22


def push_fixture(tmp_path):
    connection = connect_database(tmp_path / "push-subscriptions.db")
    apply_migrations(connection)
    owner = ensure_user_profile(connection, "push-owner@example.invalid")
    other = ensure_user_profile(connection, "push-other@example.invalid")
    return connection, owner.profile.id, other.profile.id


def rows(connection):
    return connection.execute(
        "SELECT id, profile_id, endpoint, p256dh, auth, status, revoked_at"
        " FROM push_subscriptions ORDER BY id"
    ).fetchall()


def test_migration_0018_creates_the_table_with_its_invariants(tmp_path):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(push_subscriptions)")
        }
        assert columns == {
            "id",
            "profile_id",
            "endpoint",
            "p256dh",
            "auth",
            "status",
            "created_at",
            "updated_at",
            "revoked_at",
        }
        assert rows(connection) == []

        def insert(**overrides):
            values = dict(
                profile_id=profile_id,
                endpoint=ENDPOINT,
                p256dh=P256DH,
                auth=AUTH,
                status="ACTIVE",
                revoked_at=None,
            )
            values.update(overrides)
            connection.execute(
                """INSERT INTO push_subscriptions
                   (profile_id, endpoint, p256dh, auth, status, revoked_at)
                   VALUES (:profile_id, :endpoint, :p256dh, :auth, :status, :revoked_at)""",
                values,
            )

        insert()
        connection.commit()
        # endpoint is unique, so the same browser endpoint cannot be stored twice
        with pytest.raises(sqlite3.IntegrityError):
            insert()
        connection.rollback()
        for overrides in (
            {"endpoint": "", "status": "ACTIVE"},
            {"endpoint": " https://push.example.invalid/x"},
            {"endpoint": "http://push.example.invalid/x"},
            {"endpoint": "https://" + "x" * MAX_ENDPOINT_LENGTH},
            {"endpoint": OTHER_ENDPOINT, "p256dh": ""},
            {"endpoint": OTHER_ENDPOINT, "auth": "  "},
            {"endpoint": OTHER_ENDPOINT, "auth": "a" * (MAX_KEY_LENGTH + 1)},
            {"endpoint": OTHER_ENDPOINT, "status": "PENDING"},
            # ACTIVE must never carry a revocation timestamp, and REVOKED must
            {"endpoint": OTHER_ENDPOINT, "revoked_at": "2026-09-03T00:00:00Z"},
            {"endpoint": OTHER_ENDPOINT, "status": "REVOKED"},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert(**overrides)
            connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO push_subscriptions
                   (profile_id, endpoint, p256dh, auth, status)
                   VALUES (?, ?, ?, ?, 'ACTIVE')""",
                (profile_id + 1000, OTHER_ENDPOINT, P256DH, AUTH),
            )
        connection.rollback()
    finally:
        connection.close()


def test_subscribe_is_idempotent_and_reactivates(tmp_path):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        first = subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        assert (first.created, first.reactivated, first.keys_updated) == (
            True,
            False,
            False,
        )
        stored = rows(connection)
        assert len(stored) == 1 and stored[0][5] == "ACTIVE" and stored[0][6] is None

        repeat = subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        assert repeat.subscription_id == first.subscription_id
        assert repeat.changed is False
        assert rows(connection) == stored

        rotated = subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh="BN" + "b" * 85,
            auth="d" * 22,
        )
        assert rotated.subscription_id == first.subscription_id
        assert (rotated.created, rotated.keys_updated) == (False, True)
        assert rows(connection)[0][3:5] == ("BN" + "b" * 85, "d" * 22)
        assert len(rows(connection)) == 1

        unsubscribe_push_subscription(
            connection, profile_id=profile_id, endpoint=ENDPOINT
        )
        assert rows(connection)[0][5] == "REVOKED"

        revived = subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        assert revived.subscription_id == first.subscription_id
        assert (revived.created, revived.reactivated) == (False, True)
        assert rows(connection)[0][5] == "ACTIVE" and rows(connection)[0][6] is None
    finally:
        connection.close()


def test_unsubscribe_is_idempotent_for_unknown_and_revoked_endpoints(tmp_path):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        unknown = unsubscribe_push_subscription(
            connection, profile_id=profile_id, endpoint=ENDPOINT
        )
        assert unknown == type(unknown)(None, False)
        assert rows(connection) == []

        subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        first = unsubscribe_push_subscription(
            connection, profile_id=profile_id, endpoint=ENDPOINT
        )
        assert first.revoked is True
        revoked_at = rows(connection)[0][6]
        assert revoked_at is not None

        second = unsubscribe_push_subscription(
            connection, profile_id=profile_id, endpoint=ENDPOINT
        )
        assert second.revoked is False
        assert second.subscription_id == first.subscription_id
        assert rows(connection)[0][6] == revoked_at
    finally:
        connection.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "   ",
        " https://push.example.invalid/x",
        "http://push.example.invalid/x",
        "ftp://push.example.invalid/x",
        "push.example.invalid/x",
        "https://",
        "https://user:secret@push.example.invalid/x",
        "https://" + "x" * MAX_ENDPOINT_LENGTH,
        None,
        42,
    ],
)
def test_invalid_endpoints_are_refused_and_store_nothing(tmp_path, endpoint):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        with pytest.raises(PushSubscriptionError):
            subscribe_push_subscription(
                connection,
                profile_id=profile_id,
                endpoint=endpoint,
                p256dh=P256DH,
                auth=AUTH,
            )
        with pytest.raises(PushSubscriptionError):
            unsubscribe_push_subscription(
                connection, profile_id=profile_id, endpoint=endpoint
            )
        assert rows(connection) == []
    finally:
        connection.close()


# Every case is a credential a browser cannot have produced: a wrong alphabet,
# padding we do not accept, or a payload that does not decode to the exact key
# size Web Push defines.
MALFORMED_P256DH = [
    "",
    " ",
    "BN" + "a" * 84,  # 64 bytes: one short of an uncompressed P-256 point
    "BN" + "a" * 86,  # 66 bytes: one too many
    "AA" + "a" * 85,  # right length, but not an uncompressed point (0x00)
    "BN" + "a" * 83,  # remainder of one: no such base64url string
    ("BN" + "a" * 83) + "==",  # padding is refused outright
    "BN+a" + "a" * 83,  # "+" and "/" belong to base64, not base64url
    "BN/a" + "a" * 83,
    "BN a" + "a" * 83,
    "BN" + "a" * 84 + "!",
    "BN" + "é" * 85,
    "c" * 22,  # a valid auth secret is not a valid public key
    "a" * (MAX_KEY_LENGTH + 1),
    None,
    7,
]

MALFORMED_AUTH = [
    "",
    " ",
    "c" * 21,  # 15 bytes
    "c" * 23,  # 17 bytes
    "c" * 21 + "=",  # padding is refused outright
    "c" * 17,  # remainder of one
    "c+cc" + "c" * 18,
    "c/cc" + "c" * 18,
    "c c" + "c" * 19,
    "cc!" + "c" * 19,
    "BN" + "a" * 85,  # a valid public key is not a valid auth secret
    "a" * (MAX_KEY_LENGTH + 1),
    None,
    7,
]


def test_the_shipped_fixtures_are_real_web_push_credentials():
    """The fixtures the rest of the suite uses decode to the real key shapes."""
    assert valid_p256dh(P256DH) and valid_auth(AUTH)
    assert len(decode_base64url(P256DH)) == P256DH_BYTE_LENGTH
    assert decode_base64url(P256DH)[0] == 0x04
    assert len(decode_base64url(AUTH)) == AUTH_BYTE_LENGTH


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a=",
        "aa==",
        "a" * 5 + "=",
        "abc!",
        "ab cd",
        "ab+cd",
        "ab/cd",
        "abcde",  # remainder of one leaves a byte that cannot exist
        "é" * 4,
        None,
        7,
    ],
)
def test_strict_base64url_refuses_malformed_input(value):
    """Padding, foreign alphabets and impossible lengths all decode to None."""
    assert decode_base64url(value) is None
    assert valid_p256dh(value) is False
    assert valid_auth(value) is False


@pytest.mark.parametrize("p256dh", MALFORMED_P256DH)
def test_malformed_p256dh_keys_are_refused_and_store_nothing(tmp_path, p256dh):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        with pytest.raises(PushSubscriptionError):
            subscribe_push_subscription(
                connection,
                profile_id=profile_id,
                endpoint=ENDPOINT,
                p256dh=p256dh,
                auth=AUTH,
            )
        assert rows(connection) == []
        assert connection.in_transaction is False
    finally:
        connection.close()


@pytest.mark.parametrize("auth", MALFORMED_AUTH)
def test_malformed_auth_secrets_are_refused_and_store_nothing(tmp_path, auth):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        with pytest.raises(PushSubscriptionError):
            subscribe_push_subscription(
                connection,
                profile_id=profile_id,
                endpoint=ENDPOINT,
                p256dh=P256DH,
                auth=auth,
            )
        assert rows(connection) == []
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_a_refused_credential_is_never_named_in_the_error(tmp_path):
    connection, profile_id, _ = push_fixture(tmp_path)
    secret = "BN" + "s3cr3t" + "a" * 79
    try:
        with pytest.raises(PushSubscriptionError) as refusal:
            subscribe_push_subscription(
                connection,
                profile_id=profile_id,
                endpoint=ENDPOINT,
                p256dh=secret,
                auth="c" * 21,
            )
        assert secret not in str(refusal.value)
        assert "s3cr3t" not in str(refusal.value)
    finally:
        connection.close()


@pytest.mark.parametrize("profile_id", [0, -1, True, "1", None, 10_000])
def test_invalid_or_unknown_profiles_are_refused(tmp_path, profile_id):
    connection, _, _ = push_fixture(tmp_path)
    try:
        with pytest.raises(PushSubscriptionError):
            subscribe_push_subscription(
                connection,
                profile_id=profile_id,
                endpoint=ENDPOINT,
                p256dh=P256DH,
                auth=AUTH,
            )
        with pytest.raises(PushSubscriptionError):
            unsubscribe_push_subscription(
                connection, profile_id=profile_id, endpoint=ENDPOINT
            )
        assert rows(connection) == []
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_an_endpoint_owned_by_another_profile_fails_closed(tmp_path):
    connection, profile_id, other_profile_id = push_fixture(tmp_path)
    try:
        subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        before = rows(connection)
        with pytest.raises(PushSubscriptionOwnershipError):
            subscribe_push_subscription(
                connection,
                profile_id=other_profile_id,
                endpoint=ENDPOINT,
                p256dh="BN" + "z" * 85,
                auth="z" * 22,
            )
        with pytest.raises(PushSubscriptionOwnershipError):
            unsubscribe_push_subscription(
                connection, profile_id=other_profile_id, endpoint=ENDPOINT
            )
        assert rows(connection) == before
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_a_failed_subscribe_rolls_its_whole_transaction_back(tmp_path, monkeypatch):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=OTHER_ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        before = rows(connection)
        # Waving the profile guard through leaves the foreign key to fail the
        # INSERT instead: the row must not survive, and the transaction the
        # service opened must not be left dangling.
        monkeypatch.setattr(
            "services.notifications.push_subscriptions._profile_exists",
            lambda connection, profile_id: True,
        )
        with pytest.raises(sqlite3.IntegrityError):
            subscribe_push_subscription(
                connection,
                profile_id=profile_id + 10_000,
                endpoint=ENDPOINT,
                p256dh=P256DH,
                auth=AUTH,
            )
        assert connection.in_transaction is False
        assert rows(connection) == before
    finally:
        connection.close()


def test_no_credential_value_reaches_the_logs(tmp_path, caplog):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        with caplog.at_level("DEBUG"):
            subscribe_push_subscription(
                connection,
                profile_id=profile_id,
                endpoint=ENDPOINT,
                p256dh=P256DH,
                auth=AUTH,
            )
            unsubscribe_push_subscription(
                connection, profile_id=profile_id, endpoint=ENDPOINT
            )
        emitted = "\n".join(
            [record.getMessage() for record in caplog.records]
            + [str(record.__dict__) for record in caplog.records]
        )
        assert caplog.records
        for secret in (ENDPOINT, P256DH, AUTH):
            assert secret not in emitted
    finally:
        connection.close()


def test_deleting_the_profile_removes_its_subscriptions(tmp_path):
    connection, profile_id, _ = push_fixture(tmp_path)
    try:
        subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        connection.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        connection.commit()
        assert rows(connection) == []
    finally:
        connection.close()


def _apply_up_to_0017(connection, tmp_path) -> list[str]:
    directory = tmp_path / "migrations-before-0018"
    directory.mkdir()
    versions = []
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version != "0018":
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            versions.append(migration.version)
    assert apply_migrations(connection, directory) == versions
    return versions


def test_0018_upgrades_an_existing_database_without_touching_its_data(tmp_path):
    """The upgrade path a real operational database takes: 0017, then 0018."""
    connection = connect_database(tmp_path / "upgrade.db")
    try:
        _apply_up_to_0017(connection, tmp_path)
        owner = ensure_user_profile(connection, "push-upgrade@example.invalid")
        before = connection.execute(
            "SELECT id, user_id FROM profiles ORDER BY id"
        ).fetchall()
        assert "push_subscriptions" not in {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        assert apply_migrations(connection) == ["0018"]

        assert rows(connection) == []
        assert (
            connection.execute(
                "SELECT id, user_id FROM profiles ORDER BY id"
            ).fetchall()
            == before
        )
        subscribe_push_subscription(
            connection,
            profile_id=owner.profile.id,
            endpoint=ENDPOINT,
            p256dh=P256DH,
            auth=AUTH,
        )
        assert len(rows(connection)) == 1
        assert apply_migrations(connection) == []
    finally:
        connection.close()
