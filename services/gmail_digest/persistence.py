"""Durable storage for frozen daily digests, and the reads the policy needs.

The table is a queue of messages that have already been decided. Nothing here
renders, ranks, or re-reads a Portfolio: it inserts one frozen row, or answers
one of the three questions the daily policy asks — is there a digest for this
day, what did this recipient last actually receive, and what does this
profile's outbox look like.

No function here opens a socket, and none of them starts or ends a
transaction: the caller owns the transaction, because the whole point of the
policy is that reading the current state and writing the row are one decision.
"""

from __future__ import annotations

import sqlite3

from .models import (
    DIGEST_VERSION,
    DigestOutboxRecord,
    DigestStatusReport,
    GmailDigestError,
    GmailDigestStatus,
)

REQUIRED_MIGRATION_VERSION = "0022"
DIGEST_OUTBOX_TABLE = "gmail_digest_outbox"

_RECORD_COLUMNS = """id,profile_id,digest_date,timezone,digest_version,
content_fingerprint,recipient_fingerprint,portfolio_run_id,portfolio_run_fingerprint,
item_count,status,attempt_count,created_at,sent_at"""


def preflight(connection: sqlite3.Connection) -> None:
    """Refuse to touch digest persistence that has not been migrated.

    A database that stops at 0021 is refused by name rather than by "no such
    table" from the middle of a materialization.
    """
    try:
        migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?",
            (REQUIRED_MIGRATION_VERSION,),
        ).fetchone()
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (DIGEST_OUTBOX_TABLE,),
        ).fetchone()
    except sqlite3.Error as error:
        raise GmailDigestError(
            "Gmail digest schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before use"
        ) from error
    if migrated is None or table is None:
        raise GmailDigestError(
            "Gmail digest schema is not ready;"
            f" migrate through {REQUIRED_MIGRATION_VERSION} before use"
        )


def _record(row: tuple) -> DigestOutboxRecord:
    try:
        status = GmailDigestStatus(row[10])
    except ValueError as error:
        raise GmailDigestError("persisted digest carries an unknown status") from error
    return DigestOutboxRecord(
        int(row[0]),
        int(row[1]),
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
        int(row[7]),
        row[8],
        int(row[9]),
        status,
        int(row[11]),
        row[12],
        row[13],
    )


def read_digest_for_day(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    digest_date: str,
    digest_version: str = DIGEST_VERSION,
) -> DigestOutboxRecord | None:
    """Return this profile's digest for one local day, whatever its status."""
    row = connection.execute(
        f"""SELECT {_RECORD_COLUMNS} FROM {DIGEST_OUTBOX_TABLE}
        WHERE profile_id=? AND digest_date=? AND digest_version=?""",
        (profile_id, digest_date, digest_version),
    ).fetchone()
    return None if row is None else _record(row)


def read_latest_sent_digest(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    recipient_fingerprint: str,
    digest_version: str = DIGEST_VERSION,
) -> DigestOutboxRecord | None:
    """Return the last digest this recipient was actually sent, if any.

    "Actually sent" is the whole point: a digest that was materialized and
    never delivered has told the user nothing, so it can never be the reason a
    later day stays silent.
    """
    row = connection.execute(
        f"""SELECT {_RECORD_COLUMNS} FROM {DIGEST_OUTBOX_TABLE}
        WHERE profile_id=? AND digest_version=? AND recipient_fingerprint=?
        AND status=? ORDER BY id DESC LIMIT 1""",
        (
            profile_id,
            digest_version,
            recipient_fingerprint,
            GmailDigestStatus.SENT.value,
        ),
    ).fetchone()
    return None if row is None else _record(row)


def insert_frozen_digest(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    digest_date: str,
    timezone: str,
    portfolio_run_id: int,
    portfolio_run_fingerprint: str,
    digest_version: str,
    content_fingerprint: str,
    recipient_fingerprint: str,
    item_count: int,
    subject: str,
    body_text: str,
    body_html: str,
    now: str,
) -> int:
    """Write exactly one frozen PENDING digest and return its row id.

    ``next_attempt_at`` is the materialization instant: the digest is due as
    soon as it exists, and Phase 5.4B decides when a sender actually runs.
    Every other lifecycle column stays empty, which is what migration 0022's
    CHECK constraints define a fresh PENDING row to be.
    """
    if item_count <= 0:
        raise GmailDigestError("refusing to materialize a digest with no items")
    inserted = connection.execute(
        f"""INSERT INTO {DIGEST_OUTBOX_TABLE}
        (profile_id,digest_date,timezone,portfolio_run_id,portfolio_run_fingerprint,
         digest_version,content_fingerprint,recipient_fingerprint,item_count,subject,
         body_text,body_html,status,attempt_count,next_attempt_at,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?) RETURNING id""",
        (
            profile_id,
            digest_date,
            timezone,
            portfolio_run_id,
            portfolio_run_fingerprint,
            digest_version,
            content_fingerprint,
            recipient_fingerprint,
            item_count,
            subject,
            body_text,
            body_html,
            GmailDigestStatus.PENDING.value,
            now,
            now,
            now,
        ),
    ).fetchone()
    if inserted is None:
        raise GmailDigestError("digest insert returned no row")
    return int(inserted[0])


def read_digest_status(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    digest_version: str = DIGEST_VERSION,
) -> DigestStatusReport:
    """Describe one profile's digest outbox without exposing any body."""
    if (
        isinstance(profile_id, bool)
        or not isinstance(profile_id, int)
        or profile_id <= 0
    ):
        raise GmailDigestError("profile_id must be a positive integer")
    preflight(connection)
    counts = tuple(
        (str(row[0]), int(row[1]))
        for row in connection.execute(
            f"""SELECT status,COUNT(*) FROM {DIGEST_OUTBOX_TABLE}
            WHERE profile_id=? AND digest_version=? GROUP BY status ORDER BY status""",
            (profile_id, digest_version),
        )
    )
    latest_row = connection.execute(
        f"""SELECT {_RECORD_COLUMNS} FROM {DIGEST_OUTBOX_TABLE}
        WHERE profile_id=? AND digest_version=? ORDER BY id DESC LIMIT 1""",
        (profile_id, digest_version),
    ).fetchone()
    sent_row = connection.execute(
        f"""SELECT {_RECORD_COLUMNS} FROM {DIGEST_OUTBOX_TABLE}
        WHERE profile_id=? AND digest_version=? AND status=?
        ORDER BY id DESC LIMIT 1""",
        (profile_id, digest_version, GmailDigestStatus.SENT.value),
    ).fetchone()
    return DigestStatusReport(
        profile_id,
        digest_version,
        sum(count for _, count in counts),
        counts,
        None if latest_row is None else _record(latest_row),
        None if sent_row is None else _record(sent_row),
    )
