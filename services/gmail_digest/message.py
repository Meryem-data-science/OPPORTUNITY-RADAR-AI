"""The frozen digest, wrapped in an envelope and nothing more.

Phase 5.4B is transport. The subject and the two bodies in a
``gmail_digest_outbox`` row were decided, rendered and fingerprinted by Phase
5.4A against an audited Portfolio snapshot, and this module's entire
responsibility is to put those exact bytes into a MIME message a mail client
will render. It does not re-render, re-order, re-escape, truncate, prefix,
append a footer, or add a tracking pixel; a digest that reaches a mailbox says
precisely what the frozen row says, or the phase has failed at its one job.

The shape is ``multipart/alternative``: the plain-text body first, the HTML
body second, which is the order that makes a client prefer the HTML and lets a
text-only reader see the same digest in full. ``To`` is the configured
recipient — runtime configuration, checked against the row's fingerprint
before anything is claimed — and ``From`` is deliberately absent, because
Gmail fills it in with the authorized account and a header we invented could
only disagree with it.

The one transformation, named exactly
-------------------------------------
A MIME body is a sequence of lines: the wire form separates them with CRLF and
terminates the last one. So a body whose frozen text uses bare newlines is
transmitted with CRLF, and a body not ending in a newline gains a single
terminator. That is the encoding every standards-compliant mail message uses,
and it is the *whole* of the difference between the frozen string and what a
client decodes — no character is substituted, no whitespace is stripped, no
markup is rewritten, escaped or sanitized, nothing is prefixed or appended, and
non-ASCII text survives intact because the transfer encoding is chosen to carry
it rather than to flatten it.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.policy import SMTP

from .models import GmailDigestError

#: Headers this module refuses to accept a value for. Every one of them is
#: either Gmail's to set or a way to smuggle a second recipient into a message
#: whose recipient was already checked against a fingerprint.
_REFUSED_IN_SUBJECT = ("\r", "\n")


def _frozen_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailDigestError(f"frozen digest {name} is missing")
    return value


def build_digest_message(
    *,
    recipient: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> EmailMessage:
    """Wrap one frozen digest in a ``multipart/alternative`` message.

    Both bodies go in verbatim. ``EmailMessage`` chooses a transfer encoding
    per part, so a digest containing any non-ASCII character — an accented
    organization name, a currency symbol, an em dash — survives intact rather
    than being mangled or stripped; what a client decodes is the frozen string
    itself, character for character.
    """
    address = _frozen_text(recipient, "recipient")
    if any(character in address for character in _REFUSED_IN_SUBJECT):
        raise GmailDigestError("recipient must not contain a line break")
    heading = _frozen_text(subject, "subject")
    if any(character in heading for character in _REFUSED_IN_SUBJECT):
        # A subject carrying CR or LF is header injection, whatever wrote it.
        raise GmailDigestError("frozen digest subject must not contain a line break")
    text = _frozen_text(body_text, "text body")
    html = _frozen_text(body_html, "HTML body")

    message = EmailMessage()
    message["To"] = address
    message["Subject"] = heading
    message.set_content(text, subtype="plain", charset="utf-8")
    message.add_alternative(html, subtype="html", charset="utf-8")
    return message


def encode_raw_message(message: EmailMessage) -> str:
    """Render one message into the base64url form the Gmail API expects.

    Serialization uses the SMTP policy, so the wire form carries CRLF line
    endings like any other mail; the result is URL-safe base64 of those bytes,
    which is exactly what ``users().messages().send`` reads from ``raw``.
    """
    if not isinstance(message, EmailMessage):
        raise GmailDigestError("message must be an EmailMessage")
    return base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode("ascii")


def build_raw_digest_message(
    *,
    recipient: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> str:
    """Build and encode one frozen digest in a single step."""
    return encode_raw_message(
        build_digest_message(
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
        )
    )
