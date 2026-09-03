"""Pure Phase 5.3B notification policy over audited Portfolio positions.

The Portfolio snapshot is the single source of authority: nothing here
recomputes Eligibility, Matching, Priority, or Portfolio, and nothing here
ranks, scores, or writes generated text. The policy only compares the position
one opportunity held in two consecutive Portfolio runs and decides whether that
movement is worth one notification event.

Policy v1 recognises exactly two movements:

* ``NEW_ACTIONABLE_OPPORTUNITY`` — the opportunity became actionable, i.e. it
  was absent from the previous run or excluded from it, and is now included.
* ``ATTENTION_ESCALATED`` — an already actionable opportunity climbed the
  priority ladder along one of the transitions in :data:`ESCALATIONS`.

Everything else — an unchanged position, a downgrade, a bucket change, an
exclusion — is deliberately silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from services.portfolio import PortfolioBucket, PortfolioDisposition
from services.priority import PriorityCategory

#: Explicit identity of this policy. Persisted with every state row and every
#: event, and folded into every event fingerprint, so a future policy revision
#: can never be mistaken for this one.
NOTIFICATION_POLICY_VERSION = "notification-policy-v1"

#: Internal destination for a notified opportunity. ``/portfolio`` is the
#: existing web route that presents the persisted Portfolio snapshot; no
#: per-opportunity route exists, so no deeper link is invented here.
NOTIFICATION_TARGET_PATH = "/portfolio"

#: The only priority movements policy v1 treats as an escalation. A downgrade
#: is not the mirror of an escalation: it is simply silent.
ESCALATIONS: frozenset[tuple[PriorityCategory, PriorityCategory]] = frozenset(
    {
        (PriorityCategory.MEDIUM, PriorityCategory.HIGH),
        (PriorityCategory.MEDIUM, PriorityCategory.URGENT),
        (PriorityCategory.HIGH, PriorityCategory.URGENT),
    }
)


class NotificationEventType(StrEnum):
    NEW_ACTIONABLE_OPPORTUNITY = "NEW_ACTIONABLE_OPPORTUNITY"
    ATTENTION_ESCALATED = "ATTENTION_ESCALATED"


@dataclass(frozen=True)
class PortfolioPosition:
    """The position one opportunity holds inside a single Portfolio run."""

    opportunity_id: int
    disposition: PortfolioDisposition
    bucket: PortfolioBucket | None
    priority_category: PriorityCategory | None
    reason_codes: tuple[str, ...]

    @property
    def actionable(self) -> bool:
        """Whether this position is one the user can act on today."""
        return self.disposition is PortfolioDisposition.INCLUDED


@dataclass(frozen=True)
class NotificationTransition:
    """One opportunity movement the policy decided to announce."""

    opportunity_id: int
    event_type: NotificationEventType
    previous: PortfolioPosition | None
    current: PortfolioPosition


@dataclass(frozen=True)
class OpportunityNotificationMetadata:
    """Opportunity facts read from their existing authorities, never invented."""

    opportunity_id: int
    title: str
    organization: str
    original_url: str
    target_url: str


def evaluate_transition(
    previous: PortfolioPosition | None, current: PortfolioPosition
) -> NotificationEventType | None:
    """Return the event type this single movement earns, or None."""
    became_actionable = current.actionable and (
        previous is None or not previous.actionable
    )
    escalated = (
        current.actionable
        and previous is not None
        and previous.actionable
        and (previous.priority_category, current.priority_category) in ESCALATIONS
    )
    # An opportunity that only just became actionable is announced once, as a
    # new opportunity: a simultaneous escalation adds nothing the user needs.
    if became_actionable:
        return NotificationEventType.NEW_ACTIONABLE_OPPORTUNITY
    if escalated:
        return NotificationEventType.ATTENTION_ESCALATED
    return None


def evaluate_transitions(
    previous: Mapping[int, PortfolioPosition],
    current: Mapping[int, PortfolioPosition],
) -> tuple[NotificationTransition, ...]:
    """Return every announced movement between two runs, ordered by opportunity.

    Only opportunities present in the *current* run can produce an event: an
    opportunity that left the cohort has no current position to announce.
    """
    transitions: list[NotificationTransition] = []
    for opportunity_id in sorted(current):
        position = current[opportunity_id]
        before = previous.get(opportunity_id)
        event_type = evaluate_transition(before, position)
        if event_type is not None:
            transitions.append(
                NotificationTransition(opportunity_id, event_type, before, position)
            )
    return tuple(transitions)
