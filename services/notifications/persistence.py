"""Transaction-neutral, append-only persistence for notification events.

An event and its outbox row are one indivisible fact: a persisted event that
nothing will ever deliver is as wrong as a delivery with no audited event
behind it, so both rows are written together inside the caller's transaction.

The event fingerprint is the deduplication key. Re-deriving a movement that is
already stored is not an error and is not a second notification: the stored row
is verified against what was just derived, and then left exactly as it is.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .policy import NOTIFICATION_POLICY_VERSION, NotificationEventType


class NotificationPersistenceError(RuntimeError):
    """Raised when a notification event cannot be persisted safely."""


@dataclass(frozen=True)
class NotificationEventRecord:
    """One derived event, ready to be stored verbatim."""

    profile_id: int
    event_type: NotificationEventType
    opportunity_id: int
    previous_portfolio_run_id: int
    portfolio_run_id: int
    event_fingerprint: str
    payload_json: str
    policy_version: str = NOTIFICATION_POLICY_VERSION


@dataclass(frozen=True)
class NotificationStoreResult:
    """Outcome of storing one batch, described without re-reading it."""

    event_ids: tuple[int, ...]
    created_count: int
    duplicate_count: int


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise NotificationPersistenceError(f"{field} must be a positive integer")
    return value


def _validate(record: NotificationEventRecord) -> None:
    if not isinstance(record, NotificationEventRecord):
        raise NotificationPersistenceError("invalid notification event record")
    for field in (
        "profile_id",
        "opportunity_id",
        "previous_portfolio_run_id",
        "portfolio_run_id",
    ):
        _positive(getattr(record, field), field)
    if record.previous_portfolio_run_id == record.portfolio_run_id:
        raise NotificationPersistenceError(
            "an event cannot span a single Portfolio run"
        )
    if not isinstance(record.event_type, NotificationEventType):
        raise NotificationPersistenceError("invalid notification event type")
    if record.policy_version != NOTIFICATION_POLICY_VERSION:
        raise NotificationPersistenceError("unexpected notification policy version")
    if not _sha(record.event_fingerprint):
        raise NotificationPersistenceError("invalid event fingerprint")
    payload = record.payload_json
    if (
        not isinstance(payload, str)
        or not payload.strip()
        or payload != payload.strip()
    ):
        raise NotificationPersistenceError("invalid event payload JSON")


def store_notification_events(
    connection: sqlite3.Connection,
    records: tuple[NotificationEventRecord, ...],
) -> NotificationStoreResult:
    """Append these events and their outbox rows inside an open transaction."""
    if not connection.in_transaction:
        raise NotificationPersistenceError(
            "store_notification_events requires an active transaction"
        )
    if not isinstance(records, tuple):
        raise NotificationPersistenceError("event batch must be a tuple")
    fingerprints = {record.event_fingerprint for record in records}
    if len(fingerprints) != len(records):
        raise NotificationPersistenceError("duplicate event fingerprints in one batch")
    event_ids: list[int] = []
    created = duplicates = 0
    for record in records:
        _validate(record)
        found = connection.execute(
            """SELECT id,profile_id,event_type,opportunity_id,previous_portfolio_run_id,
            portfolio_run_id,policy_version,payload_json FROM notification_events
            WHERE event_fingerprint=?""",
            (record.event_fingerprint,),
        ).fetchone()
        if found is not None:
            stored = (
                record.profile_id,
                record.event_type.value,
                record.opportunity_id,
                record.previous_portfolio_run_id,
                record.portfolio_run_id,
                record.policy_version,
                record.payload_json,
            )
            if tuple(found[1:]) != stored:
                raise NotificationPersistenceError(
                    "stored notification event contradicts its fingerprint"
                )
            event_id = int(found[0])
            if (
                connection.execute(
                    "SELECT 1 FROM notification_outbox WHERE event_id=?", (event_id,)
                ).fetchone()
                is None
            ):
                raise NotificationPersistenceError(
                    "stored notification event has no outbox row"
                )
            event_ids.append(event_id)
            duplicates += 1
            continue
        inserted = connection.execute(
            """INSERT INTO notification_events
            (profile_id,event_type,opportunity_id,previous_portfolio_run_id,
             portfolio_run_id,policy_version,event_fingerprint,payload_json)
            VALUES (?,?,?,?,?,?,?,?) RETURNING id""",
            (
                record.profile_id,
                record.event_type.value,
                record.opportunity_id,
                record.previous_portfolio_run_id,
                record.portfolio_run_id,
                record.policy_version,
                record.event_fingerprint,
                record.payload_json,
            ),
        ).fetchone()
        if inserted is None:
            raise NotificationPersistenceError("event insert returned no row")
        event_id = int(inserted[0])
        connection.execute(
            "INSERT INTO notification_outbox (event_id) VALUES (?)", (event_id,)
        )
        event_ids.append(event_id)
        created += 1
    return NotificationStoreResult(tuple(event_ids), created, duplicates)
