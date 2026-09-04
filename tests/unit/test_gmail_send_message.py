"""The frozen digest becomes a MIME message, and stays exactly what it was.

Phase 5.4B is transport. Every test here is a statement that the bytes a
mailbox receives are the bytes Phase 5.4A froze: same subject, same plain text,
same HTML, same order, no footer, no prefix, no re-render, and no loss on the
way through base64url.
"""

import base64
from email import message_from_bytes, policy

import pytest

from services.gmail_digest import (
    build_digest_message,
    build_raw_digest_message,
    encode_raw_message,
)
from services.gmail_digest.models import GmailDigestError

RECIPIENT = "digest-recipient@example.invalid"
SUBJECT = "Opportunity Radar — 3 opportunities today"
TEXT = "TARGET\n\n1. Data Engineer — Café Ltd (Zürich)\n   https://example.invalid/1\n"
HTML = '<html><body><h1>TARGET</h1><a href="https://example.invalid/1">Data Engineer</a></body></html>'


def built(**overrides):
    fields = {
        "recipient": RECIPIENT,
        "subject": SUBJECT,
        "body_text": TEXT,
        "body_html": HTML,
    }
    fields.update(overrides)
    return build_digest_message(**fields)


def parsed(message):
    """Reparse the wire form, which is what Gmail and a mail client see."""
    return message_from_bytes(
        base64.urlsafe_b64decode(encode_raw_message(message)), policy=policy.default
    )


def bodies(message):
    return {
        part.get_content_type(): part.get_content()
        for part in message.walk()
        if not part.is_multipart()
    }


def wire_form(body):
    """What MIME makes of one frozen body, and the whole of what it makes.

    A MIME body is a sequence of lines: they are separated by CRLF and the last
    one is terminated. Nothing else about the string may change, so this is the
    exact expectation every body assertion below compares against — a stricter
    one than "close enough", because any other difference is a digest that no
    longer says what the frozen row says.
    """
    canonical = body.replace("\r\n", "\n")
    return canonical if canonical.endswith("\n") else canonical + "\n"


def decoded(message, content_type):
    """One decoded part, with MIME's line endings put back to newlines."""
    return bodies(message)[content_type].replace("\r\n", "\n")


def test_the_message_is_multipart_alternative_text_then_html():
    message = parsed(built())

    assert message.get_content_type() == "multipart/alternative"
    assert [
        part.get_content_type() for part in message.walk() if not part.is_multipart()
    ] == ["text/plain", "text/html"]


def test_the_frozen_subject_survives_exactly():
    assert parsed(built())["Subject"] == SUBJECT


def test_the_frozen_bodies_survive_exactly():
    message = parsed(built())

    assert decoded(message, "text/plain") == wire_form(TEXT) == TEXT
    assert decoded(message, "text/html") == wire_form(HTML)


def test_unicode_in_a_frozen_body_is_preserved_character_for_character():
    text = "Ingénieur données — 東京 — 50 000 €\n"
    html = "<p>Ingénieur données — 東京 — 50 000 €</p>"
    subject = "Radar — Größe · 東京"

    message = parsed(built(subject=subject, body_text=text, body_html=html))

    assert message["Subject"] == subject
    assert decoded(message, "text/plain") == wire_form(text) == text
    assert decoded(message, "text/html") == wire_form(html)


def test_the_configured_recipient_is_the_only_addressee():
    message = parsed(built())

    assert message["To"] == RECIPIENT
    assert message["Cc"] is None and message["Bcc"] is None
    # From is Gmail's to fill in with the authorized account.
    assert message["From"] is None


def test_the_raw_payload_is_valid_base64url_the_gmail_api_accepts():
    raw = build_raw_digest_message(
        recipient=RECIPIENT, subject=SUBJECT, body_text=TEXT, body_html=HTML
    )

    assert isinstance(raw, str)
    assert not set(raw) - set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
    )
    wire = base64.urlsafe_b64decode(raw)
    assert b"\r\n" in wire
    message = message_from_bytes(wire, policy=policy.default)
    assert decoded(message, "text/plain") == TEXT


def test_nothing_is_added_to_or_removed_from_the_frozen_html():
    marker = '<img src="https://tracker.invalid/pixel.gif">'
    frozen = f"<p>a</p>{marker}"

    assert decoded(parsed(built(body_html=frozen)), "text/html") == wire_form(frozen)


def test_no_character_of_a_frozen_body_is_substituted_on_the_way_through():
    """The only difference is line separation; every character is untouched."""
    frozen = "a\tb  c — ü \\ <&> \"'  https://x.invalid/p?q=1&r=2#f\nlast line"

    assert decoded(parsed(built(body_text=frozen)), "text/plain") == wire_form(frozen)


def test_a_subject_carrying_a_line_break_is_refused_as_header_injection():
    with pytest.raises(GmailDigestError):
        built(subject="Radar\r\nBcc: attacker@example.invalid")


def test_a_recipient_carrying_a_line_break_is_refused():
    with pytest.raises(GmailDigestError):
        built(recipient="a@b.invalid\r\nBcc: attacker@example.invalid")


@pytest.mark.parametrize("field", ["subject", "body_text", "body_html", "recipient"])
def test_an_empty_frozen_field_is_refused_rather_than_sent(field):
    with pytest.raises(GmailDigestError):
        built(**{field: "   "})


def test_encoding_refuses_something_that_is_not_a_message():
    with pytest.raises(GmailDigestError):
        encode_raw_message("From: nobody\r\n\r\nhello")
