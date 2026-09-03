"""Notification delivery foundations for Opportunity Radar AI.

Phase 5.3A owns exactly one concern: the browser push subscriptions a user
opts into. No notification policy, outbox, or delivery lives here yet.
"""

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

__all__ = [
    "AUTH_BYTE_LENGTH",
    "MAX_ENDPOINT_LENGTH",
    "MAX_KEY_LENGTH",
    "P256DH_BYTE_LENGTH",
    "P256DH_UNCOMPRESSED_PREFIX",
    "PushSubscriptionError",
    "PushSubscriptionOwnershipError",
    "PushSubscriptionStatus",
    "SubscribeResult",
    "UnsubscribeResult",
    "decode_base64url",
    "subscribe_push_subscription",
    "unsubscribe_push_subscription",
    "valid_auth",
    "valid_p256dh",
]
