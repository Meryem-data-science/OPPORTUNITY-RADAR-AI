"""Resolve supported Indeed job links without performing network requests.

Some Indeed email links use ``cts.indeed.com/v3/<payload>/<signature>``.
Their payload is a base64url-encoded gzip JSON object; only its explicit
``u`` field is considered. This extracts a candidate URL, not a verified
signature or a job identity. Callers must still validate the job key.
"""

from __future__ import annotations

import base64
import binascii
from html import unescape
import json
import re
from urllib.parse import SplitResult, urlsplit
import zlib


MAX_URL_LENGTH = 32_768
MAX_ENCODED_PAYLOAD_LENGTH = 16_384
MAX_COMPRESSED_PAYLOAD_BYTES = 12_288
MAX_DECODED_PAYLOAD_BYTES = 65_536

_JOB_HOSTS = frozenset({"ma.indeed.com", "www.indeed.com", "indeed.com"})
_JOB_PATHS = frozenset({"/viewjob", "/rc/clk", "/pagead/clk", "/jobs"})
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")
_CTS_PATH = re.compile(
    rf"/v3/([A-Za-z0-9_-]{{1,{MAX_ENCODED_PAYLOAD_LENGTH}}})/"
    r"[A-Za-z0-9_-]{1,256}"
)


def _split_safe_url(value: str) -> SplitResult | None:
    if not value or len(value) > MAX_URL_LENGTH or _CONTROL_OR_SPACE.search(value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            # Also reject an explicitly empty port and unusual authorities.
            or parsed.netloc.lower() != parsed.hostname.lower()
        ):
            return None
    except ValueError:
        return None
    return parsed


def _direct_job_url(value: str) -> str | None:
    parsed = _split_safe_url(value)
    if (
        parsed is not None
        and parsed.hostname.lower() in _JOB_HOSTS
        and parsed.path.removesuffix("/") in _JOB_PATHS
    ):
        return value
    return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> object:
    raise ValueError("Nonstandard JSON constant")


def _payload_url(token: str) -> str | None:
    try:
        compressed = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        )
        if (
            not compressed
            or len(compressed) > MAX_COMPRESSED_PAYLOAD_BYTES
            or base64.urlsafe_b64encode(compressed).rstrip(b"=").decode("ascii") != token
        ):
            return None
        inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        decoded = inflater.decompress(compressed, MAX_DECODED_PAYLOAD_BYTES + 1)
        if (
            len(decoded) > MAX_DECODED_PAYLOAD_BYTES
            or not inflater.eof
            or inflater.unconsumed_tail
            or inflater.unused_data
        ):
            # Reject truncated streams, concatenated members and trailing data.
            return None
        payload = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_json_constant,
        )
    except (ValueError, binascii.Error, UnicodeError, zlib.error, RecursionError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("u"), str):
        return None
    return _direct_job_url(payload["u"])


def unwrap_indeed_url(url: str) -> str | None:
    """Return a supported direct job URL or the bounded CTS payload's URL.

    Tracking links are decoded locally once. Other destinations, nested
    trackers and unsupported URL forms return ``None``. No URL is requested.
    """
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return None
    value = unescape(url).strip()
    parsed = _split_safe_url(value)
    if parsed is None:
        return None
    if parsed.hostname.lower() in _JOB_HOSTS:
        return _direct_job_url(value)
    if parsed.hostname.lower() != "cts.indeed.com" or parsed.query or parsed.fragment:
        return None
    matched = _CTS_PATH.fullmatch(parsed.path)
    return _payload_url(matched.group(1)) if matched else None
