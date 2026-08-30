"""Local CLI for the Phase 3.4C explicit profile input.

    python -m services.digital_twin.preferences.cli set-availability ...
    python -m services.digital_twin.preferences.cli set-mobility ...
    python -m services.digital_twin.preferences.cli set-preferences ...
    python -m services.digital_twin.preferences.cli set-career-objectives ...
    python -m services.digital_twin.preferences.cli sync ...
    python -m services.digital_twin.preferences.cli status ...

Four `set-*` commands rather than one, because the four domains are four
statements: somebody can state where they will go without having decided when
they are free, and a single command taking every flag at once would make each
run look like a statement about all four. Each `set-*` validates its input
before touching the database, records or corrects exactly one `USER_INPUT`
fact, then synchronizes the whole projection in one transaction — so the rows
always describe the facts, and no run can leave a profile half-projected.

`sync` re-derives the four projections from the accepted facts without stating
anything, and `status` reports what is known.

Like the Phase 3.4A and 3.4B commands, this one prints **no value read back out
of the database** — no objective, no constraint, no location, no domain, no
availability date and no address — not on stdout, not in the structured log and
not in an error message, and it has no flag that would print one. The counters,
the ids, the action verbs and the `KNOWN`/`UNKNOWN` flags are the whole output.

`status` is the report to read: `KNOWN` means the person stated that domain,
`UNKNOWN` means they did not. `UNKNOWN` is never "no": a person who never said
whether they need visa sponsorship has not said they do not need it.

The values a `set-*` command *receives* are of course the person's own words,
typed by them on the command line; they are written to the fact and never read
back to the terminal. Nothing is prompted for except the address, which is
prompted without echo so it never enters the shell history.

It runs no migration: `0011` is applied by the ordinary explicit migration
command, exactly like every other one.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from getpass import getpass

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.preferences.models import (
    AvailabilityPreference,
    AvailabilityStatus,
    CareerObjectives,
    ConventionStatus,
    MobilityPreference,
    MobilityScope,
    OpportunityPreferences,
    OpportunityType,
    VisaSponsorshipRequired,
    WorkMode,
)
from services.digital_twin.preferences.repository import (
    summarize_profile_preferences,
    synchronize_profile_preferences,
)
from services.digital_twin.preferences.service import (
    set_profile_availability,
    set_profile_career_objectives,
    set_profile_mobility,
    set_profile_preferences,
)
from services.digital_twin.repository import get_user_profile_by_email

SET_AVAILABILITY_COMMAND = "set-availability"
SET_MOBILITY_COMMAND = "set-mobility"
SET_PREFERENCES_COMMAND = "set-preferences"
SET_CAREER_OBJECTIVES_COMMAND = "set-career-objectives"
SYNC_COMMAND = "sync"
STATUS_COMMAND = "status"

COMMANDS = (
    SET_AVAILABILITY_COMMAND,
    SET_MOBILITY_COMMAND,
    SET_PREFERENCES_COMMAND,
    SET_CAREER_OBJECTIVES_COMMAND,
    SYNC_COMMAND,
    STATUS_COMMAND,
)

EMAIL_PROMPT = "Email (not echoed, not logged): "

NON_SQLITE_BACKEND_ERROR = (
    "the explicit profile input writes to local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)
NO_PROFILE_ERROR = (
    "no profile exists for that address; create it explicitly with "
    "'python -m services.digital_twin.cli init-profile' first"
)

__all__ = [
    "COMMANDS",
    "EMAIL_PROMPT",
    "NON_SQLITE_BACKEND_ERROR",
    "NO_PROFILE_ERROR",
    "SET_AVAILABILITY_COMMAND",
    "SET_CAREER_OBJECTIVES_COMMAND",
    "SET_MOBILITY_COMMAND",
    "SET_PREFERENCES_COMMAND",
    "STATUS_COMMAND",
    "SYNC_COMMAND",
    "main",
    "parse_args",
]


def _add_email(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Address of the existing profile. Omit it to be prompted without "
            "echo, so the address never enters the shell history. The profile "
            "must already exist; this command never creates one."
        ),
    )


def _members(registry) -> tuple[str, ...]:
    return tuple(member.value for member in registry)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record the availability, mobility, preferences and career "
            "objectives one person states, and project them. Every value is "
            "explicit user input; nothing is read from a CV."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    availability = subparsers.add_parser(
        SET_AVAILABILITY_COMMAND,
        help="State when this person can start. Never computed from a CV.",
    )
    _add_email(availability)
    availability.add_argument(
        "--status", required=True, choices=_members(AvailabilityStatus)
    )
    availability.add_argument(
        "--available-from",
        default=None,
        help=(
            "YYYY-MM-DD, required by AVAILABLE_FROM and refused by "
            "AVAILABLE_NOW. Never derived from a CV period or from today."
        ),
    )

    mobility = subparsers.add_parser(
        SET_MOBILITY_COMMAND,
        help="State where this person will go. Never read from an address.",
    )
    _add_email(mobility)
    mobility.add_argument("--scope", required=True, choices=_members(MobilityScope))
    mobility.add_argument(
        "--location",
        action="append",
        default=[],
        dest="locations",
        help=(
            "One location, repeatable, in this person's own words. RESTRICTED "
            "requires at least one; nothing is geocoded, expanded or added."
        ),
    )

    preferences = subparsers.add_parser(
        SET_PREFERENCES_COMMAND,
        help="State what this person is looking for and under what conditions.",
    )
    _add_email(preferences)
    preferences.add_argument(
        "--opportunity-type",
        action="append",
        default=[],
        dest="opportunity_types",
        required=True,
        choices=_members(OpportunityType),
        help="One closed-registry opportunity type, repeatable. At least one.",
    )
    preferences.add_argument(
        "--work-mode",
        action="append",
        default=[],
        dest="work_modes",
        required=True,
        choices=_members(WorkMode),
        help="One closed-registry work mode, repeatable. At least one.",
    )
    preferences.add_argument(
        "--preferred-domain",
        action="append",
        default=[],
        dest="preferred_domains",
        help=(
            "One domain, repeatable, in this person's own words. Never taken "
            "from their skills, projects or CV sections."
        ),
    )
    preferences.add_argument(
        "--constraint",
        action="append",
        default=[],
        dest="constraints",
        help="One constraint, repeatable, in this person's own words.",
    )
    preferences.add_argument(
        "--convention-status",
        default=ConventionStatus.UNKNOWN.value,
        choices=_members(ConventionStatus),
        help=(
            "Whether this person says they can obtain an internship agreement. "
            "UNKNOWN is the default and a perfectly valid answer; it is never "
            "inferred from being a student."
        ),
    )
    preferences.add_argument(
        "--visa-sponsorship-required",
        default=VisaSponsorshipRequired.UNKNOWN.value,
        choices=_members(VisaSponsorshipRequired),
        help=(
            "Whether this person says they need sponsorship. UNKNOWN is the "
            "default and a perfectly valid answer; it is never inferred from "
            "a nationality or a location."
        ),
    )

    objectives = subparsers.add_parser(
        SET_CAREER_OBJECTIVES_COMMAND,
        help="State what this person is aiming for, in their own sentences.",
    )
    _add_email(objectives)
    objectives.add_argument(
        "--objective",
        action="append",
        default=[],
        dest="objectives",
        required=True,
        help=(
            "One objective, repeatable, in this person's own words. At least "
            "one; never taken from a CV's professional title."
        ),
    )

    _add_email(
        subparsers.add_parser(
            SYNC_COMMAND,
            help="Re-derive the four projections from the accepted facts.",
        )
    )
    _add_email(
        subparsers.add_parser(
            STATUS_COMMAND,
            help="Report what is known, as flags and counts. Never as values.",
        )
    )
    return parser.parse_args(argv)


def _read_email(provided: str | None, prompt: Callable[[str], str]) -> str:
    return provided if provided is not None else prompt(EMAIL_PROMPT)


def _statement(args: argparse.Namespace):
    """Build the validated statement one `set-*` command carries, or None.

    Validation happens here, before the database is opened: a malformed date or
    a `RESTRICTED` mobility naming nowhere is refused without any write, and
    without any connection.
    """
    if args.command == SET_AVAILABILITY_COMMAND:
        return AvailabilityPreference(
            status=args.status, available_from=args.available_from
        )
    if args.command == SET_MOBILITY_COMMAND:
        return MobilityPreference(scope=args.scope, locations=tuple(args.locations))
    if args.command == SET_PREFERENCES_COMMAND:
        return OpportunityPreferences(
            opportunity_types=tuple(args.opportunity_types),
            work_modes=tuple(args.work_modes),
            preferred_domains=tuple(args.preferred_domains),
            convention_status=args.convention_status,
            visa_sponsorship_required=args.visa_sponsorship_required,
            constraints=tuple(args.constraints),
        )
    if args.command == SET_CAREER_OBJECTIVES_COMMAND:
        return CareerObjectives(objectives=tuple(args.objectives))
    return None


#: Which service call records which statement.
_RECORDERS = {
    SET_AVAILABILITY_COMMAND: set_profile_availability,
    SET_MOBILITY_COMMAND: set_profile_mobility,
    SET_PREFERENCES_COMMAND: set_profile_preferences,
    SET_CAREER_OBJECTIVES_COMMAND: set_profile_career_objectives,
}


def _run(connection, command: str, profile_id: int, statement) -> dict[str, object]:
    """Do what the command asks, and return its privacy-safe summary."""
    if command == STATUS_COMMAND:
        return summarize_profile_preferences(connection, profile_id).as_dict()
    summary: dict[str, object] = {}
    if command in _RECORDERS:
        outcome = _RECORDERS[command](connection, profile_id, statement)
        summary.update(outcome.as_dict())
    # Every `set-*` synchronizes too, so the rows never lag behind the facts a
    # person just stated. `sync` is the same reconciliation on its own.
    summary.update(synchronize_profile_preferences(connection, profile_id).as_dict())
    return summary


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass,
) -> int:
    """Run one command. 0 on success, 1 on failure."""
    logger = get_logger("services.collector.digital_twin.preferences.cli")
    backend = "unconfigured"
    command = "unparsed"
    try:
        args = parse_args(argv)
        command = args.command
        # Validated before anything is opened: a refused statement writes
        # nothing and contacts nothing.
        statement = _statement(args)
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
            summary = _run(connection, command, pair.profile_id, statement)
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never the address and never a value
        # the person typed or a value read out of a fact.
        print(f"explicit profile input failed: {type(error).__name__}: {error}")
        logger.error(
            "Explicit profile input command failed.",
            extra={
                "event": "profile_explicit_input_failed",
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
        "Explicit profile input command succeeded.",
        extra={
            "event": "profile_explicit_input_succeeded",
            "command": command,
            "database_backend": backend,
            # Nested rather than spread flat, like the Phase 3.4B command:
            # `created` is a reserved `LogRecord` attribute — the record's own
            # timestamp — and spreading it would raise.
            "summary": summary,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
