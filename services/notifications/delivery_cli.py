"""Local entry point that materializes and drains Web Push deliveries once.

This is a command someone runs, not a scheduler: it does one pass and exits.
There is no daemon, no cron, no workflow, and no deployment behind it.

``--dry-run`` opens the database read-only and reports the backlog without
materializing, sending, or writing anything, which is how a real operational
database can be inspected safely. Any other mode requires a complete, valid
VAPID identity, and builds the sender *before* reading any work, so a
half-configured deployment stops before it has opened a single socket.
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

from .delivery import (
    WebPushDeliverySender,
    drain_notification_deliveries,
)
from .delivery_persistence import read_delivery_status
from .sync import sync_notification_policy
from .web_push import HttpxWebPushTransport, load_vapid_configuration


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
        description="Deliver pending Web Push notifications for one profile."
    )
    parser.add_argument(
        "--database", required=True, help="Explicit SQLite database path."
    )
    parser.add_argument("--profile-id", required=True, type=_positive_integer)
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Advance the notification policy cursor before delivering.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_integer,
        help="Attempt at most this many due targets in this pass.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the backlog using SQLite mode=ro; send and write nothing.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.dry_run:
            with connect_readonly_database(args.database) as connection:
                report = read_delivery_status(connection, profile_id=args.profile_id)
            payload: dict[str, object] = {"mode": "dry-run"} | asdict(report)
        else:
            # Fail closed before any work is read and before any socket exists.
            sender = WebPushDeliverySender(
                load_vapid_configuration(), HttpxWebPushTransport()
            )
            with connect_database(args.database) as connection:
                synced = (
                    sync_notification_policy(connection, args.profile_id).status.value
                    if args.sync
                    else None
                )
                result = drain_notification_deliveries(
                    connection, sender, profile_id=args.profile_id, limit=args.limit
                )
            payload = {"mode": "deliver", "sync_status": synced} | asdict(result)
    except Exception as error:
        # Only the class of failure is printed: a message from further down
        # could otherwise carry an endpoint or a key into a terminal or a log.
        print(f"notification delivery failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
