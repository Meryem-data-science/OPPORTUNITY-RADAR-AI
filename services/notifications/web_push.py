"""Server-side Web Push: VAPID identity, RFC 8291 encryption, one request.

Everything here is server-only. The VAPID *private* key is read from the
environment, kept as raw bytes on a single object that refuses to render
itself, and never reaches a payload, a log record, an exception message, or a
browser. The public application server key is the only half a browser is ever
shown, and Phase 5.3A already serves it.

The configuration is validated before anything is encrypted and long before a
socket exists: a missing subject, a malformed key, or a private key that does
not belong to the configured public key all fail closed, so a deployment that
is half-configured sends nothing at all rather than sending something a push
service will reject.

Encryption follows RFC 8291 (``aes128gcm``) with a single record, and the
authorization header follows RFC 8292. Both take their randomness through
parameters so a test can pin them; neither reads a clock or an environment
variable of its own.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidKey, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asymmetric_utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .push_subscriptions import (
    AUTH_BYTE_LENGTH,
    MAX_KEY_LENGTH,
    P256DH_BYTE_LENGTH,
    P256DH_UNCOMPRESSED_PREFIX,
    decode_base64url,
)

#: Environment variables this sender reads. None of them may ever be exposed
#: to the browser bundle, so none of them is a ``NEXT_PUBLIC_`` variable.
VAPID_PUBLIC_KEY_VARIABLE = "WEB_PUSH_VAPID_PUBLIC_KEY"
VAPID_PRIVATE_KEY_VARIABLE = "WEB_PUSH_VAPID_PRIVATE_KEY"
VAPID_SUBJECT_VARIABLE = "WEB_PUSH_VAPID_SUBJECT"

#: A VAPID private key is the raw P-256 scalar, base64url encoded — the shape
#: ``web-push generate-vapid-keys`` and every comparable tool emits.
PRIVATE_KEY_BYTE_LENGTH = 32

#: Lifetime of one VAPID assertion. RFC 8292 caps it at 24 hours; a shorter
#: window is signed per request, so a captured header expires quickly.
VAPID_TOKEN_LIFETIME = timedelta(hours=12)

#: One record, and a body a push service will accept. The aes128gcm header is
#: 86 bytes, the padding delimiter one, and the GCM tag sixteen.
RECORD_SIZE = 4096
_HEADER_LENGTH = 86
MAX_PAYLOAD_BYTES = RECORD_SIZE - _HEADER_LENGTH - 1 - 16

#: How long a push service should hold an undelivered message for.
DEFAULT_TTL_SECONDS = 86400

_KEY_INFO_PREFIX = b"WebPush: info\x00"
_CEK_INFO = b"Content-Encoding: aes128gcm\x00"
_NONCE_INFO = b"Content-Encoding: nonce\x00"

#: What a caller may say about a failure without describing a credential.
PUBLIC_CONFIGURATION_ERROR = "Web Push VAPID configuration is missing or invalid."


class WebPushError(RuntimeError):
    """Raised when a Web Push message cannot be prepared or delivered."""


class WebPushConfigurationError(WebPushError):
    """Raised when VAPID configuration is absent, malformed, or inconsistent."""


class WebPushTransportError(WebPushError):
    """Raised by a transport when the request never produced a response."""


def _b64(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PushTarget:
    """The three values a push service needs, and nothing else about a user."""

    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class WebPushRequest:
    """One prepared, already encrypted request. The body is opaque bytes."""

    endpoint: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header_map(self) -> dict[str, str]:
        return dict(self.headers)


@dataclass(frozen=True)
class WebPushResponse:
    """What a transport reports back: a status code, and nothing quotable."""

    status_code: int


class WebPushTransport(Protocol):
    """Anything that can turn one prepared request into a status code."""

    def __call__(self, request: WebPushRequest) -> WebPushResponse:
        """Send the request, or raise :class:`WebPushTransportError`."""


@dataclass(frozen=True)
class VapidConfiguration:
    """A validated VAPID identity whose private half never renders itself."""

    public_key: str
    subject: str
    _private_key: ec.EllipticCurvePrivateKey = field(repr=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        # A dataclass repr would print the field name and the key object; this
        # one guarantees no traceback, log, or debugger ever shows either.
        return "VapidConfiguration(public_key=<set>, subject=<set>, private_key=<redacted>)"

    __str__ = __repr__

    def authorization(self, endpoint: str, *, now: datetime) -> str:
        """Sign one RFC 8292 assertion for the origin of this endpoint."""
        return _vapid_authorization(self, endpoint, now=now)


def _decoded_public_key(value: str) -> bytes:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    if len(value) > MAX_KEY_LENGTH:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    decoded = decode_base64url(value)
    if (
        decoded is None
        or len(decoded) != P256DH_BYTE_LENGTH
        or decoded[0] != P256DH_UNCOMPRESSED_PREFIX
    ):
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    return decoded


def _private_key(value: str) -> ec.EllipticCurvePrivateKey:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    if len(value) > MAX_KEY_LENGTH:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    decoded = decode_base64url(value)
    if decoded is None or len(decoded) != PRIVATE_KEY_BYTE_LENGTH:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    try:
        return ec.derive_private_key(int.from_bytes(decoded, "big"), ec.SECP256R1())
    except (ValueError, InvalidKey, UnsupportedAlgorithm) as error:
        # The scalar itself is never named, only the fact that it is not one.
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR) from error


def _subject(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    if len(value) > 256:
        raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
    parts = urlsplit(value)
    if parts.scheme == "mailto":
        if "@" not in parts.path or parts.path.startswith("@"):
            raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)
        return value
    if parts.scheme == "https" and parts.hostname:
        return value
    raise WebPushConfigurationError(PUBLIC_CONFIGURATION_ERROR)


def build_vapid_configuration(
    *, public_key: str, private_key: str, subject: str
) -> VapidConfiguration:
    """Validate one VAPID identity, refusing anything a push service would.

    The two halves are checked against each other here, before any encryption
    and before any socket: a private key that derives a different public point
    is a misconfiguration this deployment can detect on its own rather than
    discover as a wall of 401s.
    """
    expected = _decoded_public_key(public_key)
    key = _private_key(private_key)
    derived = key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    if derived != expected:
        raise WebPushConfigurationError("Web Push VAPID keys do not form a pair.")
    return VapidConfiguration(public_key, _subject(subject), key)


def load_vapid_configuration(
    environment: dict[str, str] | None = None,
) -> VapidConfiguration:
    """Read and validate the VAPID identity from the process environment."""
    source = os.environ if environment is None else environment
    values = []
    for variable in (
        VAPID_PUBLIC_KEY_VARIABLE,
        VAPID_PRIVATE_KEY_VARIABLE,
        VAPID_SUBJECT_VARIABLE,
    ):
        raw = source.get(variable)
        value = "" if raw is None else raw.strip()
        if not value:
            # Named, because the *name* of a missing variable is not a secret.
            raise WebPushConfigurationError(
                f"{variable} is not configured; Web Push delivery is disabled."
            )
        values.append(value)
    return build_vapid_configuration(
        public_key=values[0], private_key=values[1], subject=values[2]
    )


def _vapid_authorization(
    configuration: VapidConfiguration, endpoint: str, *, now: datetime
) -> str:
    parts = urlsplit(endpoint)
    if parts.scheme != "https" or not parts.hostname:
        raise WebPushError("push endpoint is not a valid endpoint")
    audience = f"{parts.scheme}://{parts.netloc}"
    header = _b64(
        json.dumps(
            {"typ": "JWT", "alg": "ES256"}, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
    expiry = int((now + VAPID_TOKEN_LIFETIME).timestamp())
    claims = _b64(
        json.dumps(
            {"aud": audience, "exp": expiry, "sub": configuration.subject},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    der = configuration._private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asymmetric_utils.decode_dss_signature(der)
    signature = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{claims}.{signature}, k={configuration.public_key}"


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(
        ikm
    )


def encrypt_push_message(
    target: PushTarget,
    payload: bytes,
    *,
    salt: bytes,
    ephemeral_private_key: ec.EllipticCurvePrivateKey,
) -> bytes:
    """Encrypt one payload for one subscription, as a single aes128gcm record.

    Salt and ephemeral key are parameters rather than module-level randomness
    so the caller owns the entropy and a test can make the body reproducible.
    """
    if not isinstance(payload, bytes):
        raise WebPushError("push payload must be bytes")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise WebPushError("push payload is too large for one record")
    if len(salt) != 16:
        raise WebPushError("push salt must be 16 bytes")
    client_public = decode_base64url(target.p256dh)
    auth_secret = decode_base64url(target.auth)
    if (
        client_public is None
        or len(client_public) != P256DH_BYTE_LENGTH
        or client_public[0] != P256DH_UNCOMPRESSED_PREFIX
        or auth_secret is None
        or len(auth_secret) != AUTH_BYTE_LENGTH
    ):
        # The keys are the credential: the refusal never repeats them.
        raise WebPushError("push subscription keys are not usable")
    try:
        client_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), client_public
        )
    except ValueError as error:
        raise WebPushError("push subscription keys are not usable") from error
    server_public = ephemeral_private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    shared = ephemeral_private_key.exchange(ec.ECDH(), client_key)
    ikm = _hkdf(
        auth_secret,
        shared,
        _KEY_INFO_PREFIX + client_public + server_public,
        32,
    )
    content_key = _hkdf(salt, ikm, _CEK_INFO, 16)
    nonce = _hkdf(salt, ikm, _NONCE_INFO, 12)
    # 0x02 is the delimiter of the last record; there is only ever one here.
    ciphertext = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)
    return (
        salt
        + struct.pack("!L", RECORD_SIZE)
        + struct.pack("!B", len(server_public))
        + server_public
        + ciphertext
    )


def build_web_push_request(
    configuration: VapidConfiguration,
    target: PushTarget,
    payload: bytes,
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    salt: bytes | None = None,
    ephemeral_private_key: ec.EllipticCurvePrivateKey | None = None,
) -> WebPushRequest:
    """Prepare the exact HTTP request one push service will be handed."""
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise WebPushError("push TTL must be an integer number of seconds")
    if ttl_seconds < 0:
        raise WebPushError("push TTL must be an integer number of seconds")
    moment = datetime.now(timezone.utc) if now is None else now
    body = encrypt_push_message(
        target,
        payload,
        salt=os.urandom(16) if salt is None else salt,
        ephemeral_private_key=(
            ec.generate_private_key(ec.SECP256R1())
            if ephemeral_private_key is None
            else ephemeral_private_key
        ),
    )
    headers = (
        ("Authorization", configuration.authorization(target.endpoint, now=moment)),
        ("Content-Encoding", "aes128gcm"),
        ("Content-Type", "application/octet-stream"),
        ("TTL", str(ttl_seconds)),
    )
    return WebPushRequest(target.endpoint, headers, body)


class HttpxWebPushTransport:
    """The only place a real socket is opened, and it holds no secrets.

    ``httpx`` is imported lazily so that importing this module — which every
    delivery test does — never pulls in an HTTP stack, and so a test that
    forbids sockets can assert nothing here was ever constructed.
    """

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def __call__(self, request: WebPushRequest) -> WebPushResponse:
        import httpx

        try:
            response = httpx.post(
                request.endpoint,
                headers=request.header_map(),
                content=request.body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            # The endpoint is inside most httpx errors; the type name is not.
            raise WebPushTransportError(
                f"push request failed: {type(error).__name__}"
            ) from None
        return WebPushResponse(response.status_code)
