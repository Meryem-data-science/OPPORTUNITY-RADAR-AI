"""Generate one stable VAPID identity for a local deployment, once.

This is an operator command, not application code. Nothing imports it at
startup, nothing calls it lazily, and no code path anywhere generates a key
when one is missing: a deployment without a VAPID identity stays a deployment
without Web Push until a person runs this and says so. Silently minting a key
would be worse than the outage it hides — the public half is baked into every
browser subscription, so an identity that appears on its own invalidates every
device the moment it is replaced.

What it writes is a small environment file the operator sources or copies into
their secret store. The private half is written there and nowhere else: it is
never printed, never logged, never returned in a rendered form, and never put
in an exception. The public half and the path are printed, because both are
things the operator legitimately needs to see.

The identity is validated through
:func:`~services.notifications.web_push.build_vapid_configuration` before
anything is written, so this command cannot produce a file the sender would
refuse: same curve, same encodings, a private key that really derives the
public one, and a subject RFC 8292 accepts.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .web_push import (
    PRIVATE_KEY_BYTE_LENGTH,
    VAPID_PRIVATE_KEY_VARIABLE,
    VAPID_PUBLIC_KEY_VARIABLE,
    VAPID_SUBJECT_VARIABLE,
    WebPushConfigurationError,
    build_vapid_configuration,
    valid_vapid_subject,
)

#: Where an identity goes unless the operator says otherwise. ``.secrets/`` is
#: already ignored by Git for the Gmail credentials, so a secret written here
#: cannot be committed by accident.
DEFAULT_OUTPUT_PATH = Path(".secrets/web-push.env")

#: Owner read/write. Applied where the platform has POSIX permissions at all;
#: on Windows the flag is advisory and the file is simply created normally.
SECRET_FILE_MODE = 0o600


class VapidBootstrapError(RuntimeError):
    """Raised when an identity cannot be generated or written safely."""


@dataclass(frozen=True)
class VapidIdentity:
    """One generated VAPID pair, whose private half never renders itself."""

    public_key: str
    subject: str
    _private_key: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"VapidIdentity(public_key={self.public_key!r},"
            f" subject={self.subject!r}, private_key=<redacted>)"
        )

    __str__ = __repr__


def _b64(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def generate_vapid_identity(subject: str) -> VapidIdentity:
    """Mint one P-256 pair in exactly the encodings the sender reads.

    The private key is the raw 32-byte scalar and the public key the
    uncompressed point, both base64url without padding — the shapes
    ``web_push`` already validates, so the result is checked against the real
    loader here rather than trusted.
    """
    if not valid_vapid_subject(subject):
        # Refused before any key exists: there is nothing to discard, and the
        # operator gets the rule rather than a failure from further down.
        raise VapidBootstrapError(
            f"{VAPID_SUBJECT_VARIABLE} must be a 'mailto:' address or an 'https://' URL"
        )
    key = ec.generate_private_key(ec.SECP256R1())
    private_key = _b64(
        key.private_numbers().private_value.to_bytes(PRIVATE_KEY_BYTE_LENGTH, "big")
    )
    public_key = _b64(
        key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    )
    try:
        # The identity is only an identity if the sender would accept it.
        build_vapid_configuration(
            public_key=public_key, private_key=private_key, subject=subject
        )
    except WebPushConfigurationError as error:
        # Whatever went wrong, the scalar is not part of saying so.
        raise VapidBootstrapError(
            f"generated VAPID identity is unusable: {error}"
        ) from error
    return VapidIdentity(public_key, subject, private_key)


def render_identity_file(identity: VapidIdentity) -> str:
    """Render the environment file an operator sources or copies from."""
    return "\n".join(
        (
            "# Opportunity Radar AI — Web Push VAPID identity.",
            "# Generated locally. This file is a secret: it is ignored by Git,",
            "# it belongs in no image and no log, and replacing it invalidates",
            "# every browser subscription already registered against it.",
            f"{VAPID_PUBLIC_KEY_VARIABLE}={identity.public_key}",
            f"{VAPID_PRIVATE_KEY_VARIABLE}={identity._private_key}",
            f"{VAPID_SUBJECT_VARIABLE}={identity.subject}",
            "",
        )
    )


def write_vapid_identity(
    path: Path, identity: VapidIdentity, *, overwrite: bool = False
) -> Path:
    """Write one identity to ``path``, refusing to replace an existing one.

    The refusal is the point. An existing file is the identity every browser
    subscription in the database was made against, so overwriting it silently
    would break every one of them at once. Creation is ``O_EXCL`` rather than
    an "exists?" check followed by a write, so two runs at the same time cannot
    both decide the file is absent.
    """
    resolved = Path(path)
    if resolved.parent != Path(""):
        resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    try:
        descriptor = os.open(resolved, flags, SECRET_FILE_MODE)
    except FileExistsError as error:
        raise VapidBootstrapError(
            f"{resolved} already holds a VAPID identity; refusing to replace it"
        ) from error
    except OSError as error:
        raise VapidBootstrapError(
            f"cannot write {resolved}: {type(error).__name__}"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_identity_file(identity))
    except OSError as error:
        raise VapidBootstrapError(
            f"cannot write {resolved}: {type(error).__name__}"
        ) from error
    try:
        os.chmod(resolved, SECRET_FILE_MODE)
    except (OSError, NotImplementedError):
        # Windows and some mounts have no POSIX mode to set. The file is still
        # written and still outside the repository's tracked tree.
        pass
    return resolved


def file_is_owner_only(path: Path) -> bool:
    """Whether the file is readable by its owner alone, where that is a thing."""
    return not stat.S_IMODE(Path(path).stat().st_mode) & 0o077


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate one local VAPID identity for Web Push. Writes a secret"
            " file; prints the public key and never the private one."
        )
    )
    parser.add_argument(
        "--subject",
        required=True,
        help=(
            "RFC 8292 contact for the push services: a 'mailto:' address or an"
            " 'https://' URL an operator can be reached at."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help=f"Where to write the identity (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Replace an existing identity. Every browser subscription already"
            " registered against the old public key stops working."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        identity = generate_vapid_identity(args.subject)
        written = write_vapid_identity(
            Path(args.output), identity, overwrite=args.force
        )
    except (VapidBootstrapError, WebPushConfigurationError) as error:
        # Only this module's own sentences reach the terminal, and none of them
        # is ever built from a key.
        print(f"vapid bootstrap failed: {error}", file=sys.stderr)
        return 1
    print(f"{VAPID_PUBLIC_KEY_VARIABLE}={identity.public_key}")
    print(f"{VAPID_SUBJECT_VARIABLE}={identity.subject}")
    print(f"wrote {written}")
    print(
        "The private key is in that file only. Load it into the server"
        " environment, keep it out of the browser bundle, and do not replace it"
        " while subscriptions exist."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
