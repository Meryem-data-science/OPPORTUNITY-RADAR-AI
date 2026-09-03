"""Notification delivery foundations for Opportunity Radar AI.

Phase 5.3A owns exactly one concern: the browser push subscriptions a user
opts into. No notification policy, outbox, or delivery lives here yet.
"""

from services.notifications.push_subscriptions import (
    MAX_ENDPOINT_LENGTH,
    MAX_KEY_LENGTH,
    PushSubscriptionError,
    PushSubscriptionOwnershipError,
    PushSubscriptionStatus,
    SubscribeResult,
    UnsubscribeResult,
    subscribe_push_subscription,
    unsubscribe_push_subscription,
)

__all__ = [
    "MAX_ENDPOINT_LENGTH",
    "MAX_KEY_LENGTH",
    "PushSubscriptionError",
    "PushSubscriptionOwnershipError",
    "PushSubscriptionStatus",
    "SubscribeResult",
    "UnsubscribeResult",
    "subscribe_push_subscription",
    "unsubscribe_push_subscription",
]
