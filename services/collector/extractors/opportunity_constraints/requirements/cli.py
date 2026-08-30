"""Local CLI for the Phase 3.5B skill and language requirements.

    python -m services.collector.extractors.opportunity_constraints.requirements.cli status
    python -m services.collector.extractors.opportunity_constraints.requirements.cli sync
    python -m services.collector.extractors.opportunity_constraints.requirements.cli \
        extract-one --opportunity-id 42

`status` reads and reports what is stored, without extracting and without
writing; `sync` reconciles every posting in scope; `extract-one` does the same
for a single posting.

The output is **counters, versions and ids**, never a posting's words. No
description, no title, no evidence fragment, no heading, no skill name and no
language name is printed on stdout, in the structured log or in an error
message, and there is no flag that would print one — the detailed reading is
something to query read-only, not something a routine command dumps. A counter
says how many postings stated something; it never says which, and it never says
whether anybody would suit them, because nothing in this command compares a
posting to a person.

It runs no migration: `0013` is applied by the ordinary explicit migration
command, exactly like every other one. It refuses a non-SQLite backend before it
connects.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.extractors.opportunity_constraints.requirements.models import (
    REQUIREMENT_EXTRACTOR_VERSION,
)
from services.collector.extractors.opportunity_constraints.requirements.repository import (
    read_opportunity_requirements,
)
from services.collector.extractors.opportunity_constraints.requirements.service import (
    extract_one_opportunity_requirements,
    load_requirement_sources,
    summarize_requirements,
    synchronize_opportunity_requirements,
)
from services.collector.logging_config import get_logger

STATUS_COMMAND = "status"
SYNC_COMMAND = "sync"
EXTRACT_ONE_COMMAND = "extract-one"
COMMANDS = (STATUS_COMMAND, SYNC_COMMAND, EXTRACT_ONE_COMMAND)

NON_SQLITE_BACKEND_ERROR = (
    "the opportunity requirement projection writes to local SQLite only; "
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
            "Extract the skills and languages opportunities require, and "
            "project them. Reads postings; compares them to nobody."
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
    loaded = load_requirement_sources(connection, limit=limit)
    stored = [
        reading
        for reading in (
            read_opportunity_requirements(connection, source.opportunity_id)
            for source, _ in loaded
        )
        if reading is not None
    ]
    summary: dict[str, object] = {
        "total_opportunities": len(loaded),
        "constraint_projected": sum(1 for _, projected in loaded if projected),
        "requirements_extracted": len(stored),
        "not_extracted": len(loaded) - len(stored),
    }
    summary.update(summarize_requirements(stored))
    summary["extractor_version"] = REQUIREMENT_EXTRACTOR_VERSION
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. 0 on success, 1 on failure."""
    logger = get_logger(
        "services.collector.extractors.opportunity_constraints.requirements.cli"
    )
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
                summary = synchronize_opportunity_requirements(
                    connection, limit=args.limit
                ).as_dict()
            else:
                reading, written, vocabulary = extract_one_opportunity_requirements(
                    connection, args.opportunity_id
                )
                summary = {
                    "opportunity_id": reading.opportunity_id,
                    "written": written,
                    "extractor_version": reading.extractor_version,
                    "new_skill_vocabulary_rows": vocabulary,
                }
                summary.update(summarize_requirements((reading,)))
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never a posting's words.
        print(
            f"opportunity requirement {command} failed: "
            f"{type(error).__name__}: {error}"
        )
        logger.error(
            "Opportunity requirement command failed.",
            extra={
                "event": "opportunity_requirements_failed",
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
        "Opportunity requirement command succeeded.",
        extra={
            "event": "opportunity_requirements_succeeded",
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
