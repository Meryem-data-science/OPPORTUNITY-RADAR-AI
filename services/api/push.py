"""Write surface for browser Web Push subscriptions.

Phase 5.3A only lets a browser opt in and out. Nothing here sends a
notification, and no VAPID *private* key is read, stored, or exposed: the
public application server key is the one value a browser legitimately needs.

The browser never names a profile. The server resolves the single configured
profile exactly like every other Opportunity Radar surface, so a subscription
can only ever be attached to the profile this deployment owns.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_database
from services.collector.logging_config import get_logger
from services.notifications import (
    MAX_ENDPOINT_LENGTH,
    MAX_KEY_LENGTH,
    P256DH_BYTE_LENGTH,
    P256DH_UNCOMPRESSED_PREFIX,
    PushSubscriptionError,
    PushSubscriptionOwnershipError,
    decode_base64url,
    subscribe_push_subscription,
    unsubscribe_push_subscription,
    valid_auth,
    valid_p256dh,
)


LOGGER = get_logger("services.collector.api.push")

PUBLIC_PUSH_ERROR = "Push subscriptions are temporarily unavailable."
PUBLIC_PUSH_REQUEST_ERROR = "Push subscription request is invalid."
PUBLIC_PUSH_CONFLICT_ERROR = "Push subscription cannot be registered."

VAPID_PUBLIC_KEY_VARIABLE = "WEB_PUSH_VAPID_PUBLIC_KEY"
REQUIRED_MIGRATION_VERSION = "0018"


class PushApiError(RuntimeError):
    """Raised when the push surface cannot serve a request safely."""


class PushRequestError(RuntimeError):
    """Raised when a client sent a body this surface will not accept."""


class PushConflictError(RuntimeError):
    """Raised when the request cannot be applied to the stored subscription."""


class PushConfigResponse(BaseModel):
    configured: bool
    vapid_public_key: str | None


class PushSubscriptionStateResponse(BaseModel):
    status: Literal["ACTIVE", "REVOKED"]


def _valid_public_key(value: str) -> bool:
    """Accept only a base64url uncompressed P-256 point, the VAPID key shape.

    This is the same shape as a subscription's ``p256dh``, decoded through the
    same strict base64url reader: an application server key that is not exactly
    65 bytes starting with 0x04 is not one.
    """
    if value != value.strip() or len(value) > MAX_KEY_LENGTH:
        return False
    decoded = decode_base64url(value)
    return (
        decoded is not None
        and len(decoded) == P256DH_BYTE_LENGTH
        and decoded[0] == P256DH_UNCOMPRESSED_PREFIX
    )


def read_push_config() -> PushConfigResponse:
    """Report whether Web Push is configured, exposing only the public key."""
    raw = os.environ.get(VAPID_PUBLIC_KEY_VARIABLE)
    value = "" if raw is None else raw.strip()
    if not value:
        return PushConfigResponse(configured=False, vapid_public_key=None)
    if not _valid_public_key(value):
        # The value itself never reaches the log: only the fact it was rejected.
        LOGGER.error(
            "Configured VAPID public key is malformed.",
            extra={"event": "push_config_invalid_public_key"},
        )
        return PushConfigResponse(configured=False, vapid_public_key=None)
    return PushConfigResponse(configured=True, vapid_public_key=value)


def _mapping(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PushRequestError(message)
    return value


def _required_text(payload: dict[str, Any], field: str, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or value != value.strip() or not value:
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    if len(value) > maximum:
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    return value


def parse_subscription_request(body: Any) -> tuple[str, str, str]:
    """Read the three fields of a PushSubscription and refuse anything else.

    The browser's ``PushSubscription.toJSON()`` carries more than we want to
    store, so only ``endpoint`` and the two keys are read here, and a body that
    smuggles extra top-level fields is refused rather than silently trimmed.
    """
    payload = _mapping(body, PUBLIC_PUSH_REQUEST_ERROR)
    if set(payload) != {"endpoint", "keys"}:
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    keys = _mapping(payload.get("keys"), PUBLIC_PUSH_REQUEST_ERROR)
    if set(keys) != {"p256dh", "auth"}:
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    endpoint = _required_text(payload, "endpoint", MAX_ENDPOINT_LENGTH)
    if not endpoint.startswith("https://"):
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    p256dh = _required_text(keys, "p256dh", MAX_KEY_LENGTH)
    auth = _required_text(keys, "auth", MAX_KEY_LENGTH)
    # Refused here, before a connection is opened, and refused by shape rather
    # than by length alone: the rejection never names or echoes the value.
    if not valid_p256dh(p256dh) or not valid_auth(auth):
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    return endpoint, p256dh, auth


def parse_unsubscribe_request(body: Any) -> str:
    """Read the single endpoint a browser wants revoked."""
    payload = _mapping(body, PUBLIC_PUSH_REQUEST_ERROR)
    if set(payload) != {"endpoint"}:
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    endpoint = _required_text(payload, "endpoint", MAX_ENDPOINT_LENGTH)
    if not endpoint.startswith("https://"):
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR)
    return endpoint


def _profile_id() -> int:
    raw = os.environ.get("OPPORTUNITY_RADAR_PROFILE_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError as error:
        raise PushApiError(PUBLIC_PUSH_ERROR) from error
    if raw is None or value <= 0 or raw.strip() != str(value):
        raise PushApiError(PUBLIC_PUSH_ERROR)
    return value


def _connect() -> sqlite3.Connection:
    settings = load_settings()
    if (
        settings.database_backend is not DatabaseBackend.SQLITE
        or settings.sqlite_database_path is None
    ):
        raise PushApiError(PUBLIC_PUSH_ERROR)
    path = Path(settings.sqlite_database_path)
    # connect_database would happily create an empty database; a write surface
    # pointed at a mistyped path must fail instead of inventing a store.
    if not path.is_file():
        raise PushApiError(PUBLIC_PUSH_ERROR)
    connection = connect_database(path)
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (REQUIRED_MIGRATION_VERSION,),
        ).fetchone()
    except sqlite3.DatabaseError as error:
        connection.close()
        raise PushApiError(PUBLIC_PUSH_ERROR) from error
    if migrated is None:
        connection.close()
        raise PushApiError(PUBLIC_PUSH_ERROR)
    return connection


def _run(operation: str, action: Any) -> None:
    connection = None
    try:
        profile_id = _profile_id()
        connection = _connect()
        action(connection, profile_id)
    except (PushApiError, PushRequestError, PushConflictError):
        raise
    except PushSubscriptionOwnershipError as error:
        LOGGER.error(
            "Push subscription endpoint belongs to another profile.",
            extra={"event": f"push_{operation}_ownership_conflict"},
        )
        raise PushConflictError(PUBLIC_PUSH_CONFLICT_ERROR) from error
    except PushSubscriptionError as error:
        LOGGER.error(
            "Push subscription request was refused.",
            extra={"event": f"push_{operation}_refused"},
        )
        raise PushRequestError(PUBLIC_PUSH_REQUEST_ERROR) from error
    except Exception as error:
        LOGGER.error(
            "Push subscription request failed.",
            extra={
                "event": f"push_{operation}_failed",
                "error_type": type(error).__name__,
            },
        )
        raise PushApiError(PUBLIC_PUSH_ERROR) from None
    finally:
        if connection is not None:
            connection.close()


def register_subscription(body: Any) -> PushSubscriptionStateResponse:
    """Persist one browser subscription against the server-resolved profile."""
    endpoint, p256dh, auth = parse_subscription_request(body)

    def action(connection: sqlite3.Connection, profile_id: int) -> None:
        subscribe_push_subscription(
            connection,
            profile_id=profile_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
        )

    _run("subscribe", action)
    return PushSubscriptionStateResponse(status="ACTIVE")


def revoke_subscription(body: Any) -> PushSubscriptionStateResponse:
    """Revoke one browser subscription for the server-resolved profile."""
    endpoint = parse_unsubscribe_request(body)

    def action(connection: sqlite3.Connection, profile_id: int) -> None:
        unsubscribe_push_subscription(
            connection, profile_id=profile_id, endpoint=endpoint
        )

    _run("unsubscribe", action)
    return PushSubscriptionStateResponse(status="REVOKED")
