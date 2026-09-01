"""Local CLI to review what a CV proposed, by hand and in two shapes.

    python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf
    python -m services.digital_twin.cv.review_cli review /path/to/cv.pdf --grouped

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

The default shape is the original one: one fact, one question, one keystroke.
`--grouped` changes how many facts one question covers, and nothing else. It
prints a whole group — the contacts, the studies, the forty-three skills — waits
while the reader looks at it, and then accepts the ones still proposed *in that
printed group* on one explicit `a`. That is still an explicit human action on
values that were shown; what it is not is fifty-nine of them. Every refusal
above survives it: there is no group nobody saw, no acceptance beyond the group
on screen, no command for the whole CV, and no input error that ends in an
acceptance.
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
from services.digital_twin.cv.grouped_review import (
    CORRECT_NEEDS_ONE_INDEX_NOTICE,
    GROUP_ACTION_PROMPT,
    GROUP_HELP_TEXT,
    GroupAction,
    GroupCommand,
    INVALID_COMMAND_NOTICE,
    NOTHING_PENDING_NOTICE,
    REJECT_NEEDS_INDICES_NOTICE,
    ReviewGroup,
    UNKNOWN_INDEX_NOTICE,
    build_review_groups,
    parse_group_command,
    selection_is_reviewable,
)
from services.digital_twin.cv.parser import parse_cv_pdf
from services.digital_twin.facts.models import FactStatus
from services.digital_twin.facts.repository import (
    accept_profile_fact,
    correct_profile_fact,
    decide_profile_facts,
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
NOT_DISPLAYED_NOTICE = (
    "that group is not the one on screen; nothing was decided"
)

#: Above this many facts, one `a` is asked to say so twice. A group of five is
#: read in a glance and a second question about it is noise; a group of forty
#: is a paragraph, and the keystroke that accepts it should cost one more.
#: It is a size, deliberately not a score: nothing about the facts themselves
#: is measured here, and no size accepts anything on its own.
LARGE_ACCEPT_CONFIRMATION_SIZE = 20
CONFIRM_LARGE_ACCEPT_PROMPT = (
    "Accept all {count} facts still proposed in the group above? [y/N]: "
)
#: The one answer that confirms, matched exactly. Everything else — including
#: `Y`, `yes` and an empty line — decides nothing.
CONFIRM_ANSWER = "y"
CONFIRMATION_DECLINED_NOTICE = "not confirmed; nothing was decided"

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
    "CONFIRMATION_DECLINED_NOTICE",
    "CONFIRM_ANSWER",
    "CONFIRM_LARGE_ACCEPT_PROMPT",
    "CORRECTION_PROMPT",
    "EMAIL_PROMPT",
    "EMPTY_CORRECTION_NOTICE",
    "INVALID_ACTION_NOTICE",
    "LARGE_ACCEPT_CONFIRMATION_SIZE",
    "NON_SQLITE_BACKEND_ERROR",
    "NOTHING_TO_REVIEW_NOTICE",
    "NOT_DISPLAYED_NOTICE",
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
    #: True only for a `--grouped` run, and the only thing that adds the three
    #: counters below. The default run reports exactly what it always did.
    grouped: bool = False
    groups_shown: int = 0
    groups_completed: int = 0
    batch_actions: int = 0

    def as_dict(self) -> dict[str, object]:
        reported: dict[str, object] = {
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
        if self.grouped:
            reported["groups_shown"] = self.groups_shown
            reported["groups_completed"] = self.groups_completed
            reported["batch_actions"] = self.batch_actions
        return reported


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


@dataclass
class _GroupItem:
    """One line of a printed group: its index, its fact, and where it stands.

    `status` is this run's own view of the fact, updated only when a decision
    this session took succeeded. It is what makes an index stale rather than
    reusable: an item that is no longer `PROPOSED` is not offered again, and no
    command can move it a second time.
    """

    entry: ImportedCandidate
    index: int
    status: FactStatus = FactStatus.PROPOSED

    @property
    def is_pending(self) -> bool:
        return self.status is FactStatus.PROPOSED


@dataclass
class _GroupedSession(_Session):
    """The same five decisions, asked about a printed group instead of a fact.

    It holds one extra thing the one-by-one review does not need: the identity
    of the group currently on screen. `_shown` is the ids of the facts the last
    printed group offered, and no acceptance or rejection runs unless every fact
    it names is in there. That is the whole safety of `a`: it cannot reach a
    group nobody printed, a fact of another group, or a fact printed before a
    decision changed it, because every decision reprints the group it changed.
    """

    #: The still-proposed fact ids of the group last printed, or None between
    #: two groups. Never a value, never a candidate: ids only.
    _shown: tuple[int, ...] | None = None

    def review(self, groups: Sequence[ReviewGroup]) -> None:
        """Walk the groups in order. Stops at the first `q`, and stays safe."""
        for position, group in enumerate(groups, start=1):
            if not self._review_group(group, position, len(groups)):
                return

    def _review_group(
        self, group: ReviewGroup, position: int, total: int
    ) -> bool:
        """Ask about one group until it is answered, skipped or quit.

        False means quit. The group is printed once on entry and again after
        every decision that changed it, so what the next command acts on is
        always what the reader has just been shown.
        """
        items = [
            _GroupItem(entry=entry, index=index)
            for index, entry in enumerate(group.entries, start=1)
        ]
        self._display(group, items, position, total)
        # Counted once per group, not once per printing: a group reprinted
        # after a decision is the same group, still being answered.
        self.outcome.groups_shown += 1
        try:
            while True:
                pending = [item for item in items if item.is_pending]
                if not pending:
                    self.outcome.groups_completed += 1
                    return True
                typed = self._prompt(GROUP_ACTION_PROMPT)
                if typed is None:
                    self.outcome.quit_requested = True
                    return False
                command = parse_group_command(typed)
                if command is None:
                    self._explain_refusal(typed)
                    continue
                if command.action is GroupAction.HELP:
                    print(GROUP_HELP_TEXT)
                    continue
                if command.action is GroupAction.QUIT:
                    self.outcome.quit_requested = True
                    return False
                if command.action is GroupAction.SKIP:
                    # Skipping decides nothing: every fact left stays PROPOSED,
                    # and the next run will offer this group again.
                    self.outcome.skipped += len(pending)
                    return True
                if not self._act(command, group, items, pending, position, total):
                    continue
        finally:
            self._shown = None

    def _act(
        self,
        command: GroupCommand,
        group: ReviewGroup,
        items: list[_GroupItem],
        pending: list[_GroupItem],
        position: int,
        total: int,
    ) -> bool:
        """Run one accept, reject or correct. False means nothing was decided."""
        chosen = self._chosen_items(command, pending)
        if chosen is None:
            return False
        if command.action is GroupAction.CORRECT:
            changed = self._correct_one(chosen[0])
        else:
            changed = self._decide_batch(command.action, chosen)
        if changed and any(item.is_pending for item in items):
            # The group changed under the reader, so it is printed again before
            # anything else can be typed about it.
            self._display(group, items, position, total)
        return changed

    def _chosen_items(
        self, command: GroupCommand, pending: list[_GroupItem]
    ) -> list[_GroupItem] | None:
        """The items a command names, or None when it names something else.

        An index out of range, and an index whose fact is already decided, are
        refused the same way and refuse the whole command with them: deciding
        the part of a selection that happens to be valid would act on something
        nobody typed.
        """
        if not command.indices:
            if not pending:
                print(NOTHING_PENDING_NOTICE)
                return None
            return list(pending)
        by_index = {item.index: item for item in pending}
        if not selection_is_reviewable(command.indices, tuple(by_index)):
            print(UNKNOWN_INDEX_NOTICE)
            return None
        return [by_index[index] for index in command.indices]

    def _decide_batch(
        self, action: GroupAction, chosen: list[_GroupItem]
    ) -> bool:
        """Accept or reject the chosen facts, all of them or none of them."""
        fact_ids = tuple(item.entry.fact.id for item in chosen)
        if self._shown is None or not set(fact_ids).issubset(set(self._shown)):
            # Unreachable through the loop above, and asserted anyway: this is
            # the invariant that `a` decides only what was printed.
            print(NOT_DISPLAYED_NOTICE)
            return False
        target = (
            FactStatus.ACCEPTED
            if action is GroupAction.ACCEPT
            else FactStatus.REJECTED
        )
        if target is FactStatus.ACCEPTED and not self._confirm_large_accept(
            len(fact_ids)
        ):
            return False
        decide_profile_facts(self.connection, self.profile_id, fact_ids, target)
        for item in chosen:
            item.status = target
        if target is FactStatus.ACCEPTED:
            self.outcome.accepted += len(fact_ids)
        else:
            self.outcome.rejected += len(fact_ids)
        self.outcome.batch_actions += 1
        return True

    def _confirm_large_accept(self, count: int) -> bool:
        """Ask twice before a big acceptance. Anything but `y` decides nothing.

        Only the size of the acceptance decides whether the question is asked,
        so the same group always asks it or always does not. The default is no:
        a closed stdin, an empty line, a `Y` and a `yes` all leave every fact
        exactly as it was.
        """
        if count <= LARGE_ACCEPT_CONFIRMATION_SIZE:
            return True
        answer = self._prompt(CONFIRM_LARGE_ACCEPT_PROMPT.format(count=count))
        if answer == CONFIRM_ANSWER:
            return True
        print(CONFIRMATION_DECLINED_NOTICE)
        return False

    def _correct_one(self, item: _GroupItem) -> bool:
        """Correct exactly one fact. Corrections are never batched."""
        if not self._correct(item.entry.fact.id):
            return False
        item.status = FactStatus.CORRECTED
        return True

    def _explain_refusal(self, typed: str) -> None:
        """Say why nothing happened. The two near-misses get their own words."""
        if typed == GroupAction.REJECT.value:
            print(REJECT_NEEDS_INDICES_NOTICE)
            return
        if typed == GroupAction.CORRECT.value or typed.startswith(
            GroupAction.CORRECT.value + " "
        ):
            print(CORRECT_NEEDS_ONE_INDEX_NOTICE)
            return
        print(INVALID_COMMAND_NOTICE)

    def _display(
        self,
        group: ReviewGroup,
        items: list[_GroupItem],
        position: int,
        total: int,
    ) -> None:
        """Print the group, and record which facts it just offered."""
        pending = [item for item in items if item.is_pending]
        print("")
        print(f"GROUP {position}/{total} — {group.label}")
        noun = "fact" if len(items) == 1 else "facts"
        print(f"{len(pending)} of {len(items)} {noun} still PROPOSED")
        for item in items:
            print("")
            head = (
                f"[{item.index}] {item.entry.fact.fact_type} "
                f"(fact_id={item.entry.fact.id})"
            )
            if not item.is_pending:
                # Decided in this run: the marker is enough, and repeating the
                # value would invite a second answer to a settled question.
                print(f"{head} — {item.status.value} in this run")
                continue
            print(head)
            print(f"    value: {item.entry.fact.value}")
            if item.entry.fact.normalized_value is not None:
                print(f"    normalized: {item.entry.fact.normalized_value}")
            print(f"    {_describe_provenance(item.entry)}")
        self._shown = tuple(item.entry.fact.id for item in pending)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import one local CV PDF as proposed profile facts, then review "
            "them by hand, one at a time or one printed group at a time. "
            "Nothing is accepted automatically."
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
    parser.add_argument(
        "--grouped",
        action="store_true",
        help=(
            "Review by group — contacts, studies, skills — instead of fact by "
            "fact. A group is printed in full and then answered: `a` accepts "
            "the facts of THAT printed group that are still proposed, `a 1,3-5` "
            "or `r 2` answer some of them, `c 3` corrects one, `s` leaves the "
            "rest proposed. It accepts nothing you were not shown and nothing "
            "beyond the group on screen; there is no accept-all for the CV."
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
    connection,
    profile_id: int,
    result: CvImportResult,
    ask: Callable[[str], str],
    *,
    grouped: bool = False,
) -> ReviewOutcome:
    """Ask a human about what is still undecided, one fact or one group at a time.

    Both shapes start from the same list and honour the same rule: only the
    undecided ones. A fact already accepted, rejected or corrected for this same
    proof has been answered, and asking again would invite an answer that
    contradicts the one on record. Grouping changes how those facts are laid out
    on screen and how many of them one answer covers — never which of them are
    asked about, and never whether an answer was given.
    """
    session = (
        _GroupedSession(connection=connection, profile_id=profile_id, ask=ask)
        if grouped
        else _Session(connection=connection, profile_id=profile_id, ask=ask)
    )
    session.outcome.grouped = grouped
    session.outcome.candidates = result.candidate_count
    session.outcome.newly_proposed = result.newly_proposed
    session.outcome.already_imported = result.already_imported

    pending = result.pending_review
    if not pending:
        print(NOTHING_TO_REVIEW_NOTICE)
    elif grouped:
        print(SENSITIVE_OUTPUT_NOTICE)
        session.review(build_review_groups(pending))
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
            outcome = _review(
                connection, pair.profile_id, result, ask, grouped=args.grouped
            )
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
