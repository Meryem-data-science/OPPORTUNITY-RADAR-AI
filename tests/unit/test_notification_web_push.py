"""Unit coverage for the Phase 5.3C1 Web Push sender, payload, and retries.

Nothing in this module opens a socket, and one test proves it: the transport
is a parameter everywhere, so the only code that could reach the network is
never constructed here.
"""

import base64
import json
import logging
import socket
import struct

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from services.notifications import (
    MAX_DELIVERY_ATTEMPTS,
    MAX_PAYLOAD_BYTES,
    RETRY_BACKOFF_SECONDS,
    VAPID_PRIVATE_KEY_VARIABLE,
    VAPID_PUBLIC_KEY_VARIABLE,
    VAPID_SUBJECT_VARIABLE,
    DeliveryErrorCategory,
    DeliveryOutcome,
    NotificationDeliveryError,
    NotificationPayloadError,
    PushTarget,
    WebPushConfigurationError,
    WebPushError,
    build_push_payload,
    build_vapid_configuration,
    build_web_push_request,
    classify_status_code,
    encode_push_payload,
    encrypt_push_message,
    load_vapid_configuration,
    retry_delay_seconds,
    transport_failure,
)
from services.notifications.web_push import RECORD_SIZE


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


SCALAR = bytes(range(1, 33))
PRIVATE_KEY = encode(SCALAR)
PUBLIC_KEY = encode(
    ec.derive_private_key(int.from_bytes(SCALAR, "big"), ec.SECP256R1())
    .public_key()
    .public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
)
SUBJECT = "mailto:ops@example.invalid"
ENDPOINT = "https://push.example.invalid/subscription/abc"

# RFC 8291, appendix A: the published aes128gcm Web Push example.
RFC8291 = {
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPj"
    "s7Vd8pZGH6SRpkNtoIAiw4",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "plaintext": b"When I grow up, I want to be a watermelon",
    "body": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZ"
    "IIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVB"
    "St2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
}

PAYLOAD = json.dumps(
    {
        "policy_version": "notification-policy-v1",
        "event_type": "NEW_ACTIONABLE_OPPORTUNITY",
        "opportunity": {
            "opportunity_id": 7,
            "title": "Senior Data Scientist",
            "organization": "Acme Research",
            "original_url": "https://acme.example.invalid/jobs/7",
            "target_url": "/portfolio",
        },
        "portfolio": {"priority_category": "HIGH", "bucket": "TARGET"},
        "transition": {"previous": None, "current": {"disposition": "INCLUDED"}},
    }
)


def configuration():
    return build_vapid_configuration(
        public_key=PUBLIC_KEY, private_key=PRIVATE_KEY, subject=SUBJECT
    )


def test_vapid_configuration_accepts_a_real_pair_and_both_subject_forms():
    for subject in (SUBJECT, "https://ops.example.invalid/contact"):
        assert (
            build_vapid_configuration(
                public_key=PUBLIC_KEY, private_key=PRIVATE_KEY, subject=subject
            ).subject
            == subject
        )


def test_vapid_configuration_refuses_every_incomplete_or_invalid_identity():
    other = encode(bytes(range(2, 34)))
    for keywords in (
        {"public_key": "", "private_key": PRIVATE_KEY, "subject": SUBJECT},
        {
            "public_key": " " + PUBLIC_KEY,
            "private_key": PRIVATE_KEY,
            "subject": SUBJECT,
        },
        {"public_key": PUBLIC_KEY[:-4], "private_key": PRIVATE_KEY, "subject": SUBJECT},
        {
            "public_key": encode(b"\x03" + SCALAR * 2),
            "private_key": PRIVATE_KEY,
            "subject": SUBJECT,
        },
        {"public_key": PUBLIC_KEY, "private_key": "", "subject": SUBJECT},
        {
            "public_key": PUBLIC_KEY,
            "private_key": "not base64url!!",
            "subject": SUBJECT,
        },
        {
            "public_key": PUBLIC_KEY,
            "private_key": encode(SCALAR[:16]),
            "subject": SUBJECT,
        },
        {"public_key": PUBLIC_KEY, "private_key": PRIVATE_KEY, "subject": ""},
        {
            "public_key": PUBLIC_KEY,
            "private_key": PRIVATE_KEY,
            "subject": "ops@example.invalid",
        },
        {
            "public_key": PUBLIC_KEY,
            "private_key": PRIVATE_KEY,
            "subject": "http://ops.example.invalid",
        },
        {"public_key": PUBLIC_KEY, "private_key": PRIVATE_KEY, "subject": "mailto:"},
        # A well-formed pair that is not a pair: caught before any network.
        {"public_key": PUBLIC_KEY, "private_key": other, "subject": SUBJECT},
    ):
        with pytest.raises(WebPushConfigurationError):
            build_vapid_configuration(**keywords)


def test_vapid_configuration_never_renders_or_repeats_the_private_key():
    built = configuration()
    for rendering in (repr(built), str(built), f"{built}"):
        assert PRIVATE_KEY not in rendering and encode(SCALAR) not in rendering
        assert "redacted" in rendering
    other = encode(bytes(range(2, 34)))
    with pytest.raises(WebPushConfigurationError) as error:
        build_vapid_configuration(
            public_key=PUBLIC_KEY, private_key=other, subject=SUBJECT
        )
    assert other not in str(error.value) and PRIVATE_KEY not in str(error.value)


def test_load_vapid_configuration_names_the_missing_variable_and_nothing_else(
    monkeypatch,
):
    complete = {
        VAPID_PUBLIC_KEY_VARIABLE: PUBLIC_KEY,
        VAPID_PRIVATE_KEY_VARIABLE: PRIVATE_KEY,
        VAPID_SUBJECT_VARIABLE: SUBJECT,
    }
    assert load_vapid_configuration(complete).public_key == PUBLIC_KEY

    for variable in complete:
        for blank in ("", "   "):
            partial = complete | {variable: blank}
            with pytest.raises(WebPushConfigurationError) as error:
                load_vapid_configuration(partial)
            message = str(error.value)
            assert variable in message
            assert PRIVATE_KEY not in message and PUBLIC_KEY not in message
    for variable in complete:
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(WebPushConfigurationError):
        load_vapid_configuration()


def test_encryption_reproduces_the_rfc_8291_published_example():
    """The one place correctness cannot be asserted from our own code alone."""
    key = ec.derive_private_key(
        int.from_bytes(decode(RFC8291["as_private"]), "big"), ec.SECP256R1()
    )
    body = encrypt_push_message(
        PushTarget(ENDPOINT, RFC8291["ua_public"], RFC8291["auth"]),
        RFC8291["plaintext"],
        salt=decode(RFC8291["salt"]),
        ephemeral_private_key=key,
    )
    assert encode(body) == RFC8291["body"]


def test_encryption_refuses_unusable_keys_sizes_and_never_echoes_them():
    key = ec.generate_private_key(ec.SECP256R1())
    good = PushTarget(ENDPOINT, RFC8291["ua_public"], RFC8291["auth"])
    for target in (
        PushTarget(ENDPOINT, "not base64url!!", RFC8291["auth"]),
        PushTarget(ENDPOINT, RFC8291["ua_public"], "short"),
        PushTarget(ENDPOINT, encode(b"\x02" + bytes(64)), RFC8291["auth"]),
    ):
        with pytest.raises(WebPushError) as error:
            encrypt_push_message(
                target, b"x", salt=bytes(16), ephemeral_private_key=key
            )
        assert target.p256dh not in str(error.value)
        assert target.auth not in str(error.value)
    with pytest.raises(WebPushError, match="16 bytes"):
        encrypt_push_message(good, b"x", salt=bytes(15), ephemeral_private_key=key)
    with pytest.raises(WebPushError, match="too large"):
        encrypt_push_message(
            good,
            b"x" * (MAX_PAYLOAD_BYTES + 1),
            salt=bytes(16),
            ephemeral_private_key=key,
        )
    body = encrypt_push_message(
        good, b"x" * MAX_PAYLOAD_BYTES, salt=bytes(16), ephemeral_private_key=key
    )
    assert len(body) == RECORD_SIZE


def test_a_prepared_request_carries_a_vapid_header_and_an_opaque_body():
    built = build_web_push_request(
        configuration(),
        PushTarget(ENDPOINT, RFC8291["ua_public"], RFC8291["auth"]),
        b'{"title":"x"}',
    )
    headers = built.header_map()
    assert built.endpoint == ENDPOINT
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["TTL"] == "86400"
    authorization = headers["Authorization"]
    assert authorization.startswith("vapid t=") and f"k={PUBLIC_KEY}" in authorization
    token = authorization[len("vapid t=") :].split(",")[0]
    claims = json.loads(decode(token.split(".")[1]))
    assert claims["aud"] == "https://push.example.invalid"
    assert claims["sub"] == SUBJECT
    # The private key is nowhere in the request, and the body is ciphertext.
    assert PRIVATE_KEY not in authorization
    assert b'{"title"' not in built.body
    assert struct.unpack("!L", built.body[16:20])[0] == RECORD_SIZE

    with pytest.raises(WebPushError, match="endpoint"):
        build_web_push_request(
            configuration(),
            PushTarget(
                "http://push.example.invalid/x", RFC8291["ua_public"], RFC8291["auth"]
            ),
            b"{}",
        )
    for ttl in (-1, "86400", True):
        with pytest.raises(WebPushError, match="TTL"):
            build_web_push_request(
                configuration(),
                PushTarget(ENDPOINT, RFC8291["ua_public"], RFC8291["auth"]),
                b"{}",
                ttl_seconds=ttl,
            )


def test_two_requests_for_the_same_payload_never_reuse_salt_or_ephemeral_key():
    target = PushTarget(ENDPOINT, RFC8291["ua_public"], RFC8291["auth"])
    first = build_web_push_request(configuration(), target, b'{"a":1}')
    second = build_web_push_request(configuration(), target, b'{"a":1}')
    assert first.body[:16] != second.body[:16]
    assert first.body[21:86] != second.body[21:86]


def test_status_codes_map_to_the_delivery_outcomes_the_policy_states():
    for code in (200, 201, 202, 204):
        assert classify_status_code(code) == DeliveryOutcome(True)
    for code in (404, 410):
        outcome = classify_status_code(code)
        assert outcome.category is DeliveryErrorCategory.EXPIRED
        assert outcome.code == f"HTTP_{code}"
    for code in (429, 500, 502, 503, 504, 599):
        assert classify_status_code(code).category is DeliveryErrorCategory.RETRYABLE
    # Every other 4xx is this message's fault, never the subscription's.
    for code in (400, 401, 403, 405, 413, 422):
        outcome = classify_status_code(code)
        assert outcome.category is DeliveryErrorCategory.PERMANENT
        assert outcome.code == f"HTTP_{code}"
    assert transport_failure().category is DeliveryErrorCategory.RETRYABLE
    assert transport_failure().code == "TRANSPORT_ERROR"
    for code in (True, "200", 2.0):
        with pytest.raises(NotificationDeliveryError):
            classify_status_code(code)


def test_a_delivery_outcome_cannot_be_both_delivered_and_failed():
    with pytest.raises(NotificationDeliveryError):
        DeliveryOutcome(True, DeliveryErrorCategory.RETRYABLE, "HTTP_429")
    with pytest.raises(NotificationDeliveryError):
        DeliveryOutcome(False)
    with pytest.raises(NotificationDeliveryError):
        DeliveryOutcome(False, DeliveryErrorCategory.RETRYABLE, "")


def test_the_retry_schedule_is_deterministic_and_bounded():
    assert len(RETRY_BACKOFF_SECONDS) == MAX_DELIVERY_ATTEMPTS - 1
    delays = [
        retry_delay_seconds(attempt) for attempt in range(1, MAX_DELIVERY_ATTEMPTS)
    ]
    assert delays == list(RETRY_BACKOFF_SECONDS)
    assert delays == sorted(delays) and all(delay > 0 for delay in delays)
    # The last attempt is never followed by a wait, and neither is attempt zero.
    assert retry_delay_seconds(MAX_DELIVERY_ATTEMPTS) is None
    assert retry_delay_seconds(MAX_DELIVERY_ATTEMPTS + 3) is None
    assert retry_delay_seconds(0) is None


def test_the_payload_is_deterministic_and_built_only_from_the_stored_event():
    payload = build_push_payload(PAYLOAD)
    assert payload == {
        "event_type": "NEW_ACTIONABLE_OPPORTUNITY",
        "opportunity_id": 7,
        "title": "Senior Data Scientist",
        "body": "New actionable opportunity · Acme Research",
        "url": "/portfolio",
        "tag": "opportunity-radar:NEW_ACTIONABLE_OPPORTUNITY:7",
    }
    # Same bytes every time, whatever order the stored JSON happened to use.
    reordered = json.dumps(json.loads(PAYLOAD), sort_keys=True)
    assert encode_push_payload(PAYLOAD) == encode_push_payload(reordered)
    assert encode_push_payload(PAYLOAD) == encode_push_payload(PAYLOAD)

    escalated = json.loads(PAYLOAD)
    escalated["event_type"] = "ATTENTION_ESCALATED"
    assert build_push_payload(json.dumps(escalated))["body"] == (
        "Attention escalated · Acme Research"
    )
    # Nothing outside the stored event reaches the browser.
    assert set(payload) == {
        "event_type",
        "opportunity_id",
        "title",
        "body",
        "url",
        "tag",
    }
    assert "https://acme.example.invalid/jobs/7" not in json.dumps(payload)


def test_the_click_target_is_always_an_internal_path():
    for target in (
        "https://acme.example.invalid/jobs/7",
        "//evil.example.invalid/portfolio",
        "portfolio",
        "",
        None,
        "/portfolio\nx",
        "/portfolio with space",
        "\\portfolio",
    ):
        broken = json.loads(PAYLOAD)
        broken["opportunity"]["target_url"] = target
        with pytest.raises(NotificationPayloadError, match="internal click target"):
            build_push_payload(json.dumps(broken))
    kept = json.loads(PAYLOAD)
    kept["opportunity"]["target_url"] = "/portfolio?focus=7"
    assert build_push_payload(json.dumps(kept))["url"] == "/portfolio?focus=7"


def test_an_unreadable_stored_event_is_refused_rather_than_invented():
    for payload_json in (
        "",
        "   ",
        "not json",
        "[]",
        json.dumps({"event_type": "SPONTANEOUS"}),
        json.dumps({"event_type": "ATTENTION_ESCALATED"}),
        json.dumps({"event_type": "ATTENTION_ESCALATED", "opportunity": {}}),
        json.dumps(
            {
                "event_type": "ATTENTION_ESCALATED",
                "opportunity": {
                    "opportunity_id": 1,
                    "title": "  ",
                    "organization": "x",
                },
            }
        ),
        json.dumps(
            {
                "event_type": "ATTENTION_ESCALATED",
                "opportunity": {
                    "opportunity_id": True,
                    "title": "a",
                    "organization": "x",
                },
            }
        ),
    ):
        with pytest.raises(NotificationPayloadError):
            build_push_payload(payload_json)


def test_preparing_a_message_never_opens_a_socket(monkeypatch, caplog):
    def refuse(*arguments, **keywords):
        raise AssertionError("preparing a Web Push message must not reach the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    with caplog.at_level(logging.DEBUG):
        built = build_web_push_request(
            configuration(),
            PushTarget(ENDPOINT, RFC8291["ua_public"], RFC8291["auth"]),
            encode_push_payload(PAYLOAD),
        )
    assert built.body
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert PRIVATE_KEY not in logged and RFC8291["auth"] not in logged
