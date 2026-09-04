"""Local CLI for the Phase 7B.1 opportunity-type targeting foundation.

    python -m services.targeting.opportunity_type.cli audit --email you@example.com
    python -m services.targeting.opportunity_type.cli explain --email you@example.com \\
        --opportunity-id 232

`audit` derives one profile's declared target set and reports how the stored
structured types answer it — MATCH, OUT_OF_TARGET, UNKNOWN — together with the
distribution of those types, without storing a single verdict. `explain` does
the same for one posting and names the rule that decided it, which is how a
stored type that looks wrong gets **inspected** instead of quietly corrected.

There is no `sync` command, because there is nothing to synchronize: this slice
projects nothing. Both commands read.

The output is **counters, registry values, rule ids and versions**, never a
posting's words and never a person's. No title, organization, description,
URL, location, domain, constraint, career objective or email is printed on
stdout, in the structured log or in an error message. The opportunity id an
`explain` was asked about is echoed, because an operator who typed it already
has it and an audit that could not name the row it explained would explain
nothing; the address they typed is not, because a log file that collects it
holds personal data for no reason.

It runs no migration: `0012` is applied by the ordinary explicit migration
command, exactly like every other one. Phase 7B.1 adds none of its own. It
refuses a non-SQLite backend before it connects.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.repository import get_user_profile_by_email
from services.targeting.opportunity_type.models import TARGETING_VERSION
from services.targeting.opportunity_type.profile_target import (
    resolve_profile_type_target,
)
from services.targeting.opportunity_type.service import (
    audit_opportunity_type_targeting,
    explain_opportunity_type_target,
)

AUDIT_COMMAND = "audit"
EXPLAIN_COMMAND = "explain"
COMMANDS = (AUDIT_COMMAND, EXPLAIN_COMMAND)

NON_SQLITE_BACKEND_ERROR = (
    "the type-targeting audit reads local SQLite only; set DATABASE_BACKEND=sqlite"
)
MISSING_EMAIL_ERROR = "this command requires --email: a target belongs to a person"
MISSING_OPPORTUNITY_ERROR = (
    f"{EXPLAIN_COMMAND} requires --opportunity-id: one posting is explained at a time"
)
UNKNOWN_USER_ERROR = (
    "no Digital Twin exists for that address; create it with the digital twin "
    "commands before auditing what it targets"
)

__all__ = [
    "AUDIT_COMMAND",
    "COMMANDS",
    "EXPLAIN_COMMAND",
    "MISSING_EMAIL_ERROR",
    "MISSING_OPPORTUNITY_ERROR",
    "NON_SQLITE_BACKEND_ERROR",
    "UNKNOWN_USER_ERROR",
    "main",
    "parse_args",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report whether the structured type of each collected posting is a "
            "type one Digital Twin explicitly said it is looking for. No score, "
            "no ranking, no re-classification and no stored verdict."
        )
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Required: the Digital Twin whose stated preferences name the "
            "target types. It must already exist."
        ),
    )
    parser.add_argument(
        "--opportunity-id",
        type=int,
        default=None,
        help=f"Required by {EXPLAIN_COMMAND}: the one posting to explain.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Read at most this many postings. Omit to read every one in scope.",
    )
    return parser.parse_args(argv)


def _explain(connection, profile_id: int, opportunity_id: int) -> dict[str, object]:
    """One posting's verdict and the rule behind it. Never the posting's words."""
    target = resolve_profile_type_target(connection, profile_id)
    reading, assessment = explain_opportunity_type_target(
        connection, profile_id, opportunity_id, target=target
    )
    summary: dict[str, object] = {
        "profile_id": profile_id,
        "opportunity_id": reading.opportunity_id,
        "constraints_read": reading.constraints_read,
        "profile_target_known": target.known,
        "target_types": ",".join(value.value for value in target.opportunity_types)
        or "UNKNOWN",
        "target_rule_id": target.rule_id,
    }
    summary.update(assessment.as_dict())
    summary["targeting_version"] = TARGETING_VERSION
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command. 0 on success, 1 on failure."""
    logger = get_logger("services.targeting.opportunity_type.cli")
    backend = "unconfigured"
    command = "unparsed"
    try:
        args = parse_args(argv)
        command = args.command
        if not args.email:
            raise ValueError(MISSING_EMAIL_ERROR)
        if command == EXPLAIN_COMMAND and args.opportunity_id is None:
            raise ValueError(MISSING_OPPORTUNITY_ERROR)
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
            if command == AUDIT_COMMAND:
                summary = audit_opportunity_type_targeting(
                    connection, found.profile.id, limit=args.limit
                ).as_dict()
            else:
                summary = _explain(
                    connection, found.profile.id, args.opportunity_id
                )
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never a posting's words, never the
        # address the command was given.
        print(f"opportunity type {command} failed: {type(error).__name__}: {error}")
        logger.error(
            "Opportunity type targeting command failed.",
            extra={
                "event": "opportunity_type_targeting_failed",
                "command": command,
                "database_backend": backend,
                "targeting_version": TARGETING_VERSION,
                "error_type": type(error).__name__,
            },
        )
        return 1

    for key, value in summary.items():
        rendered = str(value).casefold() if isinstance(value, bool) else value
        print(f"{key}={rendered}")
    logger.info(
        "Opportunity type targeting command succeeded.",
        extra={
            "event": "opportunity_type_targeting_succeeded",
            "command": command,
            "database_backend": backend,
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
