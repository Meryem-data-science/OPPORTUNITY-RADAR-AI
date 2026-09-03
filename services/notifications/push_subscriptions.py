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

import base64
import binascii
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec

from services.collector.logging_config import get_logger


LOGGER = get_logger("services.collector.notifications.push_subscriptions")

MAX_ENDPOINT_LENGTH = 2048
MAX_KEY_LENGTH = 256

# A Web Push client key is an uncompressed P-256 point, and the auth secret is
# 16 raw bytes. Both arrive base64url-encoded and unpadded, so their encoded
# lengths are fixed too.
P256DH_BYTE_LENGTH = 65
P256DH_UNCOMPRESSED_PREFIX = 0x04
AUTH_BYTE_LENGTH = 16

_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


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


def decode_base64url(value: str) -> bytes | None:
    """Decode strict unpadded base64url, or return None.

    ``base64.urlsafe_b64decode`` silently drops characters outside the
    alphabet, so "not a key!!" would decode to *something*. The alphabet is
    checked first, and so is the length: a remainder of one leaves a byte that
    cannot exist, and any "=" means the caller sent padding we do not accept.
    """
    if not isinstance(value, str) or not _BASE64URL.fullmatch(value):
        return None
    if len(value) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


def valid_p256dh(value: str) -> bool:
    """Accept only an uncompressed P-256 public point, base64url encoded.

    The length and the 0x04 prefix only describe the *shape* of a point; 65
    bytes of noise has both. Since delivery has to run an ECDH against this
    value anyway, the point is verified to be on the curve here, at the door,
    rather than failing much later inside an encryption nobody can retry.

    Nothing about the rejection is reported: the caller gets False, and the
    candidate — which is a credential — is never named, logged, or re-raised.
    """
    decoded = decode_base64url(value)
    if (
        decoded is None
        or len(decoded) != P256DH_BYTE_LENGTH
        or decoded[0] != P256DH_UNCOMPRESSED_PREFIX
    ):
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded)
    except (ValueError, TypeError):
        return False
    return True


def valid_auth(value: str) -> bool:
    """Accept only a 16-byte auth secret, base64url encoded."""
    decoded = decode_base64url(value)
    return decoded is not None and len(decoded) == AUTH_BYTE_LENGTH


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


def _credential(value: object, field: str, accepted: Callable[[str], bool]) -> str:
    """Validate one credential without ever putting it in the error."""
    text = _text(value, field, MAX_KEY_LENGTH)
    if not accepted(text):
        raise PushSubscriptionError(f"{field} is not a valid {field}")
    return text


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
    p256dh = _credential(p256dh, "p256dh", valid_p256dh)
    auth = _credential(auth, "auth", valid_auth)

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


def revoke_push_subscription_by_id(
    connection: sqlite3.Connection, subscription_id: int
) -> bool:
    """Revoke one stored subscription by id, inside the caller's transaction.

    Delivery learns an endpoint is gone from the push service itself, not from
    the browser, and it learns it while holding a short transaction of its own.
    So the transition lives here, next to the browser-driven one, rather than
    being re-implemented against the same columns somewhere else: an already
    revoked row is left exactly as it is, and the ``REVOKED``/``revoked_at``
    invariant migration 0018 enforces is written in one place.
    """
    _positive_int(subscription_id, "subscription_id")
    if not connection.in_transaction:
        raise PushSubscriptionError(
            "revoke_push_subscription_by_id requires an active transaction"
        )
    row = connection.execute(
        "SELECT status FROM push_subscriptions WHERE id = ?", (subscription_id,)
    ).fetchone()
    if row is None:
        raise PushSubscriptionError("push subscription does not exist")
    if row[0] != PushSubscriptionStatus.ACTIVE.value:
        return False
    connection.execute(
        """UPDATE push_subscriptions
           SET status = 'REVOKED', revoked_at = CURRENT_TIMESTAMP,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (subscription_id,),
    )
    LOGGER.info(
        "Push subscription revoked by delivery.",
        extra={
            "event": "push_subscription_revoked_by_delivery",
            "subscription_id": subscription_id,
        },
    )
    return True


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
