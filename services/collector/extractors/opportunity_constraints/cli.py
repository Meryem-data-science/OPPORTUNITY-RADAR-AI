"""Local CLI for the Phase 3.5A opportunity constraints.

    python -m services.collector.extractors.opportunity_constraints.cli status
    python -m services.collector.extractors.opportunity_constraints.cli sync
    python -m services.collector.extractors.opportunity_constraints.cli \
        extract-one --opportunity-id 42

`status` reads and reports; `sync` reconciles every posting in scope;
`extract-one` does the same for a single posting, which is the command to reach
for when checking why one listing reads the way it does.

The output is **counters, rule ids and versions**, never a posting's words. No
description, no title, no evidence fragment and no location is printed on
stdout, in the structured log or in an error message, and there is no flag that
would print one. A `known_*` counter says how many postings stated something;
it never says which, and it never says whether anybody would suit them —
nothing in this command compares a posting to a person.

It runs no migration: `0012` is applied by the ordinary explicit migration
command, exactly like every other one. It refuses a non-SQLite backend before
it connects.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.extractors.opportunity_constraints.models import (
    EXTRACTOR_VERSION,
)
from services.collector.extractors.opportunity_constraints.repository import (
    read_opportunity_constraints,
)
from services.collector.extractors.opportunity_constraints.service import (
    extract_one_opportunity,
    load_opportunity_sources,
    summarize_constraints,
    synchronize_opportunity_constraints,
)
from services.collector.logging_config import get_logger

STATUS_COMMAND = "status"
SYNC_COMMAND = "sync"
EXTRACT_ONE_COMMAND = "extract-one"
COMMANDS = (STATUS_COMMAND, SYNC_COMMAND, EXTRACT_ONE_COMMAND)

NON_SQLITE_BACKEND_ERROR = (
    "the opportunity constraint projection writes to local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)

__all__ = [
    "COMMANDS",
    "EXTRACT_ONE_COMMAND",
    "NON_SQLITE_BACKEND_ERROR",
    "STATUS_COMMAND",
    "SYNC_COMMAND",
    "main",
    "parse_args",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract what opportunities require, and project it. Reads "
            "postings; compares them to nobody."
        )
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "--opportunity-id",
        type=int,
        default=None,
        help=f"Required by {EXTRACT_ONE_COMMAND}: the posting to read.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Read at most this many postings. Omit to read every one in scope.",
    )
    return parser.parse_args(argv)


def _status(connection, limit: int | None) -> dict[str, object]:
    """What is stored, as counters. Reads; writes nothing, extracts nothing."""
    sources = load_opportunity_sources(connection, limit=limit)
    stored = [
        reading
        for reading in (
            read_opportunity_constraints(connection, source.opportunity_id)
            for source in sources
        )
        if reading is not None
    ]
    summary: dict[str, object] = {
        "total_opportunities": len(sources),
        "projected": len(stored),
        "not_projected": len(sources) - len(stored),
    }
    summary.update(summarize_constraints(stored))
    summary["extractor_version"] = EXTRACTOR_VERSION
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. 0 on success, 1 on failure."""
    logger = get_logger("services.collector.extractors.opportunity_constraints.cli")
    backend = "unconfigured"
    command = "unparsed"
    try:
        args = parse_args(argv)
        command = args.command
        if command == EXTRACT_ONE_COMMAND and args.opportunity_id is None:
            raise ValueError(f"{EXTRACT_ONE_COMMAND} requires --opportunity-id")
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
                summary = synchronize_opportunity_constraints(
                    connection, limit=args.limit
                ).as_dict()
            else:
                reading, written = extract_one_opportunity(
                    connection, args.opportunity_id
                )
                summary = {
                    "opportunity_id": reading.opportunity_id,
                    "written": written,
                    "extractor_version": reading.extractor_version,
                    "evidence_count": len(reading.evidence),
                }
                summary.update(summarize_constraints((reading,)))
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never a posting's words.
        print(f"opportunity constraint {command} failed: {type(error).__name__}: {error}")
        logger.error(
            "Opportunity constraint command failed.",
            extra={
                "event": "opportunity_constraints_failed",
                "command": command,
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    for key, value in summary.items():
        rendered = str(value).casefold() if isinstance(value, bool) else value
        print(f"{key}={rendered}")
    logger.info(
        "Opportunity constraint command succeeded.",
        extra={
            "event": "opportunity_constraints_succeeded",
            "command": command,
            "database_backend": backend,
            # Nested: `created` is a reserved `LogRecord` attribute and
            # spreading the summary flat would raise.
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
