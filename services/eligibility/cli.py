"""Local CLI for the Phase 3.6 eligibility engine.

    python -m services.eligibility.cli status --email you@example.com
    python -m services.eligibility.cli sync   --email you@example.com
    python -m services.eligibility.cli audit  --email you@example.com

`status` reads and reports what is stored, deciding nothing and writing nothing;
`sync` brings every in-scope posting's decision up to date; `audit` explains the
refusals and the open questions, and re-checks the phase's invariants against
the rows on disk.

The output is **counters, versions, ids and reason codes**, never a posting's
words and never a person's. No description, no title, no evidence fragment, no
CV line, no institution and no email is printed on stdout, in the structured log
or in an error message. The email the command is given identifies the person and
is not echoed back — an operator who typed it knows it, and a log file that
collects it is a log file holding personal data for no reason.

There is no flag that prints a verdict as a percentage, and there is nothing to
build one from: the answer is one of three words.

It runs no migration: `0014` is applied by the ordinary explicit migration
command, exactly like every other one. It refuses a non-SQLite backend before it
connects.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.repository import get_user_profile_by_email
from services.eligibility.audit import audit_eligibility
from services.eligibility.inputs import SCHEMA_LIMITATIONS
from services.eligibility.models import ELIGIBILITY_ENGINE_VERSION
from services.eligibility.service import (
    in_scope_opportunity_ids,
    synchronize_eligibility,
)

STATUS_COMMAND = "status"
SYNC_COMMAND = "sync"
AUDIT_COMMAND = "audit"
COMMANDS = (STATUS_COMMAND, SYNC_COMMAND, AUDIT_COMMAND)

NON_SQLITE_BACKEND_ERROR = (
    "the eligibility engine writes to local SQLite only; set DATABASE_BACKEND=sqlite"
)
UNKNOWN_USER_ERROR = (
    "no Digital Twin exists for that address; create it with the digital twin "
    "commands before deciding anything about it"
)

__all__ = [
    "AUDIT_COMMAND",
    "COMMANDS",
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
            "Decide whether one person can apply to each collected opportunity, "
            "from what the posting explicitly demands and what their Digital "
            "Twin reliably states. Three answers, no score, no ranking."
        )
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "--email",
        required=True,
        help="The Digital Twin to decide for. It must already exist.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Decide at most this many postings. Omit to decide every one in scope.",
    )
    parser.add_argument(
        "--show-reasons",
        type=int,
        default=0,
        metavar="N",
        help=(
            f"{AUDIT_COMMAND} only: list at most N blocking reasons per verdict, "
            "as reason codes and logical references. Never as posting text."
        ),
    )
    return parser.parse_args(argv)


def _status(connection, user_id: int, limit: int | None) -> dict:
    """What is stored and what is in scope. Decides nothing, writes nothing."""
    scope = in_scope_opportunity_ids(connection, limit=limit)
    decided = int(
        connection.execute(
            "SELECT COUNT(*) FROM opportunity_eligibilities WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    )
    verdicts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status, COUNT(*) FROM opportunity_eligibilities "
            "WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
    }
    return {
        "engine_version": ELIGIBILITY_ENGINE_VERSION,
        "in_scope": len(scope),
        "requirements_read": sum(1 for _, read in scope if read),
        "not_read_by_phase_3_5": sum(1 for _, read in scope if not read),
        "decisions_stored": decided,
        "eligible": verdicts.get("ELIGIBLE", 0),
        "unknown": verdicts.get("UNKNOWN", 0),
        "ineligible": verdicts.get("INELIGIBLE", 0),
    }


def _audit(connection, user_id: int, show_reasons: int) -> dict:
    report = audit_eligibility(connection, user_id)
    summary: dict[str, object] = {"engine_version": ELIGIBILITY_ENGINE_VERSION}
    summary.update(report.as_dict())
    if show_reasons > 0:
        summary["ineligible_reason_sample"] = [
            {
                "opportunity_id": reason.opportunity_id,
                "dimension": reason.dimension,
                "reason_code": reason.reason_code,
                "requirement_ref": reason.requirement_ref,
                "profile_ref": reason.profile_ref,
            }
            for reason in report.ineligible_reasons[:show_reasons]
        ]
        summary["unknown_reason_sample"] = [
            {
                "opportunity_id": reason.opportunity_id,
                "dimension": reason.dimension,
                "reason_code": reason.reason_code,
                "requirement_ref": reason.requirement_ref,
            }
            for reason in report.unknown_reasons[:show_reasons]
        ]
    summary["schema_limitations"] = list(SCHEMA_LIMITATIONS)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. 0 on success, 1 on failure."""
    logger = get_logger("services.eligibility.cli")
    backend = "unconfigured"
    command = "unparsed"
    try:
        args = parse_args(argv)
        command = args.command
        settings = load_settings()
        backend = settings.database_backend.value
        if settings.database_backend is not DatabaseBackend.SQLITE:
            # Refused before connecting: no remote database is contacted.
            raise PermissionError(NON_SQLITE_BACKEND_ERROR)
        connection = connect_configured_database(settings)
        try:
            found = get_user_profile_by_email(connection, args.email)
            if found is None:
                raise LookupError(UNKNOWN_USER_ERROR)
            user_id = found.user.id
            profile_id = found.profile.id
            if command == STATUS_COMMAND:
                summary = _status(connection, user_id, args.limit)
            elif command == SYNC_COMMAND:
                summary = synchronize_eligibility(
                    connection, user_id, profile_id, limit=args.limit
                ).as_dict()
            else:
                summary = _audit(connection, user_id, args.show_reasons)
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never a posting's words, never the
        # address the command was given.
        print(f"eligibility {command} failed: {type(error).__name__}: {error}")
        logger.error(
            "Eligibility command failed.",
            extra={
                "event": "eligibility_failed",
                "command": command,
                "database_backend": backend,
                "engine_version": ELIGIBILITY_ENGINE_VERSION,
                "error_type": type(error).__name__,
            },
        )
        return 1

    for key, value in summary.items():
        rendered = str(value).casefold() if isinstance(value, bool) else value
        print(f"{key}={rendered}")
    logger.info(
        "Eligibility command succeeded.",
        extra={
            "event": "eligibility_succeeded",
            "command": command,
            "database_backend": backend,
            "engine_version": ELIGIBILITY_ENGINE_VERSION,
            # Nested: `created` is a reserved `LogRecord` attribute and
            # spreading the summary flat would raise.
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
