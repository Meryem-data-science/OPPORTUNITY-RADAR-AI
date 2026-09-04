"""The vocabulary of a tracked candidature, and the rules that vocabulary obeys.

A candidature is not an opportunity. The radar can observe a thousand
opportunities without a single application existing: an application appears
only when a person decides it should. This module holds that boundary as
types — the statuses a candidature can be in, the three intentions that can
create one, the events its history records — and the two small rules Phase 6.1
enforces on top of them.

Those rules are deliberately not a hiring state machine. The specification of
this project does not define an exhaustive transition graph, and inventing one
would make the tracker lie about reality: an interview can follow a submission
with nothing in between, an offer can arrive without an assessment, and a
rejection can arrive at any point. What is real, and what is enforced here, is
that a candidature which has been sent cannot become one that has not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


#: Migration 0023 creates both tables, the append-only triggers, and the
#: constraint tying an event to the state it describes. Every surface that
#: writes a candidature depends on all three, so a database that stops short of
#: it is refused rather than written to.
REQUIRED_MIGRATION_VERSION = "0023"

MAX_NOTES_LENGTH = 4000
MAX_NEXT_ACTION_LENGTH = 500


class ApplicationStatus(str, Enum):
    """The official status vocabulary, in the order a candidature travels it."""

    DISCOVERED = "DISCOVERED"
    SAVED = "SAVED"
    PREPARING = "PREPARING"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    ASSESSMENT = "ASSESSMENT"
    INTERVIEW = "INTERVIEW"
    REJECTED = "REJECTED"
    OFFER = "OFFER"
    WITHDRAWN = "WITHDRAWN"


class ApplicationAction(str, Enum):
    """The three intentions that can bring a candidature into existence."""

    SAVE = "SAVE"
    PREPARE = "PREPARE"
    MARK_SUBMITTED = "MARK_SUBMITTED"


class ApplicationEventType(str, Enum):
    """What the history of a candidature can record in Phase 6.1."""

    APPLICATION_CREATED = "APPLICATION_CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    TRACKING_UPDATED = "TRACKING_UPDATED"


class ActorType(str, Enum):
    """Who caused an event. Phase 6.1 only ever has a person."""

    USER = "USER"
    SYSTEM = "SYSTEM"


#: What each intention means as a status.
ACTION_STATUS: dict[ApplicationAction, ApplicationStatus] = {
    ApplicationAction.SAVE: ApplicationStatus.SAVED,
    ApplicationAction.PREPARE: ApplicationStatus.PREPARING,
    ApplicationAction.MARK_SUBMITTED: ApplicationStatus.SUBMITTED,
}

#: Statuses a candidature holds before it has been sent anywhere.
PRE_SUBMISSION_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.DISCOVERED,
        ApplicationStatus.SAVED,
        ApplicationStatus.PREPARING,
        ApplicationStatus.READY,
    }
)

#: Statuses that can only be reached through a real submission.
POST_SUBMISSION_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.ASSESSMENT,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
    }
)

#: Statuses that end a candidature. Nothing after them is an advance, so the
#: create-actions refuse them rather than quietly walking a finished
#: candidature backwards.
TERMINAL_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
        ApplicationStatus.WITHDRAWN,
    }
)

#: The statuses a person may set by hand in Phase 6.1.
#:
#: DISCOVERED is missing because it describes an opportunity nobody has decided
#: anything about; using it on a tracked candidature would be a demotion out of
#: the domain. READY is missing because 6.1 cannot honestly compute readiness —
#: an adapted CV (6.2), a letter (6.3) and the application's answers (6.4) are
#: what make an application ready, and none of them exists yet. Both remain in
#: the database vocabulary so the model does not have to be rewritten later.
PUBLIC_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.SAVED,
        ApplicationStatus.PREPARING,
        ApplicationStatus.SUBMITTED,
        ApplicationStatus.CONFIRMED,
        ApplicationStatus.ASSESSMENT,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
        ApplicationStatus.WITHDRAWN,
    }
)

#: How far along the *intent* ladder each status already is. It exists for one
#: purpose: to decide whether an action asks for something the candidature has
#: already passed, in which case the action is a no-op rather than a demotion.
#: It is not a transition graph — nothing consults it to decide what may follow
#: what — and WITHDRAWN is deliberately absent because abandoning a candidature
#: is not a rung on that ladder.
_ACTION_RANK: dict[ApplicationAction, int] = {
    ApplicationAction.SAVE: 1,
    ApplicationAction.PREPARE: 2,
    ApplicationAction.MARK_SUBMITTED: 3,
}

_STATUS_RANK: dict[ApplicationStatus, int] = {
    ApplicationStatus.DISCOVERED: 0,
    ApplicationStatus.SAVED: 1,
    ApplicationStatus.PREPARING: 2,
    ApplicationStatus.READY: 2,
    ApplicationStatus.SUBMITTED: 3,
    ApplicationStatus.CONFIRMED: 3,
    ApplicationStatus.ASSESSMENT: 3,
    ApplicationStatus.INTERVIEW: 3,
    ApplicationStatus.REJECTED: 3,
    ApplicationStatus.OFFER: 3,
}


class ApplicationError(RuntimeError):
    """Raised when a candidature cannot be tracked safely."""


class ApplicationRequestError(ApplicationError):
    """Raised when the caller asked for something this domain will not accept."""


class ApplicationNotFoundError(ApplicationError):
    """Raised when no such candidature exists for the current profile."""


class OpportunityNotFoundError(ApplicationError):
    """Raised when the opportunity a candidature would track does not exist."""


class ApplicationConflictError(ApplicationError):
    """Raised when a request contradicts the candidature it would change."""


@dataclass(frozen=True)
class ApplicationRecord:
    """One candidature as it currently stands."""

    id: int
    profile_id: int
    opportunity_id: int
    status: ApplicationStatus
    submitted_at: str | None
    last_status_change: str
    next_action: str | None
    followup_date: str | None
    notes: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ApplicationEventRecord:
    """One thing that happened to a candidature. Never rewritten."""

    id: int
    application_id: int
    event_type: ApplicationEventType
    from_status: ApplicationStatus | None
    to_status: ApplicationStatus | None
    actor_type: ActorType
    occurred_at: str


@dataclass(frozen=True)
class TrackingUpdate:
    """The manual tracking fields a PATCH asked to set, and only those.

    A field the caller did not mention is absent, which is not the same as a
    field the caller set to ``None``: the first leaves the stored value alone,
    the second clears it. Keeping the two apart is the whole reason this is a
    value object rather than three optional arguments.
    """

    next_action: tuple[str | None] | None = None
    followup_date: tuple[str | None] | None = None
    notes: tuple[str | None] | None = None

    @property
    def empty(self) -> bool:
        return (
            self.next_action is None
            and self.followup_date is None
            and self.notes is None
        )


def is_public_status(status: ApplicationStatus) -> bool:
    """Whether a person may set this status by hand in Phase 6.1."""
    return status in PUBLIC_STATUSES


def action_advances(action: ApplicationAction, status: ApplicationStatus) -> bool:
    """Whether this intention asks for more than the candidature already is.

    "Sauvegarder" on something already being prepared, or already submitted,
    asks for nothing: it is answered by leaving the candidature exactly as it
    is. Only an intention that reaches further than the current status is a
    real change.
    """
    return _ACTION_RANK[action] > _STATUS_RANK.get(status, 0)


def submission_guard(
    status: ApplicationStatus, *, submitted: bool
) -> bool:
    """Whether ``status`` is coherent with a candidature's submission history.

    This is the whole guard, and it is deliberately the whole guard. A
    candidature that has never been sent cannot be at a stage that only exists
    after sending; one that has been sent cannot go back to being drafted.
    Everything else — SUBMITTED straight to INTERVIEW, INTERVIEW to OFFER,
    anything to REJECTED — is a real sequence that really happens, and refusing
    it would only make the tracker disagree with the user's inbox.
    """
    if status is ApplicationStatus.WITHDRAWN:
        return True
    if submitted:
        return status in POST_SUBMISSION_STATUSES
    return status in PRE_SUBMISSION_STATUSES or status is ApplicationStatus.SUBMITTED
