"""What a candidature does when a person acts on it.

Three things happen to an application in Phase 6.1: it comes into existence
because someone intended it to, it moves to a new status because someone said
so, and it collects the notes and dates that person keeps about it. Each one
is a single SQLite transaction that writes the new state and the event
recording it together, or writes neither.

Two properties matter more than anything else here, and both are tested:

*Idempotence.* Clicking "Sauvegarder" three times leaves one candidature and
one creation event. Repeating an action a candidature has already passed
changes nothing and appends nothing — no artificial event, no touched
timestamp, no demotion. The same is true of a tracking update that submits the
values already stored.

*Honest history.* An event is written when, and only when, something really
changed. `application_events` is append-only in the database itself, so an
event written by mistake cannot be taken back; the discipline of writing one
only for a real change is what keeps the timeline worth reading.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from services.applications import repository
from services.applications.models import (
    ACTION_STATUS,
    MAX_NEXT_ACTION_LENGTH,
    MAX_NOTES_LENGTH,
    TERMINAL_STATUSES,
    ActorType,
    ApplicationAction,
    ApplicationConflictError,
    ApplicationEventType,
    ApplicationNotFoundError,
    ApplicationRecord,
    ApplicationRequestError,
    ApplicationStatus,
    OpportunityNotFoundError,
    TrackingUpdate,
    action_advances,
    is_public_status,
    submission_guard,
)
from services.collector.logging_config import get_logger


LOGGER = get_logger("services.applications.service")

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class ApplicationOutcome:
    """One operation's result: the candidature, and what actually happened.

    ``changed`` is false for every no-op, and a caller can rely on it meaning
    "nothing was written and no event exists for this call".
    """

    application: ApplicationRecord
    created: bool
    changed: bool


def normalize_text(value: object, field: str, maximum: int) -> str | None:
    """Read one free-text tracking field, or None to clear it.

    Surrounding whitespace is trimmed and a value that is nothing but
    whitespace becomes None: a cleared textarea and an explicit null mean the
    same thing to a person, so they mean the same thing here. The rejection
    never quotes the value — these are private notes, and an error message is
    a place they must not appear.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApplicationRequestError(f"{field} must be a string or null")
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > maximum:
        raise ApplicationRequestError(
            f"{field} must be at most {maximum} characters"
        )
    return trimmed


def normalize_followup_date(value: object) -> str | None:
    """Read a follow-up date as YYYY-MM-DD, or None to clear it.

    The pattern is checked before the calendar, because ``date.fromisoformat``
    also accepts spellings this schema does not store. In Phase 6.1 this date
    is a note to self and nothing more: no job reads it, no reminder is sent,
    and no follow-up is recommended from it — that is Phase 6.5.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApplicationRequestError("followup_date must be a string or null")
    trimmed = value.strip()
    if not trimmed:
        return None
    if not _ISO_DATE.fullmatch(trimmed):
        raise ApplicationRequestError("followup_date must be formatted YYYY-MM-DD")
    try:
        date.fromisoformat(trimmed)
    except ValueError as error:
        raise ApplicationRequestError("followup_date is not a real date") from error
    return trimmed


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ApplicationRequestError(f"{field} must be a positive integer")
    return value


def _begin(connection: sqlite3.Connection) -> None:
    """Take the write lock, then refuse a schema that cannot hold the result.

    The order matters: the check runs inside the transaction so that nothing
    can migrate out from under it, and a failed check gives the transaction
    back rather than leaving the caller holding a lock it never opened.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        repository.preflight(connection)
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _require(
    connection: sqlite3.Connection, *, profile_id: int, application_id: int
) -> ApplicationRecord:
    existing = repository.read_application(
        connection, profile_id=profile_id, application_id=application_id
    )
    if existing is None:
        # Another profile's candidature and a nonexistent one are the same
        # answer on purpose: this surface never confirms that a row it does
        # not own exists.
        raise ApplicationNotFoundError("application does not exist")
    return existing


def _reread(
    connection: sqlite3.Connection, *, profile_id: int, application_id: int
) -> ApplicationRecord:
    updated = repository.read_application(
        connection, profile_id=profile_id, application_id=application_id
    )
    if updated is None:
        raise ApplicationConflictError("application disappeared while being updated")
    return updated


def apply_action(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    opportunity_id: int,
    action: ApplicationAction,
) -> ApplicationOutcome:
    """Record the intention that makes an opportunity a candidature.

    This is the only door through which an application is born, and it opens
    only for a person: nothing in collection, matching, priority or Portfolio
    calls it. The opportunity must already exist in this database — a
    candidature for an opportunity nobody ever observed would be a fiction,
    so the request is refused rather than the opportunity invented.

    Repeating an intention the candidature has already passed does nothing at
    all, and a candidature that has ended — rejected, withdrawn, or turned into
    an offer — refuses the intention outright rather than being quietly walked
    back to "saved".
    """
    _positive_int(profile_id, "profile_id")
    _positive_int(opportunity_id, "opportunity_id")
    target = ACTION_STATUS[action]

    _begin(connection)
    try:
        if not repository.opportunity_exists(connection, opportunity_id):
            raise OpportunityNotFoundError("opportunity does not exist")
        existing = repository.read_application_for_opportunity(
            connection, profile_id=profile_id, opportunity_id=opportunity_id
        )
        now = repository.current_timestamp(connection)
        if existing is None:
            if not repository.profile_exists(connection, profile_id):
                raise ApplicationConflictError("profile does not exist")
            submitted_at = (
                now if target is ApplicationStatus.SUBMITTED else None
            )
            application_id = repository.insert_application(
                connection,
                profile_id=profile_id,
                opportunity_id=opportunity_id,
                status=target,
                submitted_at=submitted_at,
                now=now,
            )
            repository.insert_event(
                connection,
                application_id=application_id,
                event_type=ApplicationEventType.APPLICATION_CREATED,
                from_status=None,
                to_status=target,
                actor_type=ActorType.USER,
                occurred_at=now,
            )
            record = _reread(
                connection, profile_id=profile_id, application_id=application_id
            )
            outcome = ApplicationOutcome(record, created=True, changed=True)
        elif existing.status in TERMINAL_STATUSES:
            raise ApplicationConflictError(
                "application has already concluded and cannot be reopened by this action"
            )
        elif not action_advances(action, existing.status):
            # Already there, or already further along. Nothing is written, so
            # nothing is appended: the history stays a record of real changes.
            outcome = ApplicationOutcome(existing, created=False, changed=False)
        else:
            outcome = _move(
                connection, existing=existing, target=target, now=now, created=False
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Application action applied.",
        extra={
            "event": "application_action_applied",
            "profile_id": profile_id,
            "opportunity_id": opportunity_id,
            "action": action.value,
            "application_id": outcome.application.id,
            "application_status": outcome.application.status.value,
            "application_created": outcome.created,
            "application_changed": outcome.changed,
        },
    )
    return outcome


def _move(
    connection: sqlite3.Connection,
    *,
    existing: ApplicationRecord,
    target: ApplicationStatus,
    now: str,
    created: bool,
) -> ApplicationOutcome:
    """Write a real status change and the event that records it, together.

    ``submitted_at`` is set here exactly once, by the movement that first makes
    the candidature submitted, and is carried forward unchanged by every
    movement after it. Confirmation, an assessment, an interview and an offer
    all happen to something that was submitted at one particular instant, and
    that instant is not theirs to restate.
    """
    submitted_at = existing.submitted_at
    if submitted_at is None and target is ApplicationStatus.SUBMITTED:
        submitted_at = now
    repository.update_status(
        connection,
        application_id=existing.id,
        status=target,
        submitted_at=submitted_at,
        now=now,
    )
    repository.insert_event(
        connection,
        application_id=existing.id,
        event_type=ApplicationEventType.STATUS_CHANGED,
        from_status=existing.status,
        to_status=target,
        actor_type=ActorType.USER,
        occurred_at=now,
    )
    record = _reread(
        connection, profile_id=existing.profile_id, application_id=existing.id
    )
    return ApplicationOutcome(record, created=created, changed=True)


def change_status(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    application_id: int,
    status: ApplicationStatus,
) -> ApplicationOutcome:
    """Move a candidature by hand, within the two rules Phase 6.1 believes in.

    The rules are the submission boundary and nothing else: a candidature that
    was never sent cannot be at a stage that only exists after sending, and one
    that was sent cannot go back to being drafted. Everything a real search
    actually does — submitted straight to interview, interview to offer,
    anything at all to rejected — is allowed, because refusing it would only
    make the tracker disagree with the user's inbox.

    READY is refused here: it belongs to the model, but nothing in 6.1 can
    honestly decide that an application is ready. DISCOVERED is refused too —
    it describes an opportunity nobody has acted on, and a tracked candidature
    can never truthfully go back to being one.
    """
    _positive_int(profile_id, "profile_id")
    _positive_int(application_id, "application_id")
    if not is_public_status(status):
        raise ApplicationRequestError(
            f"{status.value} cannot be set in this phase"
        )

    _begin(connection)
    try:
        existing = _require(
            connection, profile_id=profile_id, application_id=application_id
        )
        if existing.status is status:
            outcome = ApplicationOutcome(existing, created=False, changed=False)
        elif not submission_guard(
            status, submitted=existing.submitted_at is not None
        ):
            raise ApplicationConflictError(
                f"{existing.status.value} cannot become {status.value}"
            )
        else:
            now = repository.current_timestamp(connection)
            outcome = _move(
                connection, existing=existing, target=status, now=now, created=False
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    LOGGER.info(
        "Application status change handled.",
        extra={
            "event": "application_status_change_handled",
            "profile_id": profile_id,
            "application_id": application_id,
            "application_status": outcome.application.status.value,
            "application_changed": outcome.changed,
        },
    )
    return outcome


def update_tracking(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    application_id: int,
    update: TrackingUpdate,
) -> ApplicationOutcome:
    """Write the notes, next action and follow-up date a person keeps by hand.

    This route cannot move a candidature. It never touches the status, the
    submission instant or the ownership of the row, and it deliberately leaves
    ``last_status_change`` alone: writing a note is not the candidature moving.
    Submitting the values already stored changes nothing and appends no event.
    """
    _positive_int(profile_id, "profile_id")
    _positive_int(application_id, "application_id")
    if update.empty:
        raise ApplicationRequestError("no tracking field was provided")

    _begin(connection)
    try:
        existing = _require(
            connection, profile_id=profile_id, application_id=application_id
        )
        next_action = (
            existing.next_action if update.next_action is None else update.next_action[0]
        )
        followup_date = (
            existing.followup_date
            if update.followup_date is None
            else update.followup_date[0]
        )
        notes = existing.notes if update.notes is None else update.notes[0]
        unchanged = (
            next_action == existing.next_action
            and followup_date == existing.followup_date
            and notes == existing.notes
        )
        if unchanged:
            outcome = ApplicationOutcome(existing, created=False, changed=False)
        else:
            now = repository.current_timestamp(connection)
            repository.update_tracking(
                connection,
                application_id=application_id,
                next_action=next_action,
                followup_date=followup_date,
                notes=notes,
                now=now,
            )
            repository.insert_event(
                connection,
                application_id=application_id,
                event_type=ApplicationEventType.TRACKING_UPDATED,
                from_status=None,
                to_status=None,
                actor_type=ActorType.USER,
                occurred_at=now,
            )
            record = _reread(
                connection, profile_id=profile_id, application_id=application_id
            )
            outcome = ApplicationOutcome(record, created=False, changed=True)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    # Which fields moved is not reported either: "the notes changed" is
    # already more than the log needs, and their content is never written.
    LOGGER.info(
        "Application tracking update handled.",
        extra={
            "event": "application_tracking_update_handled",
            "profile_id": profile_id,
            "application_id": application_id,
            "application_changed": outcome.changed,
        },
    )
    return outcome
