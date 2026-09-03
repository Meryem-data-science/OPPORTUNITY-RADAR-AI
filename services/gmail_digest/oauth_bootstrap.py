"""Authorize this deployment to send one thing: the daily digest. Once.

This is an operator command, run by a person at a terminal, and nothing
imports it. Delivery never falls back to it: a deployment without a stored
authorization stays a deployment that cannot send until somebody runs this and
watches the consent screen themselves. Minting an authorization implicitly, in
the middle of a drain, would be a browser window opening on a machine nobody
is sitting at — and a scope grant nobody read.

    python -m services.gmail_digest.oauth_bootstrap

The consent screen it opens asks for exactly one permission, *Send email on
your behalf* — ``https://www.googleapis.com/auth/gmail.send``. It does not ask
to read mail, and this command refuses the result if Google returns anything
wider. There is no way to widen it from here: the scope is a constant in
:mod:`services.gmail_digest.oauth`, the client configuration is checked for
scopes of its own before the flow starts, and the granted scopes are checked
again before the token is written.

What is printed: the token path, the scope, and the fact that it worked. What
is never printed, logged, or put in an exception: the access token, the
refresh token, the client secret, and the contents of either JSON file.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .oauth import (
    DEFAULT_CLIENT_SECRET_PATH,
    DEFAULT_TOKEN_PATH,
    GMAIL_SEND_SCOPE,
    GMAIL_SEND_SCOPES,
    GmailOAuthConfiguration,
    GmailOAuthError,
    _google_dependencies,
    file_is_owner_only,
    validate_client_configuration,
    validate_send_only_scope,
    write_token_document,
)


def authorize_send_only(
    configuration: GmailOAuthConfiguration,
    *,
    overwrite: bool = False,
    port: int = 0,
    dependencies: Any | None = None,
) -> Path:
    """Run one Desktop consent flow for gmail.send and store what it returns.

    The order is the safety property. The client configuration is validated
    before a flow object exists; the flow is built with the one scope constant
    and nothing else; the credentials that come back are checked for exact
    send-only scope *before* they are written; and the write itself is atomic
    and refuses to replace an existing authorization without ``--force``.
    """
    resolved = dependencies if dependencies is not None else _google_dependencies()
    client_path = Path(configuration.client_secret_path)
    if not client_path.is_file():
        raise GmailOAuthError(
            "Gmail OAuth client configuration does not exist;"
            " download a Desktop app client from Google Cloud first"
        )
    validate_client_configuration(client_path)
    try:
        flow = resolved.InstalledAppFlow.from_client_secrets_file(
            str(client_path), list(GMAIL_SEND_SCOPES)
        )
        credentials = flow.run_local_server(port=port)
    except GmailOAuthError:
        raise
    except Exception as error:
        raise GmailOAuthError(
            f"Gmail authorization did not complete ({type(error).__name__})"
        ) from error
    if credentials is None or not getattr(credentials, "valid", False):
        raise GmailOAuthError("Gmail authorization did not produce valid credentials")
    # Google may return a narrower or wider grant than the one asked for. A
    # wider one is refused here rather than stored and used carefully later.
    validate_send_only_scope(credentials)
    if not getattr(credentials, "refresh_token", None):
        raise GmailOAuthError(
            "Gmail authorization returned no refresh token;"
            " revoke this app's access in your Google Account and try again"
        )
    try:
        payload = credentials.to_json()
    except Exception as error:
        raise GmailOAuthError(
            f"Gmail authorization could not be serialized ({type(error).__name__})"
        ) from error
    if not isinstance(payload, str) or not payload.strip():
        raise GmailOAuthError("Gmail authorization serialized to nothing")
    return write_token_document(configuration.token_path, payload, overwrite=overwrite)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Authorize this deployment to send the daily Gmail digest."
            " Requests gmail.send and nothing else; never reads a mailbox."
        )
    )
    parser.add_argument(
        "--client-secret",
        default=str(DEFAULT_CLIENT_SECRET_PATH),
        help=(
            "Desktop/Installed App OAuth client JSON downloaded from Google"
            f" Cloud (default: {DEFAULT_CLIENT_SECRET_PATH})."
        ),
    )
    parser.add_argument(
        "--token",
        default=str(DEFAULT_TOKEN_PATH),
        help=f"Where to store the authorization (default: {DEFAULT_TOKEN_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Replace an existing authorization. The stored one keeps working"
            " until it is replaced, so this is never done by accident."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port for the consent redirect (default: an ephemeral one).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configuration = GmailOAuthConfiguration.local(
        client_secret_path=args.client_secret, token_path=args.token
    )
    try:
        written = authorize_send_only(
            configuration, overwrite=args.force, port=args.port
        )
    except GmailOAuthError as error:
        # Only this module's own sentences reach the terminal, and none of them
        # is ever built from a credential.
        print(f"gmail oauth bootstrap failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"gmail oauth bootstrap failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"scope={GMAIL_SEND_SCOPE}")
    print(f"wrote {written}")
    if not file_is_owner_only(written):
        print(
            "warning: the token file is readable by more than its owner;"
            " tighten its permissions.",
            file=sys.stderr,
        )
    print(
        "That file is the authorization. It is a secret, it is ignored by Git,"
        " it belongs in no image and no log, and it grants sending only —"
        " this deployment cannot read your mailbox."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
