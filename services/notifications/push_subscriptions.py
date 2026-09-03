"""Idempotent persistence for browser Web Push subscriptions.

A push subscription is an operational credential the browser hands us, not an
audited fact about an opportunity: the same endpoint can legitimately come back
with rotated keys, be revoked, and be re-enabled later. So this table is
mutable by design — there is no append-only history to keep — while every
transition stays a single atomic transaction and every endpoint stays owned by
exactly one profile.

Nothing here logs an endpoint or a key: those values are the credential.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from services.collector.logging_config import get_logger


LOGGER = get_logger("services.collector.notifications.push_subscriptions")

MAX_ENDPOINT_LENGTH = 2048
MAX_KEY_LENGTH = 256


class PushSubscriptionStatus(str, Enum):
    """Lifecycle of one stored subscription."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class PushSubscriptionError(RuntimeError):
    """Raised when a push subscription cannot be stored safely."""


class PushSubscriptionOwnershipError(PushSubscriptionError):
    """Raised when an endpoint is already owned by a different profile."""


@dataclass(frozen=True)
class SubscribeResult:
    """Outcome of one subscribe call, described without echoing credentials."""

    subscription_id: int
    created: bool
    reactivated: bool
    keys_updated: bool

    @property
    def changed(self) -> bool:
        return self.created or self.reactivated or self.keys_updated


@dataclass(frozen=True)
class UnsubscribeResult:
    """Outcome of one unsubscribe call."""

    subscription_id: int | None
    revoked: bool


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PushSubscriptionError(f"{field} must be a positive integer")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PushSubscriptionError(f"{field} must be a string")
    if value != value.strip() or not value or len(value) > maximum:
        raise PushSubscriptionError(f"{field} is not a valid {field}")
    return value


def _endpoint(value: object) -> str:
    endpoint = _text(value, "endpoint", MAX_ENDPOINT_LENGTH)
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise PushSubscriptionError("endpoint is not a valid endpoint")
    return endpoint


def _profile_exists(connection: sqlite3.Connection, profile_id: int) -> bool:
    row = connection.execute(
        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    return row is not None


def subscribe_push_subscription(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    endpoint: str,
    p256dh: str,
    auth: str,
) -> SubscribeResult:
    """Register or refresh one endpoint for this profile, idempotently.

    Subscribing an endpoint that is already active with the same keys changes
    nothing at all; rotated keys are updated in place and a revoked endpoint is
    reactivated. An endpoint already owned by another profile is refused
    without touching the stored row.
    """
    _positive_int(profile_id, "profile_id")
    endpoint = _endpoint(endpoint)
    p256dh = _text(p256dh, "p256dh", MAX_KEY_LENGTH)
    auth = _text(auth, "auth", MAX_KEY_LENGTH)

    connection.execute("BEGIN IMMEDIATE")
    try:
        if not _profile_exists(connection, profile_id):
            raise PushSubscriptionError("profile does not exist")
        row = connection.execute(
            "SELECT id, profile_id, p256dh, auth, status FROM push_subscriptions"
            " WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
        if row is None:
            inserted = connection.execute(
                """INSERT INTO push_subscriptions
                   (profile_id, endpoint, p256dh, auth, status)
                   VALUES (?, ?, ?, ?, 'ACTIVE') RETURNING id""",
                (profile_id, endpoint, p256dh, auth),
            ).fetchone()
            if inserted is None:
                raise PushSubscriptionError("push subscription insert returned no row")
            result = SubscribeResult(int(inserted[0]), True, False, False)
        else:
            subscription_id = int(row[0])
            if int(row[1]) != profile_id:
                raise PushSubscriptionOwnershipError(
                    "endpoint is registered to a different profile"
                )
            keys_updated = (row[2], row[3]) != (p256dh, auth)
            reactivated = row[4] != PushSubscriptionStatus.ACTIVE.value
            if keys_updated or reactivated:
                connection.execute(
                    """UPDATE push_subscriptions
                       SET p256dh = ?, auth = ?, status = 'ACTIVE', revoked_at = NULL,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (p256dh, auth, subscription_id),
                )
            result = SubscribeResult(subscription_id, False, reactivated, keys_updated)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Push subscription registered.",
        extra={
            "event": "push_subscription_registered",
            "profile_id": profile_id,
            "subscription_id": result.subscription_id,
            "subscription_created": result.created,
            "subscription_reactivated": result.reactivated,
            "subscription_keys_updated": result.keys_updated,
        },
    )
    return result


def unsubscribe_push_subscription(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    endpoint: str,
) -> UnsubscribeResult:
    """Revoke one endpoint for this profile, idempotently.

    An unknown endpoint and an already revoked one are both a no-op, so a
    browser that unsubscribes twice — or after the server already forgot it —
    still gets a clean answer.
    """
    _positive_int(profile_id, "profile_id")
    endpoint = _endpoint(endpoint)

    connection.execute("BEGIN IMMEDIATE")
    try:
        if not _profile_exists(connection, profile_id):
            raise PushSubscriptionError("profile does not exist")
        row = connection.execute(
            "SELECT id, profile_id, status FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
        if row is None:
            result = UnsubscribeResult(None, False)
        else:
            subscription_id = int(row[0])
            if int(row[1]) != profile_id:
                raise PushSubscriptionOwnershipError(
                    "endpoint is registered to a different profile"
                )
            if row[2] == PushSubscriptionStatus.ACTIVE.value:
                connection.execute(
                    """UPDATE push_subscriptions
                       SET status = 'REVOKED', revoked_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (subscription_id,),
                )
                result = UnsubscribeResult(subscription_id, True)
            else:
                result = UnsubscribeResult(subscription_id, False)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Push subscription revoked.",
        extra={
            "event": "push_subscription_revoked",
            "profile_id": profile_id,
            "subscription_id": result.subscription_id,
            "subscription_revoked": result.revoked,
        },
    )
    return result
