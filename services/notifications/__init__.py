"""Notification foundations for Opportunity Radar AI.

Phase 5.3A owns the browser push subscriptions a user opts into. Phase 5.3B
adds the notification policy: which movements of the audited Portfolio deserve
a notification, the append-only events those movements produce, and the outbox
a later phase will drain. Nothing here delivers anything.
"""

from services.notifications.fingerprint import (
    canonical_notification_event_payload,
    notification_event_fingerprint,
)
from services.notifications.opportunity_metadata import (
    NotificationMetadataError,
    read_opportunity_metadata,
)
from services.notifications.persistence import (
    NotificationEventRecord,
    NotificationPersistenceError,
    NotificationStoreResult,
    store_notification_events,
)
from services.notifications.policy import (
    ESCALATIONS,
    NOTIFICATION_POLICY_VERSION,
    NOTIFICATION_TARGET_PATH,
    NotificationEventType,
    NotificationTransition,
    OpportunityNotificationMetadata,
    PortfolioPosition,
    evaluate_transition,
    evaluate_transitions,
)
from services.notifications.push_subscriptions import (
    AUTH_BYTE_LENGTH,
    MAX_ENDPOINT_LENGTH,
    MAX_KEY_LENGTH,
    P256DH_BYTE_LENGTH,
    P256DH_UNCOMPRESSED_PREFIX,
    PushSubscriptionError,
    PushSubscriptionOwnershipError,
    PushSubscriptionStatus,
    SubscribeResult,
    UnsubscribeResult,
    decode_base64url,
    subscribe_push_subscription,
    unsubscribe_push_subscription,
    valid_auth,
    valid_p256dh,
)
from services.notifications.sync import (
    NotificationSyncError,
    NotificationSyncResult,
    NotificationSyncStatus,
    sync_notification_policy,
)

__all__ = [
    "AUTH_BYTE_LENGTH",
    "ESCALATIONS",
    "MAX_ENDPOINT_LENGTH",
    "MAX_KEY_LENGTH",
    "NOTIFICATION_POLICY_VERSION",
    "NOTIFICATION_TARGET_PATH",
    "NotificationEventRecord",
    "NotificationEventType",
    "NotificationMetadataError",
    "NotificationPersistenceError",
    "NotificationStoreResult",
    "NotificationSyncError",
    "NotificationSyncResult",
    "NotificationSyncStatus",
    "NotificationTransition",
    "OpportunityNotificationMetadata",
    "P256DH_BYTE_LENGTH",
    "P256DH_UNCOMPRESSED_PREFIX",
    "PortfolioPosition",
    "PushSubscriptionError",
    "PushSubscriptionOwnershipError",
    "PushSubscriptionStatus",
    "SubscribeResult",
    "UnsubscribeResult",
    "canonical_notification_event_payload",
    "decode_base64url",
    "evaluate_transition",
    "evaluate_transitions",
    "notification_event_fingerprint",
    "read_opportunity_metadata",
    "store_notification_events",
    "subscribe_push_subscription",
    "sync_notification_policy",
    "unsubscribe_push_subscription",
    "valid_auth",
    "valid_p256dh",
]
