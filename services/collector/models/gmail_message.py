"""Transient models produced by the read-only Gmail intake."""

from dataclasses import dataclass


@dataclass(frozen=True)
class GmailMessageCandidate:
    """A normalized Gmail message that is never persisted by this module."""

    message_id: str
    thread_id: str | None
    sender: str | None
    subject: str | None
    received_at: str | None
    snippet: str | None
    body_text: str | None
    body_html: str | None
