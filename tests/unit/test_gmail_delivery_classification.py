"""Deterministic classification of Gmail outcomes, and the retry policy.

Every exception here is synthetic. Nothing in this module imports a Google
package, opens a socket, or needs a credential: classification is a pure
function of an HTTP status and the structured reasons Google names beside it,
which is exactly why it can be pinned down this thoroughly.
"""

import json
from types import SimpleNamespace

import pytest

from services.gmail_digest import (
    AUTHORIZATION_ERROR_CODE,
    MAX_DELIVERY_ATTEMPTS,
    MISSING_MESSAGE_ID_CODE,
    RETRY_BACKOFF_SECONDS,
    TRANSPORT_ERROR_CODE,
    ClaimedDigest,
    GmailDeliveryError,
    GmailDeliveryErrorCategory,
    GmailDigestSender,
    GmailSendOutcome,
    bounded_error_code,
    classify_gmail_status,
    classify_send_exception,
    error_reasons,
    exhausted_error_code,
    retry_delay_seconds,
)

RETRYABLE = GmailDeliveryErrorCategory.RETRYABLE
PERMANENT = GmailDeliveryErrorCategory.PERMANENT


class HttpError(Exception):
    """The shape ``googleapiclient.errors.HttpError`` presents to a caller."""

    def __init__(self, status, body=None, *, error_details=None):
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status, reason="")
        self.status_code = status
        self.content = b"" if body is None else json.dumps(body).encode("utf-8")
        if error_details is not None:
            self.error_details = error_details


class RefreshError(Exception):
    """Stands in for ``google.auth.exceptions.RefreshError``."""

    __module__ = "google.auth.exceptions"


def google_error(status, *, reason=None, status_text=None, message="detail"):
    error = {"code": status, "message": message}
    if reason is not None:
        error["errors"] = [{"message": message, "domain": "global", "reason": reason}]
    if status_text is not None:
        error["status"] = status_text
    return HttpError(status, {"error": error})


def claim(**overrides):
    fields = dict(
        outbox_id=1,
        profile_id=1,
        digest_date="2026-09-03",
        digest_version="gmail-digest-v1",
        recipient_fingerprint="a" * 64,
        content_fingerprint="b" * 64,
        item_count=2,
        attempt_count=0,
        claim_token="0" * 32,
        subject="Radar",
        body_text="text",
        body_html="<p>html</p>",
    )
    fields.update(overrides)
    return ClaimedDigest(**fields)


def sender(transport):
    return GmailDigestSender(transport, recipient="person@example.invalid")


# --------------------------------------------------------------------------
# Retryable
# --------------------------------------------------------------------------


def test_a_request_that_never_produced_a_status_is_retryable():
    outcome = classify_send_exception(ConnectionResetError("connection reset"))

    assert outcome == GmailSendOutcome(
        False, category=RETRYABLE, code=TRANSPORT_ERROR_CODE
    )


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        OSError("network unreachable"),
        ConnectionRefusedError("refused"),
    ],
)
def test_every_transport_failure_is_retryable(error):
    assert classify_send_exception(error).category is RETRYABLE


def test_rate_limiting_is_retryable():
    assert classify_gmail_status(429) == GmailSendOutcome(
        False, category=RETRYABLE, code="HTTP_429"
    )


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_every_server_error_is_retryable(status):
    outcome = classify_send_exception(HttpError(status))

    assert outcome.category is RETRYABLE
    assert outcome.code == f"HTTP_{status}"


def test_a_request_timeout_is_retryable():
    assert classify_send_exception(HttpError(408)).category is RETRYABLE


# --------------------------------------------------------------------------
# Permanent
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 411, 413, 415, 422])
def test_a_non_retryable_4xx_is_permanent(status):
    outcome = classify_send_exception(HttpError(status))

    assert outcome.category is PERMANENT
    assert outcome.code == f"HTTP_{status}"


def test_authorization_that_fails_after_validation_is_permanent():
    # The credential was loaded, refreshed and scope-checked before the digest
    # was claimed, so a 401 now is a revoked grant, not a blip.
    assert classify_send_exception(HttpError(401)).category is PERMANENT


def test_a_refresh_failure_during_a_send_is_permanent():
    outcome = classify_send_exception(RefreshError("invalid_grant"))

    assert outcome == GmailSendOutcome(
        False, category=PERMANENT, code=AUTHORIZATION_ERROR_CODE
    )


# --------------------------------------------------------------------------
# 403 is read, not guessed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,code",
    [
        ("rateLimitExceeded", "HTTP_403_RATE_LIMIT_EXCEEDED"),
        ("userRateLimitExceeded", "HTTP_403_USER_RATE_LIMIT_EXCEEDED"),
        ("dailyLimitExceeded", "HTTP_403_DAILY_LIMIT_EXCEEDED"),
        ("quotaExceeded", "HTTP_403_QUOTA_EXCEEDED"),
        ("backendError", "HTTP_403_BACKEND_ERROR"),
    ],
)
def test_a_403_that_means_slow_down_is_retryable(reason, code):
    outcome = classify_send_exception(google_error(403, reason=reason))

    assert outcome == GmailSendOutcome(False, category=RETRYABLE, code=code)


@pytest.mark.parametrize(
    "reason,code",
    [
        ("forbidden", "HTTP_403_FORBIDDEN"),
        ("insufficientPermissions", "HTTP_403_INSUFFICIENT_PERMISSIONS"),
        ("accessNotConfigured", "HTTP_403_ACCESS_NOT_CONFIGURED"),
        ("domainPolicy", "HTTP_403_DOMAIN_POLICY"),
    ],
)
def test_a_403_that_means_you_may_not_do_this_is_permanent(reason, code):
    outcome = classify_send_exception(google_error(403, reason=reason))

    assert outcome == GmailSendOutcome(False, category=PERMANENT, code=code)


def test_an_insufficient_scope_403_is_permanent():
    outcome = classify_send_exception(
        google_error(
            403, reason="insufficientPermissions", status_text="PERMISSION_DENIED"
        )
    )

    assert outcome.category is PERMANENT


def test_a_resource_exhausted_403_is_retryable_even_by_its_status_alone():
    outcome = classify_send_exception(
        google_error(403, status_text="RESOURCE_EXHAUSTED")
    )

    assert outcome.category is RETRYABLE


def test_an_unrecognised_403_is_refused_rather_than_retried_forever():
    outcome = classify_send_exception(google_error(403, reason="somethingNew"))

    assert outcome == GmailSendOutcome(False, category=PERMANENT, code="HTTP_403")


def test_the_structured_reason_is_read_from_error_details_too():
    error = HttpError(403, error_details=[{"reason": "rateLimitExceeded"}])

    assert classify_send_exception(error).category is RETRYABLE


def test_reasons_are_read_from_a_body_without_choking_on_a_malformed_one():
    error = HttpError(403)
    error.content = b"<html>503 Service Unavailable</html>"

    assert error_reasons(error) == ()
    assert classify_send_exception(error).category is PERMANENT


def test_an_enormous_error_body_is_not_parsed_at_all():
    error = HttpError(403)
    error.content = (
        b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}' + b"x" * 100_000
    )

    assert error_reasons(error) == ()


# --------------------------------------------------------------------------
# Nothing quotable survives classification
# --------------------------------------------------------------------------


def test_no_part_of_a_google_response_body_reaches_the_persisted_code():
    error = google_error(
        400,
        reason="invalidArgument",
        message="Recipient person@example.invalid rejected: 'secret detail'",
    )

    outcome = classify_send_exception(error)

    assert outcome.code == "HTTP_400"
    assert "example.invalid" not in outcome.code and "secret" not in outcome.code


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("HTTP_429", "HTTP_429"),
        ("  http_500  ", "HTTP_500"),
        ("weird code: person@example.invalid", "WEIRD_CODE_PERSON_EXAMPLE_INVALID"),
        ("x" * 200, "X" * 64),
        ("!!!", "UNKNOWN"),
    ],
)
def test_every_persisted_code_is_bounded_and_uppercase(raw, expected):
    assert bounded_error_code(raw) == expected


def test_a_terminal_exhaustion_code_keeps_the_cause_and_stays_bounded():
    code = exhausted_error_code("HTTP_503")

    assert code == "RETRY_LIMIT_EXHAUSTED_HTTP_503"
    assert len(exhausted_error_code("Z" * 200)) <= 64


# --------------------------------------------------------------------------
# A defect is not a delivery outcome
# --------------------------------------------------------------------------


def test_an_error_that_is_not_gmails_is_not_classified_at_all():
    assert classify_send_exception(TypeError("a bug in our own code")) is None
    assert classify_send_exception(ValueError("also a bug")) is None


def test_a_defect_in_the_transport_propagates_instead_of_costing_an_attempt():
    def broken(raw_message):
        raise TypeError("a bug")

    with pytest.raises(TypeError):
        sender(broken)(claim())


# --------------------------------------------------------------------------
# The sender's own contract
# --------------------------------------------------------------------------


def test_a_successful_send_requires_a_gmail_message_id():
    outcome = sender(lambda raw_message: {"id": "18f0a1b2c3d4e5f6"})(claim())

    assert outcome == GmailSendOutcome(True, message_id="18f0a1b2c3d4e5f6")


@pytest.mark.parametrize("response", [{}, {"id": ""}, {"id": None}, "accepted", None])
def test_an_acceptance_without_an_id_is_never_recorded_as_sent(response):
    outcome = sender(lambda raw_message: response)(claim())

    assert outcome == GmailSendOutcome(
        False, category=PERMANENT, code=MISSING_MESSAGE_ID_CODE
    )


def test_the_sender_hands_the_transport_a_base64url_message_and_nothing_else():
    seen = []
    sender(lambda raw_message: seen.append(raw_message) or {"id": "x"})(claim())

    assert len(seen) == 1
    assert not set(seen[0]) - set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
    )


def test_an_impossible_outcome_cannot_be_constructed():
    with pytest.raises(GmailDeliveryError):
        GmailSendOutcome(True)
    with pytest.raises(GmailDeliveryError):
        GmailSendOutcome(True, message_id="x", code="HTTP_500")
    with pytest.raises(GmailDeliveryError):
        GmailSendOutcome(False, code="HTTP_500")
    with pytest.raises(GmailDeliveryError):
        GmailSendOutcome(False, category=RETRYABLE)
    with pytest.raises(GmailDeliveryError):
        GmailSendOutcome(False, message_id="x", category=RETRYABLE, code="HTTP_500")


# --------------------------------------------------------------------------
# The backoff schedule
# --------------------------------------------------------------------------


def test_the_backoff_is_the_web_push_schedule_exactly():
    assert RETRY_BACKOFF_SECONDS == (60, 300, 900, 3600)
    assert MAX_DELIVERY_ATTEMPTS == 5


@pytest.mark.parametrize(
    "attempt,delay", [(1, 60), (2, 300), (3, 900), (4, 3600), (5, None), (6, None)]
)
def test_the_wait_after_each_attempt_is_deterministic(attempt, delay):
    assert retry_delay_seconds(attempt) == delay


# --------------------------------------------------------------------------
# Nothing sensitive renders
# --------------------------------------------------------------------------


def test_a_claimed_digest_never_renders_its_message():
    rendered = f"{claim(subject='Secret subject')!r} {claim()!s}"

    assert "Secret subject" not in rendered
    assert (
        "<p>html</p>" not in rendered and "text" not in rendered.split("digest_date")[0]
    )
    assert "redacted" in rendered


def test_a_sender_never_renders_its_recipient():
    rendered = f"{sender(lambda raw: {'id': 'x'})!r}"

    assert "person@example.invalid" not in rendered
    assert "redacted" in rendered
