"""The network half of delivery: one message to Gmail, one classified answer.

This module is deliberately the only place in Phase 5.4B that talks to Google,
and it holds no database handle at all. A drain hands it a claimed digest and
gets back a :class:`~services.gmail_digest.delivery_persistence.GmailSendOutcome`
— sent with an id, or failed with a bounded code and a category. Persistence
then decides what that means for the row. The split is what lets every
lifecycle test run against real SQLite with a fake transport, with no Google
package installed and no socket in reach.

Only ``users().messages().send`` is ever called. There is no list, no get, no
label, no history, no watch, and no draft: the authorization this phase holds
would refuse them anyway, and the code does not know how to ask.

Classification is deterministic and the same every time, because the retry
policy is only as good as its inputs:

* no HTTP status at all — DNS, refused connection, TLS, timeout — is
  ``RETRYABLE``: nothing was sent, so nothing can be duplicated by trying
  again;
* ``429`` and every ``5xx`` are ``RETRYABLE``;
* ``403`` is read properly rather than guessed at — Google says whether it
  means "slow down" or "you may not do this", and those are opposite
  operational facts, so the structured reason decides;
* ``401`` and every other ``4xx`` are ``PERMANENT``, because the credential
  was already loaded, refreshed and scope-checked before this digest was
  claimed: authorization failing *now* is a revoked or downgraded grant, and
  no number of retries fixes it.

Nothing from a Google response body is ever persisted or logged. What survives
classification is a code from the closed vocabulary below and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from services.collector.logging_config import get_logger

from .delivery_persistence import (
    MISSING_MESSAGE_ID_CODE,
    GmailDeliveryErrorCategory,
    GmailSendOutcome,
    authorization_failure,
    delivered,
    transport_failure,
)
from .message import build_raw_digest_message
from .oauth import (
    GmailOAuthConfiguration,
    build_gmail_service,
    load_send_credentials,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .delivery_persistence import ClaimedDigest

LOGGER = get_logger("services.collector.gmail_digest.sender")

#: The Gmail user this phase sends as: the account that granted the
#: authorization, named the way the API names it.
GMAIL_SEND_USER_ID = "me"

#: A refusal to parse an implausibly large error body as if it were a small
#: JSON document. Nothing from it is stored either way.
MAX_ERROR_BODY_BYTES = 64 * 1024

#: 403 reasons that mean "not now": a quota, a rate limit, a transient backend
#: refusal. Every one of them is worth the backoff and another attempt.
RETRYABLE_403_REASONS: dict[str, str] = {
    "ratelimitexceeded": "RATE_LIMIT_EXCEEDED",
    "userratelimitexceeded": "USER_RATE_LIMIT_EXCEEDED",
    "dailylimitexceeded": "DAILY_LIMIT_EXCEEDED",
    "quotaexceeded": "QUOTA_EXCEEDED",
    "resource_exhausted": "RESOURCE_EXHAUSTED",
    "backenderror": "BACKEND_ERROR",
    "sharingratelimitexceeded": "SHARING_RATE_LIMIT_EXCEEDED",
    "servinglimitexceeded": "SERVING_LIMIT_EXCEEDED",
    "concurrentlimitexceeded": "CONCURRENT_LIMIT_EXCEEDED",
    "unavailable": "UNAVAILABLE",
}

#: 403 reasons that mean "not ever, as configured": a scope that was not
#: granted, an API that is not enabled, a policy that forbids this. Retrying
#: these only spends the attempt budget on an answer that will not change.
PERMANENT_403_REASONS: dict[str, str] = {
    "forbidden": "FORBIDDEN",
    "permissiondenied": "PERMISSION_DENIED",
    "permission_denied": "PERMISSION_DENIED",
    "insufficientpermissions": "INSUFFICIENT_PERMISSIONS",
    "insufficientscope": "INSUFFICIENT_SCOPE",
    "insufficient_scope": "INSUFFICIENT_SCOPE",
    "accessnotconfigured": "ACCESS_NOT_CONFIGURED",
    "domainpolicy": "DOMAIN_POLICY",
    "failedprecondition": "FAILED_PRECONDITION",
    "failed_precondition": "FAILED_PRECONDITION",
    "autherror": "AUTH_ERROR",
    "unauthorized": "UNAUTHORIZED",
}

#: Exception module prefixes that mean "the request never arrived". Google's
#: HTTP stack raises from these, and none of their messages is quotable.
_TRANSPORT_MODULE_PREFIXES = (
    "httplib2",
    "http",
    "socket",
    "ssl",
    "urllib3",
    "requests",
)

#: Exception types that mean the authorization itself failed at send time.
_AUTHORIZATION_TYPE_NAMES = frozenset({"RefreshError", "DefaultCredentialsError"})


class GmailMessageTransport(Protocol):
    """Anything that can hand Gmail one raw message and report its answer."""

    def __call__(self, raw_message: str) -> Mapping[str, Any]:
        """Send the message, or raise. The answer names the created message."""


class GoogleApiGmailTransport:
    """The real transport: ``users().messages().send`` and nothing else.

    The Gmail service is built and validated before this exists, so
    constructing a transport never authenticates and never opens a socket.
    """

    def __init__(self, service: Any, *, user_id: str = GMAIL_SEND_USER_ID) -> None:
        self._service = service
        self._user_id = user_id

    def __call__(self, raw_message: str) -> Mapping[str, Any]:
        return (
            self._service.users()
            .messages()
            .send(userId=self._user_id, body={"raw": raw_message})
            .execute()
        )

    def __repr__(self) -> str:
        return "GoogleApiGmailTransport(service=<gmail>, scope=send-only)"

    __str__ = __repr__


def _http_status(error: BaseException) -> int | None:
    """Read the HTTP status out of a Google API error, however it carries it."""
    response = getattr(error, "resp", None)
    for candidate in (
        getattr(response, "status", None),
        getattr(error, "status_code", None),
    ):
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.strip().isdigit():
            return int(candidate.strip())
    return None


def _reason_tokens(value: Any, found: list[str]) -> None:
    if isinstance(value, Mapping):
        for key in ("reason", "status"):
            token = value.get(key)
            if isinstance(token, str) and token.strip():
                found.append(token.strip().lower())
        for nested in value.values():
            _reason_tokens(nested, found)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reason_tokens(nested, found)


def error_reasons(error: BaseException) -> tuple[str, ...]:
    """Every structured reason Google named, lowercased and deduplicated.

    Both shapes are read: ``error_details``, which the client library parses
    for us, and the raw JSON body, which is where the classic
    ``errors[].reason`` lives. The body is parsed *only* to pick reason and
    status tokens out of it; nothing else from it is kept, and nothing from it
    is ever stored.
    """
    found: list[str] = []
    _reason_tokens(getattr(error, "error_details", None), found)
    content = getattr(error, "content", None)
    if isinstance(content, (bytes, bytearray)) and len(content) <= MAX_ERROR_BODY_BYTES:
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            content = None
    if isinstance(content, str) and len(content) <= MAX_ERROR_BODY_BYTES:
        try:
            _reason_tokens(json.loads(content), found)
        except (json.JSONDecodeError, ValueError):
            pass
    ordered: list[str] = []
    for token in found:
        if token not in ordered:
            ordered.append(token)
    return tuple(ordered)


def classify_gmail_status(
    status_code: int, reasons: tuple[str, ...] = ()
) -> GmailSendOutcome:
    """Turn one Gmail HTTP status, and what Google said about it, into a verdict."""
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise TypeError("status_code must be an integer")
    if 200 <= status_code < 300:
        raise ValueError("a successful status is not a failure to classify")
    if status_code == 403:
        for reason in reasons:
            if reason in RETRYABLE_403_REASONS:
                return GmailSendOutcome(
                    False,
                    category=GmailDeliveryErrorCategory.RETRYABLE,
                    code=f"HTTP_403_{RETRYABLE_403_REASONS[reason]}",
                )
        for reason in reasons:
            if reason in PERMANENT_403_REASONS:
                return GmailSendOutcome(
                    False,
                    category=GmailDeliveryErrorCategory.PERMANENT,
                    code=f"HTTP_403_{PERMANENT_403_REASONS[reason]}",
                )
        # An unrecognised 403 is a refusal until Google says it was a limit.
        return GmailSendOutcome(
            False, category=GmailDeliveryErrorCategory.PERMANENT, code="HTTP_403"
        )
    retryable = status_code in (408, 429) or 500 <= status_code < 600
    return GmailSendOutcome(
        False,
        category=(
            GmailDeliveryErrorCategory.RETRYABLE
            if retryable
            else GmailDeliveryErrorCategory.PERMANENT
        ),
        code=f"HTTP_{status_code}",
    )


def _is_authorization_error(error: BaseException) -> bool:
    kind = type(error)
    module = getattr(kind, "__module__", "") or ""
    return kind.__name__ in _AUTHORIZATION_TYPE_NAMES or module.startswith(
        "google.auth"
    )


def _is_transport_error(error: BaseException) -> bool:
    if isinstance(error, (OSError, TimeoutError)):
        return True
    module = (getattr(type(error), "__module__", "") or "").split(".")[0]
    return module in _TRANSPORT_MODULE_PREFIXES


def classify_send_exception(error: BaseException) -> GmailSendOutcome | None:
    """Classify a failed send, or return None when the failure is not Gmail's.

    Returning None matters: a ``TypeError`` from our own code is a bug, not a
    delivery outcome, and recording it as one would burn a user's retry budget
    on a defect. Such an exception propagates instead, the claim is handed back
    unused, and the drain stops.
    """
    status = _http_status(error)
    if status is not None:
        if 200 <= status < 300:
            return None
        return classify_gmail_status(status, error_reasons(error))
    if _is_authorization_error(error):
        return authorization_failure()
    if _is_transport_error(error):
        return transport_failure()
    return None


@dataclass(frozen=True)
class _Recipient:
    """The configured address, held so it cannot be printed by accident."""

    address: str

    def __repr__(self) -> str:
        return "<recipient redacted>"

    __str__ = __repr__


class GmailDigestSender:
    """Sends one frozen digest, and reports only a delivery outcome.

    The recipient address lives here and nowhere else in the delivery path:
    persistence knows a fingerprint, the queue stores a fingerprint, and the
    plaintext mailbox is runtime configuration that never reaches either. The
    transport is injected, so a test supplies one that never opens a socket
    while production supplies :class:`GoogleApiGmailTransport`.
    """

    def __init__(self, transport: GmailMessageTransport, *, recipient: str) -> None:
        if not callable(transport):
            raise TypeError("transport must be callable")
        if not isinstance(recipient, str) or not recipient.strip():
            raise ValueError("recipient must be a non-empty string")
        self._transport = transport
        self._recipient = _Recipient(recipient)

    def __repr__(self) -> str:
        return "GmailDigestSender(recipient=<redacted>, scope=send-only)"

    __str__ = __repr__

    def __call__(self, claim: ClaimedDigest) -> GmailSendOutcome:
        """Send exactly what was frozen, and say what Gmail made of it."""
        raw_message = build_raw_digest_message(
            recipient=self._recipient.address,
            subject=claim.subject,
            body_text=claim.body_text,
            body_html=claim.body_html,
        )
        try:
            response = self._transport(raw_message)
        except BaseException as error:
            outcome = classify_send_exception(error)
            if outcome is None:
                raise
            LOGGER.warning(
                "Gmail refused a digest.",
                extra={
                    "event": "gmail_digest_send_failed",
                    "outbox_id": claim.outbox_id,
                    "delivery_error_code": outcome.code,
                    "delivery_error_category": (
                        None if outcome.category is None else outcome.category.value
                    ),
                },
            )
            return outcome
        message_id = response.get("id") if isinstance(response, Mapping) else None
        if not isinstance(message_id, str) or not message_id.strip():
            # 0022 requires a SENT digest to name the message Gmail created, so
            # a nameless acceptance cannot be recorded as sent. Retrying a
            # response shape that will recur would only mail the user twice.
            return GmailSendOutcome(
                False,
                category=GmailDeliveryErrorCategory.PERMANENT,
                code=MISSING_MESSAGE_ID_CODE,
            )
        return delivered(message_id)


def build_gmail_digest_sender(
    recipient: str,
    *,
    configuration: GmailOAuthConfiguration | None = None,
    dependencies: Any | None = None,
) -> GmailDigestSender:
    """Load the authorization, validate the client, and return a live sender.

    This is what a delivery command calls *before* it opens the database for
    writing. Every credential failure — no token, corrupt token, unrefreshable
    token, revoked grant, over-broad scope, unusable client — happens here, so
    an OAuth problem can never move a PENDING digest to IN_FLIGHT.
    """
    resolved = (
        GmailOAuthConfiguration.local() if configuration is None else configuration
    )
    credentials = load_send_credentials(resolved, dependencies=dependencies)
    service = build_gmail_service(credentials, dependencies=dependencies)
    return GmailDigestSender(GoogleApiGmailTransport(service), recipient=recipient)
