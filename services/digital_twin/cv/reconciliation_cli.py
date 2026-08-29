"""Local CLI that reconciles a re-read CV with the facts an older read produced.

    python -m services.digital_twin.cv.reconciliation_cli plan     /path/to/cv.pdf
    python -m services.digital_twin.cv.reconciliation_cli prepare  /path/to/cv.pdf
    python -m services.digital_twin.cv.reconciliation_cli finalize /path/to/cv.pdf

Three commands, in the order they are meant to be run, with a human in between:

* `plan` writes nothing at all. It parses the PDF, extracts the current
  candidates, compares them with the facts the older campaign produced for the
  same document, and prints the counters. It is the way to look before writing;
* `prepare` attaches the current campaign's evidence to every reading that has
  not changed, and proposes the ones that have. It accepts nothing — the new
  readings come out `PROPOSED`, and the ordinary review is what answers them:

      python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf

* `finalize` retires the old readings the confirmed new ones replaced. It
  refuses to run while any new reading is still unanswered, and it rejects
  rather than deletes, so both segmentations stay auditable.

All three are safe to re-run: `prepare` writes nothing the second time and
`finalize` rejects nothing the second time.

Like the skill projection and unlike the review, this command prints **no** CV
content: no value, no `raw_text`, no name, employer, school, project or skill
mention reaches stdout, the structured log or an error message, and there is no
flag that would print one. The output is counters, canonical fact types,
version strings and the document digest. The path of the PDF is not printed
either, not even on failure, because a CV filename usually carries the person's
name.

It writes to local SQLite only, to `profile_facts` and
`profile_fact_provenance`, and to nothing else. It runs no migration, touches
no skill, opportunity or matching row, reads no network, loads no model and
calls no API.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from getpass import getpass

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.cv.candidates.extractor import extract_candidates
from services.digital_twin.cv.parser import parse_cv_pdf
from services.digital_twin.cv.reconciliation import (
    PREVIOUS_EXTRACTOR_VERSION,
    PREVIOUS_PARSER_VERSION,
    UnresolvedChangedReadingError,
    compute_cv_reconciliation_plan,
    extraction_is_current,
    finalize_cv_fact_reconciliation,
    prepare_cv_fact_reconciliation,
)
from services.digital_twin.repository import get_user_profile_by_email

PLAN_COMMAND = "plan"
PREPARE_COMMAND = "prepare"
FINALIZE_COMMAND = "finalize"
COMMANDS = (PLAN_COMMAND, PREPARE_COMMAND, FINALIZE_COMMAND)

EMAIL_PROMPT = "Email (not echoed, not logged): "

NON_SQLITE_BACKEND_ERROR = (
    "the CV reconciliation writes profile facts to local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)
NO_PROFILE_ERROR = (
    "no profile exists for that address; create it explicitly with "
    "'python -m services.digital_twin.cli init-profile' first"
)
STALE_EXTRACTION_ERROR = (
    "this checkout does not produce the newer campaign this reconciliation "
    "would need; re-run it from a checkout whose parser and extractor versions "
    "are the ones you are reconciling towards"
)

PLAN_NOTICE = "nothing was written; this is what prepare would do"
PREPARE_NOTICE = (
    "the changed readings are PROPOSED and nobody has decided them; review them "
    "with 'python -m services.digital_twin.cv.review_cli review', then finalize"
)
FINALIZE_NOTICE = (
    "the superseded readings are REJECTED, not deleted; their values and their "
    "own provenance stay readable"
)
UNRESOLVED_NOTICE = (
    "no old fact was rejected; review the new readings first, then finalize again"
)

__all__ = [
    "COMMANDS",
    "EMAIL_PROMPT",
    "FINALIZE_COMMAND",
    "FINALIZE_NOTICE",
    "NON_SQLITE_BACKEND_ERROR",
    "NO_PROFILE_ERROR",
    "PLAN_COMMAND",
    "PLAN_NOTICE",
    "PREPARE_COMMAND",
    "PREPARE_NOTICE",
    "STALE_EXTRACTION_ERROR",
    "UNRESOLVED_NOTICE",
    "main",
    "parse_args",
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the current reading of one local CV PDF with the facts "
            "an older parser and extractor produced for the same document. "
            "Creates no duplicate, deletes nothing, and accepts nothing."
        )
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("pdf_path", help="Path to a local PDF. Never printed back.")
    parser.add_argument(
        "--email",
        default=None,
        help=(
            "Address of the existing profile. Omit it to be prompted without "
            "echo, so the address never enters the shell history. The profile "
            "must already exist; this command never creates one."
        ),
    )
    parser.add_argument(
        "--old-parser-version",
        default=PREVIOUS_PARSER_VERSION,
        help=(
            "Parser version of the campaign being reconciled away from. A "
            "technical version string, never CV content. Default: "
            f"{PREVIOUS_PARSER_VERSION}."
        ),
    )
    parser.add_argument(
        "--old-extractor-version",
        default=PREVIOUS_EXTRACTOR_VERSION,
        help=(
            "Extractor version of that same campaign. Default: "
            f"{PREVIOUS_EXTRACTOR_VERSION}."
        ),
    )
    return parser.parse_args(argv)


def _read_email(provided: str | None, prompt: Callable[[str], str]) -> str:
    return provided if provided is not None else prompt(EMAIL_PROMPT)


def _render(summary: dict[str, object]) -> None:
    """Print the report line by line. Every entry is a counter or a name."""
    for key, value in summary.items():
        if isinstance(value, bool):
            print(f"{key}={str(value).casefold()}")
        elif isinstance(value, dict):
            print(f"{key}={len(value)}")
            for entry_key in value:
                print(f"  {entry_key}={value[entry_key]}")
        else:
            print(f"{key}={value}")


def _report_unresolved(error: UnresolvedChangedReadingError) -> None:
    """Say which new readings are still unanswered, by type, status and id."""
    print(f"unresolved_changed_readings={len(error.unresolved)}")
    for entry in error.unresolved:
        fact_id = entry.get("fact_id", "(none)")
        print(
            f"  fact_type={entry['fact_type']} fact_id={fact_id} "
            f"reason={entry['reason']}"
        )
    print(UNRESOLVED_NOTICE)


def _run(connection, profile_id: int, args, extraction):
    """Dispatch to the one operation the command names. Returns its summary."""
    if args.command == PLAN_COMMAND:
        plan = compute_cv_reconciliation_plan(
            connection,
            profile_id=profile_id,
            extraction=extraction,
            old_parser_version=args.old_parser_version,
            old_extractor_version=args.old_extractor_version,
        )
        return plan.summary(), PLAN_NOTICE
    if args.command == PREPARE_COMMAND:
        preparation = prepare_cv_fact_reconciliation(
            connection,
            profile_id=profile_id,
            extraction=extraction,
            old_parser_version=args.old_parser_version,
            old_extractor_version=args.old_extractor_version,
        )
        return preparation.summary(), PREPARE_NOTICE
    finalization = finalize_cv_fact_reconciliation(
        connection,
        profile_id=profile_id,
        extraction=extraction,
        old_parser_version=args.old_parser_version,
        old_extractor_version=args.old_extractor_version,
    )
    return finalization.summary(), FINALIZE_NOTICE


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass,
) -> int:
    """Plan, prepare or finalize one reconciliation. 0 on success, 1 on failure."""
    logger = get_logger("services.collector.digital_twin.cv.reconciliation_cli")
    backend = "unconfigured"
    command = "unparsed"
    unresolved: UnresolvedChangedReadingError | None = None
    try:
        args = parse_args(argv)
        command = args.command
        settings = load_settings()
        backend = settings.database_backend.value
        if settings.database_backend is not DatabaseBackend.SQLITE:
            # Refused before connecting, and before asking for an address the
            # command could not use: no remote database is contacted.
            raise PermissionError(NON_SQLITE_BACKEND_ERROR)
        extraction = extract_candidates(parse_cv_pdf(args.pdf_path))
        if not extraction_is_current(extraction):
            # Reconciling *towards* a stale reading would retire good facts in
            # favour of an older segmentation. Refused before any connection.
            raise RuntimeError(STALE_EXTRACTION_ERROR)
        email = _read_email(args.email, prompt)
        connection = connect_configured_database(settings)
        try:
            pair = get_user_profile_by_email(connection, email)
            if pair is None:
                # An absent profile is an absence, never an invitation to
                # invent one: this command creates no user and no profile.
                raise LookupError(NO_PROFILE_ERROR)
            summary, notice = _run(connection, pair.profile_id, args, extraction)
        finally:
            connection.close()
    except UnresolvedChangedReadingError as error:
        # A refusal, not a crash: the state it describes is one a human moves.
        print(f"cv reconciliation refused: {error}")
        _report_unresolved(error)
        unresolved = error
        logger.info(
            "CV reconciliation refused to finalize.",
            extra={
                "event": "cv_reconciliation_refused",
                "database_backend": backend,
                "command": command,
                "unresolved_changed_readings": len(unresolved.unresolved),
            },
        )
        return 1
    except Exception as error:
        # The message quotes the failure, never the address, the CV path or any
        # value read out of the document.
        print(f"cv reconciliation failed: {type(error).__name__}: {error}")
        logger.error(
            "CV reconciliation command failed.",
            extra={
                "event": "cv_reconciliation_command_failed",
                "database_backend": backend,
                "command": command,
                "error_type": type(error).__name__,
            },
        )
        return 1

    _render(summary)
    print(notice)
    logger.info(
        "CV reconciliation command succeeded.",
        extra={
            "event": "cv_reconciliation_command_succeeded",
            "database_backend": backend,
            "command": command,
            # The nested tallies are canonical fact types and counts; they are
            # flattened out of the log event rather than nested in it.
            **{
                key: value
                for key, value in summary.items()
                if not isinstance(value, dict)
            },
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
