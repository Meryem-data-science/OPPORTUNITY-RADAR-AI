"""Local CLI to review what a CV proposed, one candidate at a time.

    python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf

It runs the whole Phase 3.3B path in one command — parse the PDF, extract the
candidates, import them idempotently as `PROPOSED` facts, then ask a human
about each one that is still undecided — and it is the only command in the
project that deliberately prints CV content.

That exception is the point of the command: a person cannot accept or reject a
value they are not shown. It is bounded on every side. The values reach the
terminal and nowhere else — never the structured log, which carries counters,
versions, canonical types and ids only; never the closing summary, which is
counters; and never a file, because this command writes none. The path of the
CV is not printed either, not even in an error message, because a CV filename
usually carries the person's name.

Nothing here decides anything by itself. There is no accept-all, no
yes-to-all, no auto-accept, no threshold, no confidence and no score: every
fact that becomes `ACCEPTED` does so because somebody typed `a` in front of it.
Quitting is safe and resuming is expected — undecided facts stay `PROPOSED`,
decided ones are never asked again, and re-running the command re-imports
nothing.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from getpass import getpass

from services.collector.config import DatabaseBackend, load_settings
from services.collector.database.connection import connect_configured_database
from services.collector.logging_config import get_logger
from services.digital_twin.cv.candidates.extractor import extract_candidates
from services.digital_twin.cv.fact_bridge import (
    CvImportResult,
    ImportedCandidate,
    import_cv_candidates,
)
from services.digital_twin.cv.parser import parse_cv_pdf
from services.digital_twin.facts.models import FactStatus
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    correct_profile_fact,
    get_profile_fact,
    reject_profile_fact,
)
from services.digital_twin.repository import get_user_profile_by_email

REVIEW_COMMAND = "review"

EMAIL_PROMPT = "Email (not echoed, not logged): "
ACTION_PROMPT = "[a]ccept [r]eject [c]orrect [s]kip [q]uit: "
CORRECTION_PROMPT = "Corrected value: "

NON_SQLITE_BACKEND_ERROR = (
    "the CV review writes profile facts to local SQLite only; "
    "set DATABASE_BACKEND=sqlite"
)
NO_PROFILE_ERROR = (
    "no profile exists for that address; create it explicitly with "
    "'python -m services.digital_twin.cli init-profile' first"
)
INVALID_ACTION_NOTICE = "not an action; nothing was decided"
EMPTY_CORRECTION_NOTICE = "a correction needs a value; nothing was decided"
SENSITIVE_OUTPUT_NOTICE = (
    "this command prints CV content so you can decide; it is logged nowhere"
)
NOTHING_TO_REVIEW_NOTICE = "nothing left to review for this CV"

#: The five decisions, and the only five. They are matched exactly, so a
#: capital, a stray space or anything longer decides nothing and asks again: a
#: mistyped key must never accept somebody's CV for them.
ACCEPT_ACTION = "a"
REJECT_ACTION = "r"
CORRECT_ACTION = "c"
SKIP_ACTION = "s"
QUIT_ACTION = "q"

__all__ = [
    "ACTION_PROMPT",
    "CORRECTION_PROMPT",
    "EMAIL_PROMPT",
    "EMPTY_CORRECTION_NOTICE",
    "INVALID_ACTION_NOTICE",
    "NON_SQLITE_BACKEND_ERROR",
    "NOTHING_TO_REVIEW_NOTICE",
    "NO_PROFILE_ERROR",
    "REVIEW_COMMAND",
    "SENSITIVE_OUTPUT_NOTICE",
    "ReviewOutcome",
    "main",
    "parse_args",
]


@dataclass
class ReviewOutcome:
    """What one review run did. Counters only: no value is ever kept here."""

    candidates: int = 0
    newly_proposed: int = 0
    already_imported: int = 0
    accepted: int = 0
    rejected: int = 0
    corrected: int = 0
    skipped: int = 0
    remaining_proposed: int = 0
    quit_requested: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": self.candidates,
            "newly_proposed": self.newly_proposed,
            "already_imported": self.already_imported,
            "accepted_this_run": self.accepted,
            "rejected_this_run": self.rejected,
            "corrected_this_run": self.corrected,
            "skipped_this_run": self.skipped,
            "remaining_proposed": self.remaining_proposed,
            "quit_requested": self.quit_requested,
        }


@dataclass
class _Session:
    """The one place a decision is taken, so every path through it is visible."""

    connection: object
    profile_id: int
    ask: Callable[[str], str]
    outcome: ReviewOutcome = field(default_factory=ReviewOutcome)

    def _prompt(self, message: str) -> str | None:
        """Read one answer, or None when the input ended. None means quit."""
        try:
            return self.ask(message)
        except EOFError:
            return None

    def decide(self, entry: ImportedCandidate) -> bool:
        """Ask about one fact until an action is taken. False means stop.

        The loop is what enforces "no other character decides": an unknown
        answer prints a notice and asks again, it does not fall through to a
        default, and it does not skip either — skipping is `s`, typed on
        purpose.

        The comparison is exact. `"A"`, `"a "` and `" a"` are not the answer
        `a`, and none of them accepts anything: tidying an answer up before
        reading it would be this command guessing what somebody meant, on the
        one prompt where guessing is the whole thing to avoid. The correction
        value is a different matter and is still read as typed, then trimmed —
        that is a value, not a decision.
        """
        fact_id = entry.fact.id
        while True:
            action = self._prompt(ACTION_PROMPT)
            if action is None:
                self.outcome.quit_requested = True
                return False
            if action == ACCEPT_ACTION:
                accept_profile_fact(self.connection, self.profile_id, fact_id)
                self.outcome.accepted += 1
                return True
            if action == REJECT_ACTION:
                reject_profile_fact(self.connection, self.profile_id, fact_id)
                self.outcome.rejected += 1
                return True
            if action == CORRECT_ACTION:
                if self._correct(fact_id):
                    return True
                continue
            if action == SKIP_ACTION:
                self.outcome.skipped += 1
                return True
            if action == QUIT_ACTION:
                self.outcome.quit_requested = True
                return False
            print(INVALID_ACTION_NOTICE)

    def _correct(self, fact_id: int) -> bool:
        """Replace one value with what the person typed. False if they gave none.

        The replacement is a new `ACCEPTED` fact carrying `USER_INPUT`
        evidence; the previous fact keeps its own value and becomes
        `CORRECTED`. Nothing is normalized on the way in — what the person
        typed is what is stored, and deriving a "cleaner" form of it would be
        this bridge interpreting a human's own statement.
        """
        typed = self._prompt(CORRECTION_PROMPT)
        if typed is None or typed.strip() == "":
            print(EMPTY_CORRECTION_NOTICE)
            return False
        correct_profile_fact(
            self.connection, self.profile_id, fact_id, value=typed.strip()
        )
        self.outcome.corrected += 1
        return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import one local CV PDF as proposed profile facts, then review "
            "each of them by hand. Nothing is accepted automatically."
        )
    )
    parser.add_argument("command", choices=(REVIEW_COMMAND,))
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
    return parser.parse_args(argv)


def _read_email(provided: str | None, prompt: Callable[[str], str]) -> str:
    return provided if provided is not None else prompt(EMAIL_PROMPT)


def _describe_provenance(entry: ImportedCandidate) -> str:
    """Where the value was read and by which rule. Carries no CV text."""
    candidate = entry.candidate
    pages = (
        ",".join(str(number) for number in candidate.page_numbers)
        if candidate.page_numbers
        else "(none)"
    )
    if candidate.section_type is None:
        section = "(none)"
    elif candidate.section_index is None:
        section = candidate.section_type.value
    else:
        section = f"{candidate.section_type.value}#{candidate.section_index}"
    return f"pages={pages} section={section} rule={candidate.rule_id.value}"


def _present(entry: ImportedCandidate, position: int, total: int) -> None:
    """Show one undecided fact. This is the one place CV text is printed."""
    print("")
    print(f"[{position}/{total}] {entry.fact.fact_type} (fact_id={entry.fact.id})")
    print(f"  value: {entry.fact.value}")
    if entry.fact.normalized_value is not None:
        print(f"  normalized: {entry.fact.normalized_value}")
    print(f"  {_describe_provenance(entry)}")


def _count_still_proposed(
    connection, profile_id: int, result: CvImportResult
) -> int:
    """How many facts of this CV nobody has decided, read back from the database.

    Recounted rather than deduced from the run's own tally: a fact could have
    been decided elsewhere while this command was running, and the summary
    should state what is true now.
    """
    still_proposed = 0
    for entry in result.imported:
        fact = get_profile_fact(connection, profile_id, entry.fact.id)
        if fact is not None and fact.status is FactStatus.PROPOSED:
            still_proposed += 1
    return still_proposed


def _review(
    connection, profile_id: int, result: CvImportResult, ask: Callable[[str], str]
) -> ReviewOutcome:
    session = _Session(connection=connection, profile_id=profile_id, ask=ask)
    session.outcome.candidates = result.candidate_count
    session.outcome.newly_proposed = result.newly_proposed
    session.outcome.already_imported = result.already_imported

    # Only the undecided ones: a fact already accepted, rejected or corrected
    # for this same proof has been answered, and asking again would invite an
    # answer that contradicts the one on record.
    pending = result.pending_review
    if not pending:
        print(NOTHING_TO_REVIEW_NOTICE)
    else:
        print(SENSITIVE_OUTPUT_NOTICE)
        for position, entry in enumerate(pending, start=1):
            _present(entry, position, len(pending))
            if not session.decide(entry):
                break

    session.outcome.remaining_proposed = _count_still_proposed(
        connection, profile_id, result
    )
    return session.outcome


def _report(outcome: ReviewOutcome) -> None:
    """Print the counters. Reads no value out of any fact or candidate."""
    print("")
    for key, value in outcome.as_dict().items():
        rendered = str(value).casefold() if isinstance(value, bool) else value
        print(f"{key}={rendered}")


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Callable[[str], str] = getpass,
    ask: Callable[[str], str] = input,
) -> int:
    """Import and review one local CV PDF. 0 on success, 1 on failure."""
    logger = get_logger("services.collector.digital_twin.cv.review_cli")
    backend = "unconfigured"
    try:
        args = parse_args(argv)
        settings = load_settings()
        backend = settings.database_backend.value
        if settings.database_backend is not DatabaseBackend.SQLITE:
            # Refused before connecting, and before asking for an address the
            # command could not store: no remote database is contacted.
            raise PermissionError(NON_SQLITE_BACKEND_ERROR)
        email = _read_email(args.email, prompt)
        connection = connect_configured_database(settings)
        try:
            pair = get_user_profile_by_email(connection, email)
            if pair is None:
                # An absent profile is an absence, never an invitation to
                # invent one: this command creates no user and no profile.
                raise LookupError(NO_PROFILE_ERROR)
            extraction = extract_candidates(parse_cv_pdf(args.pdf_path))
            result = import_cv_candidates(
                connection, profile_id=pair.profile_id, extraction=extraction
            )
            outcome = _review(connection, pair.profile_id, result, ask)
        finally:
            connection.close()
    except Exception as error:
        # The message quotes the failure, never the address, the CV path or any
        # value read out of the document.
        print(f"cv review failed: {type(error).__name__}: {error}")
        logger.error(
            "CV review command failed.",
            extra={
                "event": "cv_review_command_failed",
                "database_backend": backend,
                "error_type": type(error).__name__,
            },
        )
        return 1

    _report(outcome)
    logger.info(
        "CV review command succeeded.",
        extra={
            "event": "cv_review_command_succeeded",
            "database_backend": backend,
            "parser_version": result.parser_version,
            "extractor_version": result.extractor_version,
            **outcome.as_dict(),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
