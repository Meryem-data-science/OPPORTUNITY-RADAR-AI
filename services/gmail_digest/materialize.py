"""The daily decision: build the candidate, then decide whether it is news.

Phase 5.4A ends here. A digest is assembled from the audited Portfolio, given
an identity, and either frozen into the outbox or explicitly not — and nothing
sends it. There is no Gmail credential in this module, no HTTP client, and no
network call of any kind.

The policy, in full
------------------
The first digest is **not** a silent baseline. A user who has never received
one, and whose Portfolio has opportunities to act on, gets a digest for what
their Portfolio holds today: they have not already seen it anywhere else, so
suppressing it would be losing information rather than avoiding noise.

After that, exactly four outcomes:

``EMPTY``
    The audited Portfolio has no INCLUDED opportunity. There is nothing to
    say, so nothing is written — not even a row recording that there was
    nothing to say.

``ALREADY_MATERIALIZED``
    This profile already has a digest for this local day and this version.
    One day is one message; a second pass is a byte-level no-op.

``UNCHANGED``
    A new day, but the content fingerprint equals the one this recipient was
    **last actually sent**. Repeating a message someone already received is
    the definition of the noise a digest is supposed to replace, so the day
    passes in silence and nothing is written. Web Push remains responsible for
    telling the user immediately when something genuinely moves.

``CREATED``
    Everything else: one frozen PENDING row, and only one.

Fail-closed, and why
--------------------
A Portfolio that is NOT_SYNCED or that fails its audit produces no digest and
no row — an email is an irreversible act on a real person's attention, and
sending one derived from a snapshot the product itself does not trust is worse
than sending nothing. The same is true of an INCLUDED opportunity whose link
cannot be resolved: the whole assembly is refused rather than one dead link
being shipped.

One transaction
---------------
Reading the audited Portfolio, resolving the content, applying the four rules
and inserting the row all happen inside one ``BEGIN IMMEDIATE``. SQLite
serializes two materializers for the same profile and day, so the second sees
the first's row and reports ``ALREADY_MATERIALIZED``; the UNIQUE constraint on
``(profile_id, digest_date, digest_version)`` is the database's own copy of
that promise. Every no-write outcome ends in ``ROLLBACK``, so a repeated pass
leaves the file byte-identical rather than touching a timestamp to say it
looked.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone as utc_timezone

from services.collector.logging_config import get_logger
from services.portfolio.audit import (
    PortfolioAuditStatus,
    PortfolioProfileAuditStatus,
    audit_current_portfolio,
    audit_portfolio_run,
)
from services.portfolio.read_model import read_portfolio_run

from .assembly import build_digest_items
from .fingerprint import digest_content_fingerprint, recipient_fingerprint
from .local_day import resolve_local_day
from .models import (
    DIGEST_VERSION,
    DigestCandidate,
    DigestContent,
    DigestMaterializationResult,
    DigestMaterializationStatus,
    GmailDigestError,
)
from .persistence import (
    insert_frozen_digest,
    preflight,
    read_digest_for_day,
    read_latest_sent_digest,
)
from .rendering import render_digest

LOGGER = get_logger("services.collector.gmail_digest.materialize")

#: SQLite's own ``CURRENT_TIMESTAMP`` format, in UTC, so an instant this module
#: writes sorts and compares identically to one the database wrote.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# ``timezone`` is a parameter name in this module — the IANA name that decides
# the local day — so the datetime one is imported under an alias above rather
# than shadowed halfway through a function.


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GmailDigestError(f"{name} must be a positive integer")
    return value


def now_timestamp(moment: datetime | None = None) -> str:
    """Render an instant the way SQLite's ``CURRENT_TIMESTAMP`` renders it.

    Row timestamps stay in UTC whatever timezone decides the *day*: the local
    day is a business boundary, and a stored instant is a fact about when a
    write happened.
    """
    value = datetime.now(utc_timezone.utc) if moment is None else moment
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise GmailDigestError("now must be timezone-aware")
    return value.astimezone(utc_timezone.utc).strftime(TIMESTAMP_FORMAT)


def audited_current_run(connection: sqlite3.Connection, profile_id: int):
    """Return this profile's current Portfolio run only if it audits clean.

    Both audits are run: the profile-level one, which is what says the state
    and the run agree, and the run-level one, which is what says the stored
    payloads and fingerprints are the ones they claim to be.
    """
    profile_audit = audit_current_portfolio(connection, profile_id)
    if profile_audit.status is PortfolioProfileAuditStatus.CORRUPT:
        raise GmailDigestError("Portfolio state is corrupt; refusing to build a digest")
    if profile_audit.status is PortfolioProfileAuditStatus.NOT_SYNCED:
        raise GmailDigestError("Portfolio is not synced; refusing to build a digest")
    run_id = profile_audit.current_run_id
    if run_id is None:
        raise GmailDigestError("audited Portfolio state has no current run")
    run_audit = audit_portfolio_run(connection, run_id)
    if (
        run_audit.status is not PortfolioAuditStatus.OK
        or run_audit.profile_id != profile_id
    ):
        raise GmailDigestError(
            f"Portfolio run {run_id} is corrupt; refusing to build a digest"
        )
    run = read_portfolio_run(connection, run_id)
    if run.profile_id != profile_id:
        raise GmailDigestError(f"Portfolio run {run_id} belongs to another profile")
    return run


def build_digest_candidate(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    digest_version: str = DIGEST_VERSION,
) -> DigestCandidate:
    """Assemble what a digest would say today, without deciding to send it.

    This is the pure half of the phase as far as a database read allows: it
    reads, it renders, it fingerprints, and it writes nothing.
    """
    profile_id = _positive(profile_id, "profile_id")
    if digest_version != DIGEST_VERSION:
        raise GmailDigestError(f"unsupported digest version {digest_version!r}")
    run = audited_current_run(connection, profile_id)
    items = build_digest_items(connection, run)
    if not items:
        return DigestCandidate(
            profile_id, run.run_id, run.run_fingerprint, digest_version, None
        )
    subject, body_text, body_html = render_digest(items, digest_version=digest_version)
    content = DigestContent(
        digest_version,
        items,
        subject,
        body_text,
        body_html,
        digest_content_fingerprint(items, digest_version=digest_version),
    )
    return DigestCandidate(
        profile_id, run.run_id, run.run_fingerprint, digest_version, content
    )


def _result(
    status: DigestMaterializationStatus,
    *,
    profile_id: int,
    digest_version: str,
    digest_date: str,
    timezone: str,
    candidate: DigestCandidate | None,
    recipient: str,
    outbox_id: int | None,
) -> DigestMaterializationResult:
    content = None if candidate is None else candidate.content
    return DigestMaterializationResult(
        profile_id,
        status,
        digest_version,
        digest_date,
        timezone,
        0 if content is None else len(content.items),
        None if content is None else content.content_fingerprint,
        recipient,
        outbox_id,
        None if candidate is None else candidate.portfolio_run_id,
        status is DigestMaterializationStatus.CREATED,
    )


def materialize_daily_digest(
    connection: sqlite3.Connection,
    profile_id: int,
    *,
    timezone: str,
    recipient: str,
    now: datetime | None = None,
    digest_version: str = DIGEST_VERSION,
) -> DigestMaterializationResult:
    """Freeze at most one digest for this profile's current local day."""
    profile_id = _positive(profile_id, "profile_id")
    if digest_version != DIGEST_VERSION:
        raise GmailDigestError(f"unsupported digest version {digest_version!r}")
    if connection.in_transaction:
        raise GmailDigestError(
            "materialize_daily_digest requires a connection without an active"
            " transaction"
        )
    # Both are resolved before the transaction opens, so a bad timezone or a
    # malformed recipient never takes a write lock on the database.
    local_day = resolve_local_day(timezone, now)
    fingerprint = recipient_fingerprint(recipient)
    moment = now_timestamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        preflight(connection)
        existing = read_digest_for_day(
            connection,
            profile_id=profile_id,
            digest_date=local_day.date,
            digest_version=digest_version,
        )
        if existing is not None:
            # A day already has its message. Nothing about today's Portfolio
            # can change that, so the candidate is not even built.
            connection.execute("ROLLBACK")
            return DigestMaterializationResult(
                profile_id,
                DigestMaterializationStatus.ALREADY_MATERIALIZED,
                digest_version,
                local_day.date,
                local_day.timezone,
                existing.item_count,
                existing.content_fingerprint,
                fingerprint,
                existing.outbox_id,
                existing.portfolio_run_id,
                False,
            )
        candidate = build_digest_candidate(
            connection, profile_id, digest_version=digest_version
        )
        content = candidate.content
        if content is None:
            connection.execute("ROLLBACK")
            return _result(
                DigestMaterializationStatus.EMPTY,
                profile_id=profile_id,
                digest_version=digest_version,
                digest_date=local_day.date,
                timezone=local_day.timezone,
                candidate=candidate,
                recipient=fingerprint,
                outbox_id=None,
            )
        last_sent = read_latest_sent_digest(
            connection,
            profile_id=profile_id,
            recipient_fingerprint=fingerprint,
            digest_version=digest_version,
        )
        if (
            last_sent is not None
            and last_sent.content_fingerprint == content.content_fingerprint
        ):
            connection.execute("ROLLBACK")
            return _result(
                DigestMaterializationStatus.UNCHANGED,
                profile_id=profile_id,
                digest_version=digest_version,
                digest_date=local_day.date,
                timezone=local_day.timezone,
                candidate=candidate,
                recipient=fingerprint,
                outbox_id=last_sent.outbox_id,
            )
        outbox_id = insert_frozen_digest(
            connection,
            profile_id=profile_id,
            digest_date=local_day.date,
            timezone=local_day.timezone,
            portfolio_run_id=candidate.portfolio_run_id,
            portfolio_run_fingerprint=candidate.portfolio_run_fingerprint,
            digest_version=digest_version,
            content_fingerprint=content.content_fingerprint,
            recipient_fingerprint=fingerprint,
            item_count=len(content.items),
            subject=content.subject,
            body_text=content.body_text,
            body_html=content.body_html,
            now=moment,
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    # Only the identity is logged. The subject, the bodies and the recipient
    # address never reach a log line.
    LOGGER.info(
        "Gmail digest materialized.",
        extra={
            "event": "gmail_digest_materialized",
            "profile_id": profile_id,
            "digest_date": local_day.date,
            "digest_version": digest_version,
            "item_count": len(content.items),
            "outbox_id": outbox_id,
        },
    )
    return _result(
        DigestMaterializationStatus.CREATED,
        profile_id=profile_id,
        digest_version=digest_version,
        digest_date=local_day.date,
        timezone=local_day.timezone,
        candidate=candidate,
        recipient=fingerprint,
        outbox_id=outbox_id,
    )
