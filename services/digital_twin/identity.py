"""Deterministic email normalization for the Digital Twin identity root.

The same address written with a different case or surrounded by spaces must
resolve to one single user, so normalization happens once, here, before any
read or write. Validation stays deliberately conservative: it rejects an
address that is manifestly malformed rather than trying to implement RFC 5322.
"""

from __future__ import annotations

MAX_EMAIL_LENGTH = 254
MAX_LOCAL_PART_LENGTH = 64
_ALLOWED_DOMAIN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-."
)
_FORBIDDEN_LOCAL_CHARACTERS = frozenset("<>,;:\\\"()[]")


class InvalidEmailError(ValueError):
    """Raised when an address is manifestly not a usable email address."""


def _reject(reason: str) -> None:
    # The address itself is never interpolated: an invalid value is still
    # personal data and must not reach a message, a log, or a traceback.
    raise InvalidEmailError(f"invalid email address: {reason}")


def normalize_email(value: object) -> str:
    """Return the canonical form of an address, or raise `InvalidEmailError`.

    Canonical form is the trimmed address lowercased in full. The local part is
    case-sensitive in theory, but every provider this project can reach treats
    it as case-insensitive, and one person must not become two users because
    they capitalised their own address differently.
    """
    if not isinstance(value, str):
        _reject("value must be a string")
    candidate = value.strip()
    if not candidate:
        _reject("address is empty")
    if any(character.isspace() for character in candidate):
        _reject("address contains whitespace")
    if len(candidate) > MAX_EMAIL_LENGTH:
        _reject("address is too long")
    if candidate.count("@") != 1:
        _reject("address must contain exactly one @")
    local_part, _, domain = candidate.partition("@")
    if not local_part:
        _reject("address has no local part")
    if len(local_part) > MAX_LOCAL_PART_LENGTH:
        _reject("local part is too long")
    if _FORBIDDEN_LOCAL_CHARACTERS & set(local_part):
        _reject("local part contains a forbidden character")
    if not domain:
        _reject("address has no domain")
    if not set(domain) <= _ALLOWED_DOMAIN_CHARACTERS:
        _reject("domain contains a forbidden character")
    labels = domain.split(".")
    if len(labels) < 2:
        _reject("domain must contain at least one dot")
    for label in labels:
        if not label:
            _reject("domain has an empty label")
        if label.startswith("-") or label.endswith("-"):
            _reject("domain label cannot start or end with a hyphen")
    return candidate.lower()
