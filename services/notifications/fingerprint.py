"""Canonical, identity-independent notification event payloads and identities.

A notification event has to survive being recomputed: the same movement seen
again must produce the same bytes and the same fingerprint. So neither the
payload nor the fingerprint contains a database id of its own, a timestamp, or
any generated sentence — only the semantic content Phase 5.3C will render, and
the Portfolio run fingerprints the movement happened between.
"""

from __future__ import annotations

import hashlib
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .policy import (
    NOTIFICATION_POLICY_VERSION,
    NotificationTransition,
    OpportunityNotificationMetadata,
    PortfolioPosition,
)


def _position_payload(position: PortfolioPosition | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {
        "disposition": position.disposition.value,
        "bucket": None if position.bucket is None else position.bucket.value,
        "priority_category": (
            None
            if position.priority_category is None
            else position.priority_category.value
        ),
    }


def canonical_notification_event_payload(
    transition: NotificationTransition,
    metadata: OpportunityNotificationMetadata,
    *,
    policy_version: str = NOTIFICATION_POLICY_VERSION,
) -> dict[str, Any]:
    """Return exactly what a renderer needs, and nothing a renderer must invent."""
    if metadata.opportunity_id != transition.opportunity_id:
        raise ValueError("notification metadata belongs to another opportunity")
    current = transition.current
    return {
        "policy_version": policy_version,
        "event_type": transition.event_type.value,
        "opportunity": {
            "opportunity_id": transition.opportunity_id,
            "title": metadata.title,
            "organization": metadata.organization,
            "original_url": metadata.original_url,
            "target_url": metadata.target_url,
        },
        "portfolio": {
            "priority_category": (
                None
                if current.priority_category is None
                else current.priority_category.value
            ),
            "bucket": None if current.bucket is None else current.bucket.value,
            "reason_codes": list(current.reason_codes),
        },
        "transition": {
            "previous": _position_payload(transition.previous),
            "current": _position_payload(current),
        },
    }


def notification_event_fingerprint(
    *,
    profile_id: int,
    previous_portfolio_run_fingerprint: str,
    portfolio_run_fingerprint: str,
    payload: dict[str, Any],
) -> str:
    """Identify one movement by its meaning and its Portfolio provenance."""
    identity = {
        "policy_version": payload["policy_version"],
        "profile_id": profile_id,
        "event_type": payload["event_type"],
        "opportunity_id": payload["opportunity"]["opportunity_id"],
        "previous_portfolio_run_fingerprint": previous_portfolio_run_fingerprint,
        "portfolio_run_fingerprint": portfolio_run_fingerprint,
        "payload": payload,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
