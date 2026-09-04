"""Application tracking: the candidatures a person actually decided to pursue.

This is a domain of its own, and deliberately not a corner of the collector.
Collection answers "what exists out there"; this answers "what did I do about
it" — and the second is never derived from the first. No opportunity becomes a
candidature because it was detected, matched, prioritised or included in the
Portfolio. A row exists here because a person saved an opportunity, started
preparing it, or recorded that they had applied.

Phase 6.1 is the foundation: the two tables, the events that make the history
readable, and the operations a person can perform by hand. The adapted CV
(6.2), the motivation letter (6.3), the application's answers (6.4) and the
follow-ups (6.5) each bring their own storage when they arrive; nothing here
is a placeholder for them.
"""

from services.applications.models import (
    ACTION_STATUS,
    MAX_NEXT_ACTION_LENGTH,
    MAX_NOTES_LENGTH,
    POST_SUBMISSION_STATUSES,
    PRE_SUBMISSION_STATUSES,
    PUBLIC_STATUSES,
    REQUIRED_MIGRATION_VERSION,
    TERMINAL_STATUSES,
    ActorType,
    ApplicationAction,
    ApplicationConflictError,
    ApplicationError,
    ApplicationEventRecord,
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
from services.applications.read_model import (
    ApplicationDetailView,
    ApplicationOpportunityView,
    ApplicationReadError,
    ApplicationView,
    read_application_detail,
    read_applications,
    read_opportunity_views,
)
from services.applications.service import (
    ApplicationOutcome,
    apply_action,
    change_status,
    normalize_followup_date,
    normalize_text,
    update_tracking,
)

__all__ = [
    "ACTION_STATUS",
    "MAX_NEXT_ACTION_LENGTH",
    "MAX_NOTES_LENGTH",
    "POST_SUBMISSION_STATUSES",
    "PRE_SUBMISSION_STATUSES",
    "PUBLIC_STATUSES",
    "REQUIRED_MIGRATION_VERSION",
    "TERMINAL_STATUSES",
    "ActorType",
    "ApplicationAction",
    "ApplicationConflictError",
    "ApplicationDetailView",
    "ApplicationError",
    "ApplicationEventRecord",
    "ApplicationEventType",
    "ApplicationNotFoundError",
    "ApplicationOpportunityView",
    "ApplicationOutcome",
    "ApplicationReadError",
    "ApplicationRecord",
    "ApplicationRequestError",
    "ApplicationStatus",
    "ApplicationView",
    "OpportunityNotFoundError",
    "TrackingUpdate",
    "action_advances",
    "apply_action",
    "change_status",
    "is_public_status",
    "normalize_followup_date",
    "normalize_text",
    "read_application_detail",
    "read_applications",
    "read_opportunity_views",
    "submission_guard",
    "update_tracking",
]
