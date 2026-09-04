"""Send-only Gmail authorization: one scope, two local files, no database.

Everything Phase 5.4B needs from Google is the permission to hand Gmail one
already-written message. That is exactly one OAuth scope —
``https://www.googleapis.com/auth/gmail.send`` — and this module is the only
place in the delivery path that knows it. There is no read scope here, no
``gmail.modify``, no ``gmail.metadata`` and no ``mail.google.com``; a token
that carries any of them is refused rather than used narrowly, because a
credential this process holds is a credential this process could be made to
misuse. Gmail Intelligence — reading a mailbox, classifying a reply, tracking
an application — is Phase 7 and needs its own consent, not a scope quietly
widened here.

Two files, both local, both outside the repository's tracked tree:

``.secrets/gmail-oauth-client.json``
    The Desktop/Installed-App OAuth client, downloaded from Google Cloud by a
    person. Read, never written.

``.secrets/gmail-token.json``
    The reusable authorization this deployment obtained. Written by
    :mod:`services.gmail_digest.oauth_bootstrap`, refreshed here, and written
    atomically both times so a crash mid-write cannot leave a half a token.

Neither file ever enters SQLite, a log line, an exception message, or a
``repr``. Failures here name what went wrong in this module's own words and
carry nothing from the credential that failed.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The one scope this phase may hold. Sending a message is the whole mandate.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

#: What the flow requests and what a loaded credential must grant — exactly.
GMAIL_SEND_SCOPES: tuple[str, ...] = (GMAIL_SEND_SCOPE,)

#: Scopes whose presence is a refusal rather than a warning. Every one of them
#: reads or mutates a mailbox, which is not this phase's business and not what
#: the user consented to when they authorized a daily digest. The check that
#: matters is the exact-equality one below; this set exists so the operator is
#: told *which* over-broad grant they are holding.
FORBIDDEN_SCOPES: frozenset[str] = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.settings.sharing",
    }
)

#: Where the two local files live unless an operator says otherwise.
#: ``.secrets/`` is already ignored by Git, so a credential written here cannot
#: be committed by accident.
DEFAULT_CLIENT_SECRET_PATH = Path(".secrets/gmail-oauth-client.json")
DEFAULT_TOKEN_PATH = Path(".secrets/gmail-token.json")

#: Owner read/write, applied where the platform has POSIX permissions at all.
SECRET_FILE_MODE = 0o600

#: A refusal to read something enormous as if it were a small JSON document.
MAX_SECRET_FILE_BYTES = 128 * 1024


class GmailOAuthError(RuntimeError):
    """Raised when send-only authorization cannot be established safely.

    Every message this class carries is written here. Nothing from a token, a
    client secret, or a Google response body is ever interpolated into one.
    """


def _google_dependencies() -> Any:
    """Import the official SDK at the boundary, not at module import.

    Keeping the import here is what lets the whole delivery path — classifier,
    MIME builder, persistence, drain — be tested on a machine with no Google
    packages installed and no network, and it is the seam a test replaces to
    prove the flow requests one scope without ever contacting Google.
    """
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    class Dependencies:
        pass

    dependencies = Dependencies()
    dependencies.GoogleAuthError = GoogleAuthError
    dependencies.Request = Request
    dependencies.Credentials = Credentials
    dependencies.InstalledAppFlow = InstalledAppFlow
    dependencies.build = build
    return dependencies


@dataclass(frozen=True)
class GmailOAuthConfiguration:
    """Where the two local files are. The paths are not secret; the files are.

    The paths are kept out of ``repr`` anyway: a traceback that names them is
    a traceback that tells a reader exactly which file to go and read.
    """

    client_secret_path: Path = field(repr=False)
    token_path: Path = field(repr=False)

    @classmethod
    def local(
        cls,
        *,
        client_secret_path: str | Path | None = None,
        token_path: str | Path | None = None,
    ) -> GmailOAuthConfiguration:
        return cls(
            Path(
                DEFAULT_CLIENT_SECRET_PATH
                if client_secret_path is None
                else client_secret_path
            ),
            Path(DEFAULT_TOKEN_PATH if token_path is None else token_path),
        )

    def __repr__(self) -> str:
        return "GmailOAuthConfiguration(client_secret_path=<set>, token_path=<set>)"

    __str__ = __repr__


def _read_json_document(path: Path, description: str) -> dict[str, Any]:
    """Read one small local JSON object, or say why it could not be read.

    The file's *contents* never appear in the failure: a malformed token is
    reported as malformed, not quoted back into a terminal or a log.
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        raise GmailOAuthError(f"{description} is not readable") from error
    if size > MAX_SECRET_FILE_BYTES:
        raise GmailOAuthError(
            f"{description} is implausibly large; refusing to read it"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GmailOAuthError(f"{description} is missing or not valid JSON") from error
    if not isinstance(document, dict):
        raise GmailOAuthError(f"{description} is not a JSON object")
    return document


def _scope_strings(value: Any) -> list[str]:
    """Every Google auth scope appearing anywhere inside a parsed document.

    A client configuration has no business naming a scope at all, but nothing
    stops a hand-edited or vendor-modified file from carrying one — so the
    whole document is walked and every ``.../auth/...`` string is collected,
    rather than only the couple of keys the SDK happens to read today.
    """
    found: list[str] = []
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            for token in current.replace(",", " ").split():
                if token.startswith("https://www.googleapis.com/auth/") or token in (
                    "https://mail.google.com/",
                    "https://mail.google.com",
                ):
                    found.append(token)
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return found


def validate_client_configuration(path: Path) -> dict[str, Any]:
    """Refuse a client file that is not a Desktop app, or that names a scope.

    Two different escalations are possible before a single browser window
    opens. A *web* client would run a redirect flow this command does not own,
    and a client configuration that carries its own scope list could hand the
    consent screen something wider than the one constant above. Both are
    refused here, before the flow exists, so a bad file costs an error message
    rather than a real Google consent.
    """
    document = _read_json_document(Path(path), "Gmail OAuth client configuration")
    section = document.get("installed")
    if not isinstance(section, dict):
        raise GmailOAuthError(
            "Gmail OAuth client must be a Desktop/Installed App client"
            " (a JSON document with an 'installed' section)"
        )
    for required in ("client_id", "client_secret", "auth_uri", "token_uri"):
        value = section.get(required)
        if not isinstance(value, str) or not value.strip():
            raise GmailOAuthError(
                f"Gmail OAuth client configuration is missing {required}"
            )
    escalating = sorted(
        {scope for scope in _scope_strings(document) if scope != GMAIL_SEND_SCOPE}
    )
    if escalating:
        # The scope *names* are safe to print — they are a public vocabulary,
        # not a credential — and they are the only useful thing to say here.
        raise GmailOAuthError(
            "Gmail OAuth client configuration names scopes beyond gmail.send: "
            + ", ".join(escalating)
        )
    return document


def granted_scopes(credentials: Any) -> tuple[frozenset[str], ...]:
    """Every view a credential offers of what it was granted."""
    views: list[frozenset[str]] = []
    for attribute in ("scopes", "granted_scopes"):
        value = getattr(credentials, attribute, None)
        if value is not None:
            views.append(frozenset(value))
    return tuple(views)


def validate_send_only_scope(credentials: Any) -> None:
    """Require every scope view to be exactly ``{gmail.send}``.

    Exact equality, not "contains send": a token that also grants
    ``gmail.readonly`` would let a bug — or a later phase written in a hurry —
    read a mailbox this phase promised never to open. A credential that
    exposes no scope view at all is refused too, because an unknown grant is
    not a narrow one.
    """
    required = frozenset(GMAIL_SEND_SCOPES)
    views = granted_scopes(credentials)
    if not views:
        raise GmailOAuthError(
            "Gmail credentials do not declare their scopes; refusing to use them"
        )
    for view in views:
        forbidden = sorted(view & FORBIDDEN_SCOPES)
        if forbidden:
            raise GmailOAuthError(
                "Gmail credentials grant mailbox scopes this phase must not hold: "
                + ", ".join(forbidden)
            )
        if view != required:
            raise GmailOAuthError(
                "Gmail credentials must grant exactly gmail.send;"
                " re-run the OAuth bootstrap to replace them"
            )


def write_token_document(
    path: str | Path, payload: str, *, overwrite: bool = False
) -> Path:
    """Write one token file atomically, and never half of one.

    The content is written to a temporary file in the *same* directory, given
    owner-only permissions, flushed to disk, and then moved onto the
    destination with :func:`os.replace` — an atomic rename on every platform
    this runs on. A reader therefore sees either the previous token or the new
    one, never a truncated JSON document, and a crash mid-write costs a stray
    temporary file rather than the authorization.

    When ``overwrite`` is false the destination is reserved first with
    ``O_EXCL``, so two bootstraps racing each other cannot both decide the file
    was absent. Replacing an existing authorization is a deliberate act with a
    flag behind it, because the old token keeps working until it is replaced
    and silently discarding it is how a working deployment stops sending.
    """
    destination = Path(path)
    directory = destination.parent if str(destination.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)
    reserved = False
    if not overwrite:
        try:
            os.close(
                os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    SECRET_FILE_MODE,
                )
            )
        except FileExistsError as error:
            raise GmailOAuthError(
                f"{destination} already holds a Gmail authorization;"
                " re-run with --force to replace it"
            ) from error
        except OSError as error:
            raise GmailOAuthError(
                f"cannot write {destination}: {type(error).__name__}"
            ) from error
        reserved = True
    descriptor, temporary = tempfile.mkstemp(
        dir=str(directory), prefix=".gmail-token-", suffix=".tmp"
    )
    try:
        try:
            os.fchmod(descriptor, SECRET_FILE_MODE)
        except (OSError, NotImplementedError, AttributeError):
            # Windows and some mounts have no POSIX mode to set; the file is
            # still written outside the repository's tracked tree.
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as error:
        for leftover in (Path(temporary), destination if reserved else None):
            if leftover is not None:
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
        raise GmailOAuthError(
            f"cannot write {destination}: {type(error).__name__}"
        ) from error
    try:
        os.chmod(destination, SECRET_FILE_MODE)
    except (OSError, NotImplementedError):
        pass
    return destination


def file_is_owner_only(path: str | Path) -> bool:
    """Whether the file is readable by its owner alone, where that is a thing."""
    return not stat.S_IMODE(Path(path).stat().st_mode) & 0o077


def load_send_credentials(
    configuration: GmailOAuthConfiguration,
    *,
    dependencies: Any | None = None,
) -> Any:
    """Load, validate and if necessary refresh the send-only authorization.

    This never opens a browser and never starts a consent flow: delivery is a
    command that either already holds an authorization or fails. A missing,
    corrupt, revoked, unrefreshable or over-scoped token is an error *here*,
    which is the whole point of calling it before any digest is claimed — an
    OAuth problem must never be able to move a PENDING row to IN_FLIGHT.
    """
    if not isinstance(configuration, GmailOAuthConfiguration):
        raise GmailOAuthError("configuration must be a GmailOAuthConfiguration")
    resolved = dependencies if dependencies is not None else _google_dependencies()
    token_path = Path(configuration.token_path)
    if not token_path.is_file():
        raise GmailOAuthError(
            "no Gmail authorization is stored;"
            " run python -m services.gmail_digest.oauth_bootstrap first"
        )
    # Parsed here as well as by the SDK so a truncated or hand-edited file is
    # refused by this module's own sentence rather than by a library's.
    _read_json_document(token_path, "Gmail token file")
    try:
        credentials = resolved.Credentials.from_authorized_user_file(
            str(token_path), list(GMAIL_SEND_SCOPES)
        )
    except Exception as error:
        raise GmailOAuthError(
            f"stored Gmail authorization is unusable ({type(error).__name__})"
        ) from error
    if credentials is None:
        raise GmailOAuthError("stored Gmail authorization is unusable")
    validate_send_only_scope(credentials)
    if not getattr(credentials, "valid", False):
        if not getattr(credentials, "refresh_token", None):
            raise GmailOAuthError(
                "stored Gmail authorization has expired and cannot be refreshed;"
                " re-run the OAuth bootstrap"
            )
        try:
            credentials.refresh(resolved.Request())
        except Exception as error:
            # A revoked grant and an unreachable token endpoint both land here.
            # Neither is worth quoting Google's body for.
            raise GmailOAuthError(
                f"Gmail authorization could not be refreshed ({type(error).__name__})"
            ) from error
        validate_send_only_scope(credentials)
        if not getattr(credentials, "valid", False):
            raise GmailOAuthError("refreshed Gmail authorization is still not valid")
        try:
            write_token_document(token_path, credentials.to_json(), overwrite=True)
        except GmailOAuthError:
            # A refreshed access token that could not be written back is a
            # cache miss, not a failure: the grant is still good and the next
            # run refreshes again.
            pass
    return credentials


def build_gmail_service(credentials: Any, *, dependencies: Any | None = None) -> Any:
    """Build the Gmail client this phase uses, and validate it while doing so.

    ``static_discovery`` keeps the build offline: the client is assembled from
    the discovery document shipped with the library rather than fetched, so
    constructing it proves the configuration is usable without making the
    first network call a surprise.
    """
    resolved = dependencies if dependencies is not None else _google_dependencies()
    validate_send_only_scope(credentials)
    try:
        return resolved.build(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
            static_discovery=True,
        )
    except Exception as error:
        raise GmailOAuthError(
            f"Gmail client could not be built ({type(error).__name__})"
        ) from error
