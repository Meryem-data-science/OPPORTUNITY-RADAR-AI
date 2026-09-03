"""Two identities: what a digest says, and who it is for.

The content fingerprint answers one question and only one: *would the reader
see anything different?* It is therefore built from the visible business
content and nothing else — no digest date, no row id, no Portfolio run id or
run fingerprint, no timestamp. Those are provenance: they are persisted beside
the digest and deliberately kept out of its identity, because a Portfolio run
that only changed an EXCLUDED opportunity produces a new run fingerprint and
must not produce a new email.

The recipient fingerprint answers the other: *is this frozen message for the
mailbox now configured?* It identifies a recipient without persisting one, so
a copy of the database discloses no address, and Phase 5.4B can still refuse
to send a digest that was frozen for somebody else.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .models import DIGEST_VERSION, DigestItem, GmailDigestError


def digest_item_payload(item: DigestItem) -> dict[str, Any]:
    """Return the logical fields of one item — everything a reader sees."""
    return {
        "opportunity_id": item.opportunity_id,
        "title": item.title,
        "organization": item.organization,
        "location": item.location,
        "url": item.url,
        "bucket": item.bucket.value,
        "priority_category": item.priority_category.value,
        "eligibility_status": item.eligibility_status.value,
    }


def canonical_digest_payload(
    items: Iterable[DigestItem], *, digest_version: str = DIGEST_VERSION
) -> dict[str, Any]:
    """Return the canonical logical content of one digest, in reading order.

    Order is part of the content: a digest whose items were re-ranked reads
    differently even when the same opportunities appear, so the list is kept in
    the order the renderer will use rather than re-sorted here.
    """
    return {
        "digest_version": digest_version,
        "items": [digest_item_payload(item) for item in items],
    }


def digest_content_fingerprint(
    items: Iterable[DigestItem], *, digest_version: str = DIGEST_VERSION
) -> str:
    """Identify one digest by what it says, and by nothing else."""
    payload = canonical_digest_payload(items, digest_version=digest_version)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_recipient(recipient: object) -> str:
    """Return the canonical form of one intended recipient identity.

    Normalization is deliberately conservative: Unicode NFKC, surrounding
    whitespace removed, and the whole address lowercased. Lowercasing the
    local part is not universally safe in SMTP, but it is safe for the mailbox
    this product sends to — a Gmail address, where the local part is already
    case-insensitive — and it keeps a fingerprint stable across the ways a
    human types their own address into a configuration file.
    """
    if not isinstance(recipient, str):
        raise GmailDigestError("recipient must be a string")
    normalized = unicodedata.normalize("NFKC", recipient).strip().lower()
    local, separator, domain = normalized.partition("@")
    if not separator or not local or not domain or "@" in domain:
        raise GmailDigestError("recipient must be a single email address")
    if any(character.isspace() for character in normalized):
        raise GmailDigestError("recipient must not contain whitespace")
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise GmailDigestError("recipient domain is not a valid mail domain")
    return normalized


def recipient_fingerprint(recipient: str) -> str:
    """Identify a mailbox without storing one.

    The value is domain-separated so a recipient fingerprint can never collide
    with, or be mistaken for, a content fingerprint over the same bytes.
    """
    identity = {"recipient": normalize_recipient(recipient)}
    return hashlib.sha256(
        b"gmail-digest-recipient-v1:" + canonical_json(identity).encode("utf-8")
    ).hexdigest()
