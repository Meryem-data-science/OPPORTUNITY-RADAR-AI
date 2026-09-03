"""The one operator command: freeze today's digest, then send what is due.

This is a command someone runs, not a scheduler: one pass, then it exits.
There is no daemon, no cron entry, no workflow and no deployment behind it.

Normal mode does four things in this order, and the order is the safety
property:

1. **loads, refreshes and validates the Gmail authorization** — before the
   database is opened for writing at all, so a missing, corrupt, revoked or
   over-scoped credential can never move a PENDING digest to IN_FLIGHT;
2. materializes today's digest using the unchanged Phase 5.4A policy;
3. drains the digests that are due, one at a time;
4. prints counters and exits.

Run twice in one day, the second run is quiet by construction: materialization
reports ``ALREADY_MATERIALIZED`` and writes nothing, the drain finds nothing
due because the digest is already ``SENT``, and no second email is sent.

``--dry-run`` is the mode that is safe against a real operational database. It
opens SQLite through the ``mode=ro`` URI with ``query_only`` pinned, requires
no OAuth file, refreshes no credential, opens no socket, claims nothing,
recovers nothing and writes nothing — the file is byte-identical afterwards.
What it reports is operational shape only: what today's materialization would
decide, and how many digests are pending, due, in flight, stale, sent, failed,
or frozen for a mailbox other than the configured one.

Neither mode ever prints the recipient address, the subject, either rendered
body, the MIME payload, or anything from an OAuth file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)

from .delivery import drain_gmail_digests
from .delivery_persistence import read_delivery_status
from .fingerprint import recipient_fingerprint
from .local_day import resolve_local_day
from .materialize import materialize_daily_digest, preview_digest_decision
from .models import DIGEST_VERSION, GmailDigestError
from .oauth import (
    DEFAULT_CLIENT_SECRET_PATH,
    DEFAULT_TOKEN_PATH,
    GmailOAuthConfiguration,
)
from .sender import build_gmail_digest_sender


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize today's Gmail digest and deliver what is due, once."
            " Sends through OAuth gmail.send only; never reads a mailbox."
        )
    )
    parser.add_argument(
        "--database", required=True, help="Explicit SQLite database path."
    )
    parser.add_argument("--profile-id", required=True, type=_positive_integer)
    parser.add_argument(
        "--timezone",
        required=True,
        help="Explicit IANA timezone deciding the local day, e.g. Africa/Casablanca.",
    )
    parser.add_argument(
        "--recipient",
        required=True,
        help=(
            "Intended recipient address. Checked against the frozen digest's"
            " fingerprint; never persisted, printed or logged."
        ),
    )
    parser.add_argument(
        "--limit",
        type=_positive_integer,
        help="Attempt at most this many due digests in this pass.",
    )
    parser.add_argument(
        "--client-secret",
        default=str(DEFAULT_CLIENT_SECRET_PATH),
        help=f"Desktop OAuth client JSON (default: {DEFAULT_CLIENT_SECRET_PATH}).",
    )
    parser.add_argument(
        "--token",
        default=str(DEFAULT_TOKEN_PATH),
        help=f"Stored gmail.send authorization (default: {DEFAULT_TOKEN_PATH}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report the backlog using SQLite mode=ro. Requires no OAuth file,"
            " opens no socket, and writes nothing."
        ),
    )
    return parser.parse_args(argv)


def _dry_run(args: argparse.Namespace, fingerprint: str) -> dict[str, object]:
    """Describe what would happen, over a connection that cannot write."""
    local_day = resolve_local_day(args.timezone)
    with connect_readonly_database(args.database) as connection:
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise GmailDigestError("dry run could not pin the connection read-only")
        preview = preview_digest_decision(
            connection,
            profile_id=args.profile_id,
            digest_date=local_day.date,
            recipient_fingerprint=fingerprint,
            digest_version=DIGEST_VERSION,
        )
        report = read_delivery_status(
            connection,
            profile_id=args.profile_id,
            recipient_fingerprint=fingerprint,
            digest_version=DIGEST_VERSION,
        )
    return {
        "mode": "dry-run",
        "digest_date": local_day.date,
        "timezone": local_day.timezone,
        "recipient_fingerprint": fingerprint,
        "would_materialize": preview.status.value,
        "would_materialize_item_count": preview.item_count,
        "delivery": asdict(report),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        fingerprint = recipient_fingerprint(args.recipient)
        if args.dry_run:
            payload: dict[str, object] = _dry_run(args, fingerprint)
        else:
            # Fail closed before the database is opened for writing, before a
            # digest is claimed, and before any socket exists.
            sender = build_gmail_digest_sender(
                args.recipient,
                configuration=GmailOAuthConfiguration.local(
                    client_secret_path=args.client_secret, token_path=args.token
                ),
            )
            with connect_database(args.database) as connection:
                materialization = materialize_daily_digest(
                    connection,
                    args.profile_id,
                    timezone=args.timezone,
                    recipient=args.recipient,
                )
                result = drain_gmail_digests(
                    connection,
                    sender,
                    profile_id=args.profile_id,
                    recipient=args.recipient,
                    limit=args.limit,
                )
            payload = {
                "mode": "deliver",
                "digest_date": materialization.digest_date,
                "timezone": materialization.timezone,
                "materialization_status": materialization.status.value,
                "materialization_item_count": materialization.item_count,
                "materialized": materialization.created,
                "outbox_id": materialization.outbox_id,
                "delivery": asdict(result),
            }
    except Exception as error:
        # Only the class of failure is printed: a message from further down
        # could otherwise carry a recipient address, a rendered body or
        # something from a credential into a terminal or a log.
        print(f"gmail digest delivery failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
