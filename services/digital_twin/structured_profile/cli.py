"""Local CLI that projects the verified facts of Phase 3.4B onto rows.

    python -m services.digital_twin.structured_profile.cli sync

One command, one reconciliation, five fact types: `EXPERIENCE` and `PROJECT`
since Phase 3.4B1, and `EDUCATION`, `CERTIFICATION` and `LANGUAGE` since Phase
3.4B2. There is deliberately no second command and no per-type flag — the
structured profile is one thing, synchronized in one transaction, so a run can
never leave a profile half-projected.

It runs one reconciliation against the configured local SQLite database and
prints what it did as counters. It is not a review command and it decides
nothing: every fact it reads was already accepted by a human in the Phase 3.3B
review, and a fact nobody accepted is invisible to it.

Like the Phase 3.4A skill command, it prints **no** value at all — no role, no
organization, no period, no title, no description, no institution, no diploma,
no certification, no language and no level — not on stdout, not in the
structured log, not in an error message, and it has no flag that would print
one. The counters, the rule tallies and the structurer version are the whole
output. A CV's history is personal content, and a command that needs nobody to
read a value has no reason to display one.

An `unparsed` counter is a property of how a document was written, never a
judgement about the person: a diploma nobody typed with pipes is exactly as
real as one that was.

It runs no migration: `0009` and `0010` are applied by the ordinary explicit
migration command, exactly like every other one.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from getpass import getpass

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.repository import get_user_profile_by_email
from services.digital_twin.structured_profile.repository import (
    synchronize_structured_profile_entries,
)

SYNC_COMMAND = "sync"
EMAIL_PROMPT = "Email (not echoed, not logged): "

NON_SQLITE_BACKEND_ERROR = (
    "the structured profile projection writes to local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)
NO_PROFILE_ERROR = (
    "no profile exists for that address; create it explicitly with "
    "'python -m services.digital_twin.cli init-profile' first"
)

__all__ = [
    "EMAIL_PROMPT",
    "NON_SQLITE_BACKEND_ERROR",
    "NO_PROFILE_ERROR",
    "SYNC_COMMAND",
    "main",
    "parse_args",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Project the ACCEPTED experience, project, education, "
            "certification and language facts of one existing profile onto "
            "structured rows. Reads facts; never decides one."
        )
    )
    parser.add_argument("command", choices=(SYNC_COMMAND,))
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Address of the existing profile. Omit it to be prompted without "
            "echo, so the address never enters the shell history. The profile "
            "must already exist; this command never creates one."
        ),
    )
    return parser.parse_args(argv)


def _read_email(provided: str | None, prompt: Callable[[str], str]) -> str:
    return provided if provided is not None else prompt(EMAIL_PROMPT)


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass,
) -> int:
    """Synchronize one profile's structured rows. 0 on success, 1 on failure."""
    logger = get_logger("services.collector.digital_twin.structured_profile.cli")
    backend = "unconfigured"
    try:
        args = parse_args(argv)
        settings = load_settings()
        backend = settings.database_backend.value
        if settings.database_backend is not DatabaseBackend.SQLITE:
            # Refused before connecting, and before asking for an address the
            # command could not use: no remote database is contacted.
            raise PermissionError(NON_SQLITE_BACKEND_ERROR)
        email = _read_email(args.email, prompt)
        connection = connect_configured_database(settings)
        try:
            pair = get_user_profile_by_email(connection, email)
            if pair is None:
                # An absent profile is an absence, never an invitation to
                # invent one: this command creates no user and no profile.
                raise LookupError(NO_PROFILE_ERROR)
            outcome = synchronize_structured_profile_entries(
                connection, pair.profile_id
            )
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never the address and never a value
        # read out of a fact.
        print(f"structured profile sync failed: {type(error).__name__}: {error}")
        logger.error(
            "Structured profile synchronization failed.",
            extra={
                "event": "structured_profile_sync_failed",
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    summary = outcome.as_dict()
    for key, value in summary.items():
        rendered = str(value).casefold() if isinstance(value, bool) else value
        print(f"{key}={rendered}")
    logger.info(
        "Structured profile synchronization succeeded.",
        extra={
            "event": "structured_profile_sync_succeeded",
            "database_backend": backend,
            # Nested rather than spread flat, unlike the Phase 3.4A command:
            # `created` is a reserved `LogRecord` attribute — the record's own
            # timestamp — and spreading it would raise. Renaming the counter
            # for the log alone would give one number two names, and the name
            # that drifts is the one that gets believed, so the whole summary
            # keeps its single set of names under one key.
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
