"""The deterministic Web Push payload, built from the persisted event alone.

Phase 5.3B already decided what a movement means and wrote it down. This
module is a projection of that record onto the small object
``apps/web/public/sw.js`` knows how to render — ``title``, ``body``, ``url``,
``tag`` — and nothing else. It reads no table, recomputes no Portfolio,
Priority, Matching, or Eligibility, and writes no generated prose: every word
that is not a fixed label of this module comes verbatim from the stored
payload.

The click target is the internal path the policy already persisted. An
external URL is never a click target, so a payload whose target is not a
same-origin path is refused rather than quietly redirected: the stored event
is written by our own policy, so a target that is not internal means the
record is wrong and nothing should be sent from it.
"""

from __future__ import annotations

import json
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .policy import NOTIFICATION_TARGET_PATH, NotificationEventType

#: Identity of this projection. A future revision of the wording or the field
#: set is a new version, never a silent change to what a browser was shown.
DELIVERY_PAYLOAD_VERSION = "notification-delivery-payload-v1"

#: The fixed lead of each notification. Everything after it is stored data.
_LEADS = {
    NotificationEventType.NEW_ACTIONABLE_OPPORTUNITY: "New actionable opportunity",
    NotificationEventType.ATTENTION_ESCALATED: "Attention escalated",
}

MAX_TEXT_LENGTH = 200


class NotificationPayloadError(RuntimeError):
    """Raised when a stored event cannot be projected onto a push payload."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotificationPayloadError(f"stored event has no usable {field}")
    return " ".join(value.split())[:MAX_TEXT_LENGTH]


def _internal_path(value: Any) -> str:
    """Accept only a same-origin absolute path, and never anything else.

    ``//host/path`` is a protocol-relative URL, not a path, so a single
    leading slash is not enough on its own.
    """
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or len(value) > MAX_TEXT_LENGTH
        or any(character in value for character in ("\\", "\n", "\r", "\t", " "))
    ):
        raise NotificationPayloadError(
            "stored event does not carry an internal click target"
        )
    return value


def build_push_payload(payload_json: str) -> dict[str, Any]:
    """Project one persisted ``notification_events.payload_json`` for the browser."""
    if not isinstance(payload_json, str) or not payload_json.strip():
        raise NotificationPayloadError("stored event payload is empty")
    try:
        stored = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise NotificationPayloadError("stored event payload is not JSON") from error
    if not isinstance(stored, dict):
        raise NotificationPayloadError("stored event payload is not an object")
    try:
        event_type = NotificationEventType(stored["event_type"])
    except (KeyError, TypeError, ValueError) as error:
        raise NotificationPayloadError("stored event has no known type") from error
    opportunity = stored.get("opportunity")
    if not isinstance(opportunity, dict):
        raise NotificationPayloadError("stored event describes no opportunity")
    opportunity_id = opportunity.get("opportunity_id")
    if isinstance(opportunity_id, bool) or not isinstance(opportunity_id, int):
        raise NotificationPayloadError("stored event has no opportunity id")
    title = _text(opportunity.get("title"), "title")
    organization = _text(opportunity.get("organization"), "organization")
    # The target the policy persisted, not one derived here, and never the
    # opportunity's outward ``original_url``.
    target = _internal_path(opportunity.get("target_url", NOTIFICATION_TARGET_PATH))
    return {
        "event_type": event_type.value,
        "opportunity_id": opportunity_id,
        "title": title,
        "body": f"{_LEADS[event_type]} · {organization}",
        "url": target,
        # One notification per opportunity and movement: a redelivery to a
        # second device replaces nothing, but a repeat of the same movement
        # collapses instead of stacking.
        "tag": f"opportunity-radar:{event_type.value}:{opportunity_id}",
    }


def encode_push_payload(payload_json: str) -> bytes:
    """Return the exact bytes a push service will carry, canonically ordered."""
    return canonical_json(build_push_payload(payload_json)).encode("utf-8")
