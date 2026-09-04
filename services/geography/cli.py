"""Local CLI for the Phase 7A.1 geographic targeting foundation.

    python -m services.geography.cli status
    python -m services.geography.cli sync
    python -m services.geography.cli audit --email you@example.com

`status` reads and reports what is stored; `sync` materialises the geographic
resolutions of every in-scope posting; `audit` derives one profile's target
country and reports how the stored projection answers it — MATCH,
OUT_OF_TARGET, UNKNOWN — without storing a single verdict.

The output is **counters, country codes, rule ids and versions**, never a
posting's words and never a person's. No description, no title, no location
string, no mobility entry and no email is printed on stdout, in the structured
log or in an error message; a country code is the one geographic value that
appears, and it is a code the registry defines rather than anything an employer
wrote. There is no flag that would print a raw segment.

It runs no migration: `0024` is applied by the ordinary explicit migration
command, exactly like every other one. It refuses a non-SQLite backend before
it connects.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.repository import get_user_profile_by_email
from services.geography.models import RESOLVER_VERSION
from services.geography.repository import read_opportunity_resolutions
from services.geography.service import (
    audit_geographic_targeting,
    in_scope_opportunity_ids,
    load_location_sources,
    summarize_resolutions,
    synchronize_location_resolutions,
)

STATUS_COMMAND = "status"
SYNC_COMMAND = "sync"
AUDIT_COMMAND = "audit"
COMMANDS = (STATUS_COMMAND, SYNC_COMMAND, AUDIT_COMMAND)

NON_SQLITE_BACKEND_ERROR = (
    "the geographic projection writes to local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)
MISSING_EMAIL_ERROR = f"{AUDIT_COMMAND} requires --email: a target belongs to a person"
UNKNOWN_USER_ERROR = (
    "no Digital Twin exists for that address; create it with the digital twin "
    "commands before auditing what it targets"
)

__all__ = [
    "AUDIT_COMMAND",
    "COMMANDS",
    "MISSING_EMAIL_ERROR",
    "NON_SQLITE_BACKEND_ERROR",
    "STATUS_COMMAND",
    "SYNC_COMMAND",
    "UNKNOWN_USER_ERROR",
    "main",
    "parse_args",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve where collected postings say they are, and report whether "
            "that is the country one Digital Twin is looking in. No score, no "
            "ranking, and no stored verdict."
        )
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "--email",
        default=None,
        help=(
            f"Required by {AUDIT_COMMAND}: the Digital Twin whose declared "
            "mobility names the target country. It must already exist."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Read at most this many postings. Omit to read every one in scope.",
    )
    return parser.parse_args(argv)


def _status(connection, limit: int | None) -> dict[str, object]:
    """What is stored, as counters. Reads; writes nothing, resolves nothing."""
    scope = in_scope_opportunity_ids(connection, limit=limit)
    sources = load_location_sources(connection, limit=limit)
    stored = [
        resolution
        for opportunity_id in scope
        for resolution in read_opportunity_resolutions(connection, opportunity_id)
    ]
    read_rows = {resolution.source_location_id for resolution in stored}
    summary: dict[str, object] = {
        "total_opportunities": len(scope),
        "source_location_rows": len(sources),
        "source_location_rows_resolved": len(read_rows),
        "source_location_rows_pending": len(sources) - len(read_rows),
    }
    summary.update(summarize_resolutions(stored))
    summary["resolver_version"] = RESOLVER_VERSION
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. 0 on success, 1 on failure."""
    logger = get_logger("services.geography.cli")
    backend = "unconfigured"
    command = "unparsed"
    try:
        args = parse_args(argv)
        command = args.command
        if command == AUDIT_COMMAND and not args.email:
            raise ValueError(MISSING_EMAIL_ERROR)
        settings = load_settings()
        backend = settings.database_backend.value
        if settings.database_backend is not DatabaseBackend.SQLITE:
            # Refused before connecting: no remote database is contacted.
            raise PermissionError(NON_SQLITE_BACKEND_ERROR)
        connection = connect_configured_database(settings)
        try:
            if command == STATUS_COMMAND:
                summary = _status(connection, args.limit)
            elif command == SYNC_COMMAND:
                summary = synchronize_location_resolutions(
                    connection, limit=args.limit
                ).as_dict()
            else:
                found = get_user_profile_by_email(connection, args.email)
                if found is None:
                    raise LookupError(UNKNOWN_USER_ERROR)
                summary = audit_geographic_targeting(
                    connection, found.profile.id, limit=args.limit
                ).as_dict()
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never a posting's words, never the
        # address the command was given.
        print(f"geographic {command} failed: {type(error).__name__}: {error}")
        logger.error(
            "Geographic command failed.",
            extra={
                "event": "geographic_targeting_failed",
                "command": command,
                "database_backend": backend,
                "resolver_version": RESOLVER_VERSION,
                "error_type": type(error).__name__,
            },
        )
        return 1

    for key, value in summary.items():
        rendered = str(value).casefold() if isinstance(value, bool) else value
        print(f"{key}={rendered}")
    logger.info(
        "Geographic command succeeded.",
        extra={
            "event": "geographic_targeting_succeeded",
            "command": command,
            "database_backend": backend,
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
