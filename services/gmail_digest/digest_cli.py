"""Local entry point that materializes today's Gmail digest once.

This is a command someone runs, not a scheduler: one pass, then it exits.
There is no daemon, no cron entry, and no workflow behind it — and, in Phase
5.4A, no send either. The command's whole output is a frozen row.

Two modes, and the difference between them is the point:

``--dry-run``
    Opens the database through SQLite's ``mode=ro`` URI, pins ``query_only``,
    and reports what a materialization *would* decide. It writes nothing at
    all — not a row, not a timestamp, not a byte — which is what makes it safe
    to point at a real operational database.

normal
    Runs the daily policy and, when the policy says so, freezes exactly one
    PENDING digest. It sends no email.

The timezone is required in both modes, because a daily boundary taken from
the machine's clock settings is not a boundary anyone chose. The recipient is
required too: a frozen digest is bound to the mailbox it is for, as a
fingerprint. Neither the recipient address nor any rendered body is ever
printed — the output carries the fingerprint, the counts, and the identities.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from services.collector.database.connection import (
    connect_database,
    connect_readonly_database,
)

from .fingerprint import recipient_fingerprint
from .local_day import LocalDay, resolve_local_day
from .materialize import build_digest_candidate, materialize_daily_digest
from .models import DIGEST_VERSION, DigestMaterializationStatus, GmailDigestError
from .persistence import preflight, read_digest_for_day, read_latest_sent_digest


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
            "Materialize one deterministic daily Gmail digest for one profile."
            " Sends nothing."
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
            "Intended recipient address. Only its fingerprint is stored, and"
            " the address itself is never printed or logged."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the decision using SQLite mode=ro; write nothing.",
    )
    return parser.parse_args(argv)


def _dry_run(
    args: argparse.Namespace, local_day: LocalDay, fingerprint: str
) -> dict[str, object]:
    """Decide what would happen, over a connection that cannot write."""
    with connect_readonly_database(args.database) as connection:
        connection.execute("PRAGMA query_only = ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if enabled is None or enabled[0] != 1:
            raise GmailDigestError("dry run could not pin the connection read-only")
        preflight(connection)
        existing = read_digest_for_day(
            connection,
            profile_id=args.profile_id,
            digest_date=local_day.date,
            digest_version=DIGEST_VERSION,
        )
        if existing is not None:
            return {
                "status": DigestMaterializationStatus.ALREADY_MATERIALIZED.value,
                "item_count": existing.item_count,
                "content_fingerprint": existing.content_fingerprint,
                "outbox_id": existing.outbox_id,
                "portfolio_run_id": existing.portfolio_run_id,
            }
        candidate = build_digest_candidate(connection, args.profile_id)
        content = candidate.content
        if content is None:
            return {
                "status": DigestMaterializationStatus.EMPTY.value,
                "item_count": 0,
                "content_fingerprint": None,
                "outbox_id": None,
                "portfolio_run_id": candidate.portfolio_run_id,
            }
        last_sent = read_latest_sent_digest(
            connection,
            profile_id=args.profile_id,
            recipient_fingerprint=fingerprint,
            digest_version=DIGEST_VERSION,
        )
        unchanged = (
            last_sent is not None
            and last_sent.content_fingerprint == content.content_fingerprint
        )
        return {
            "status": (
                DigestMaterializationStatus.UNCHANGED.value
                if unchanged
                else DigestMaterializationStatus.CREATED.value
            ),
            "item_count": len(content.items),
            "content_fingerprint": content.content_fingerprint,
            "outbox_id": last_sent.outbox_id if unchanged else None,
            "portfolio_run_id": candidate.portfolio_run_id,
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        fingerprint = recipient_fingerprint(args.recipient)
        if args.dry_run:
            local_day = resolve_local_day(args.timezone)
            payload: dict[str, object] = {
                "mode": "dry-run",
                "profile_id": args.profile_id,
                "digest_version": DIGEST_VERSION,
                "digest_date": local_day.date,
                "timezone": local_day.timezone,
                "recipient_fingerprint": fingerprint,
                "created": False,
            } | _dry_run(args, local_day, fingerprint)
        else:
            with connect_database(args.database) as connection:
                result = materialize_daily_digest(
                    connection,
                    args.profile_id,
                    timezone=args.timezone,
                    recipient=args.recipient,
                )
            payload = {
                "mode": "materialize",
                "profile_id": result.profile_id,
                "status": result.status.value,
                "digest_version": result.digest_version,
                "digest_date": result.digest_date,
                "timezone": result.timezone,
                "item_count": result.item_count,
                "content_fingerprint": result.content_fingerprint,
                "recipient_fingerprint": result.recipient_fingerprint,
                "outbox_id": result.outbox_id,
                "portfolio_run_id": result.portfolio_run_id,
                "created": result.created,
            }
    except Exception as error:
        # Only the class of failure is printed: a message from further down
        # could otherwise carry a recipient address or a rendered body into a
        # terminal or a log.
        print(f"gmail digest failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
