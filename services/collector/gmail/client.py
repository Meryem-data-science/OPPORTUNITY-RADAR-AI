"""Official Gmail API client with local user OAuth and MIME normalization."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
import json
import os
from pathlib import Path
from typing import Any, Mapping

from services.collector.models.gmail_message import GmailMessageCandidate


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)
MAX_MESSAGE_LIMIT = 100


def _google_dependencies() -> Any:
    """Load the required official SDK at the OAuth/API boundary."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    class Dependencies:
        pass

    dependencies = Dependencies()
    dependencies.GoogleAuthError = GoogleAuthError
    dependencies.Request = Request
    dependencies.Credentials = Credentials
    dependencies.InstalledAppFlow = InstalledAppFlow
    dependencies.build = build
    dependencies.HttpError = HttpError
    return dependencies


class GmailConfigurationError(ValueError):
    """Raised when local Gmail configuration is missing or unsafe."""


class GmailAuthenticationError(RuntimeError):
    """Raised when user OAuth authorization cannot be established."""


class GmailApiError(RuntimeError):
    """Raised when the Gmail API rejects a read operation."""


class GmailPayloadError(ValueError):
    """Raised when a Gmail payload cannot be safely normalized."""


@dataclass(frozen=True)
class GmailConfiguration:
    """Paths to local OAuth material. Values are deliberately absent from repr."""

    client_secret_path: Path = field(repr=False)
    token_path: Path = field(repr=False)

    @classmethod
    def from_environment(cls) -> GmailConfiguration:
        client = os.environ.get("GMAIL_OAUTH_CLIENT_SECRET_PATH", "").strip()
        token = os.environ.get("GMAIL_TOKEN_PATH", "").strip()
        if not client:
            raise GmailConfigurationError(
                "GMAIL_OAUTH_CLIENT_SECRET_PATH must reference a local OAuth client file"
            )
        if not token:
            raise GmailConfigurationError(
                "GMAIL_TOKEN_PATH must reference a local OAuth token file"
            )
        return cls(Path(client), Path(token))


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MESSAGE_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_MESSAGE_LIMIT}")


def _load_credentials(configuration: GmailConfiguration) -> Any:
    if not configuration.client_secret_path.is_file():
        raise GmailConfigurationError("configured Gmail OAuth client file does not exist")

    dependencies = _google_dependencies()
    credentials: Any = None
    try:
        if configuration.token_path.is_file():
            configuration.token_path.chmod(0o600)
            credentials = dependencies.Credentials.from_authorized_user_file(
                str(configuration.token_path)
            )
            serialized_scopes = frozenset(credentials.scopes or ())
            granted_scopes_value = getattr(credentials, "granted_scopes", None)
            granted_scopes = (
                frozenset(granted_scopes_value)
                if granted_scopes_value is not None
                else serialized_scopes
            )
            required_scopes = frozenset(GMAIL_SCOPES)
            if serialized_scopes != required_scopes or granted_scopes != required_scopes:
                raise GmailAuthenticationError(
                    "stored Gmail token must be replaced by a new authorization "
                    "granting only gmail.readonly"
                )

        if credentials and credentials.valid:
            return credentials
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(dependencies.Request())
        else:
            flow = dependencies.InstalledAppFlow.from_client_secrets_file(
                str(configuration.client_secret_path), GMAIL_SCOPES
            )
            credentials = flow.run_local_server(port=0)
        if not credentials or not credentials.valid:
            raise GmailAuthenticationError("Gmail OAuth did not produce valid credentials")

        configuration.token_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            configuration.token_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
            token_file.write(credentials.to_json())
        configuration.token_path.chmod(0o600)
        return credentials
    except GmailAuthenticationError:
        raise
    except (dependencies.GoogleAuthError, OSError, ValueError, json.JSONDecodeError) as error:
        raise GmailAuthenticationError(
            f"Gmail OAuth failed ({type(error).__name__})"
        ) from error


def _decode_body_data(data: str) -> bytes:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise GmailPayloadError("message contains invalid base64url body data") from error


def _part_charset(part: Mapping[str, Any]) -> str | None:
    """Read a MIME part's declared charset from case-insensitive headers."""
    raw_headers = part.get("headers", ())
    if not isinstance(raw_headers, list):
        return None
    for header in raw_headers:
        if not isinstance(header, Mapping):
            continue
        name, value = header.get("name"), header.get("value")
        if (
            isinstance(name, str)
            and name.lower() == "content-type"
            and isinstance(value, str)
        ):
            mime_header = Message()
            mime_header["Content-Type"] = value
            return mime_header.get_content_charset()
    return None


def _decode_part_body(part: Mapping[str, Any], data: str) -> str:
    raw_body = _decode_body_data(data)
    charset = _part_charset(part) or "utf-8"
    try:
        return raw_body.decode(charset, errors="replace")
    except LookupError:
        return raw_body.decode("utf-8", errors="replace")


def _extract_bodies(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    text_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: Mapping[str, Any]) -> None:
        body = part.get("body")
        filename = part.get("filename")
        if filename or (isinstance(body, Mapping) and body.get("attachmentId")):
            return
        mime_type = str(part.get("mimeType", "")).lower()
        if isinstance(body, Mapping) and isinstance(body.get("data"), str):
            decoded = _decode_part_body(part, body["data"])
            if mime_type == "text/plain":
                text_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)
        parts = part.get("parts", ())
        if isinstance(parts, list):
            for child in parts:
                if isinstance(child, Mapping):
                    visit(child)

    visit(payload)
    return ("\n".join(text_parts) or None, "\n".join(html_parts) or None)


def normalize_message(message: Mapping[str, Any]) -> GmailMessageCandidate:
    """Normalize a Gmail API full-format message without fetching attachments."""
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise GmailPayloadError("Gmail message is missing its id")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    headers: dict[str, str] = {}
    raw_headers = payload.get("headers", ())
    if isinstance(raw_headers, list):
        for header in raw_headers:
            if isinstance(header, Mapping):
                name, value = header.get("name"), header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    headers[name.lower()] = value
    body_text, body_html = _extract_bodies(payload)
    internal_date = message.get("internalDate")
    received_at: str | None = None
    if isinstance(internal_date, str):
        try:
            received_at = datetime.fromtimestamp(
                int(internal_date) / 1000, tz=timezone.utc
            ).isoformat()
        except (ValueError, OverflowError, OSError):
            received_at = None
    return GmailMessageCandidate(
        message_id=message_id,
        thread_id=message.get("threadId") if isinstance(message.get("threadId"), str) else None,
        sender=headers.get("from"),
        subject=headers.get("subject"),
        received_at=received_at,
        snippet=message.get("snippet") if isinstance(message.get("snippet"), str) else None,
        body_text=body_text,
        body_html=body_html,
    )


class GmailClient:
    """Bounded generic Gmail reader; it exposes no mutation operations."""

    def __init__(self, service: Any):
        self._service = service

    @classmethod
    def from_configuration(cls, configuration: GmailConfiguration) -> GmailClient:
        dependencies = _google_dependencies()
        credentials = _load_credentials(configuration)
        try:
            return cls(dependencies.build("gmail", "v1", credentials=credentials, cache_discovery=False))
        except (dependencies.GoogleAuthError, dependencies.HttpError, OSError) as error:
            raise GmailAuthenticationError(
                f"could not initialize Gmail API ({type(error).__name__})"
            ) from error

    def list_message_ids(self, query: str, limit: int) -> list[str]:
        dependencies = _google_dependencies()
        _validate_limit(limit)
        if not isinstance(query, str) or not query.strip():
            raise GmailConfigurationError("Gmail query must not be empty")
        ids: list[str] = []
        page_token: str | None = None
        try:
            while len(ids) < limit:
                request = self._service.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=min(limit - len(ids), MAX_MESSAGE_LIMIT),
                    pageToken=page_token,
                )
                response = request.execute()
                if not isinstance(response, Mapping):
                    raise GmailPayloadError("Gmail list response is not an object")
                for item in response.get("messages", ()):
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                        ids.append(item["id"])
                        if len(ids) == limit:
                            break
                token = response.get("nextPageToken")
                page_token = token if isinstance(token, str) and token else None
                if not page_token:
                    break
            return ids
        except GmailPayloadError:
            raise
        except dependencies.HttpError as error:
            raise GmailApiError(f"Gmail message search failed (HTTP {error.resp.status})") from error
        except (dependencies.GoogleAuthError, OSError) as error:
            raise GmailApiError(f"Gmail message search failed ({type(error).__name__})") from error

    def get_message(self, message_id: str) -> GmailMessageCandidate:
        dependencies = _google_dependencies()
        if not message_id:
            raise GmailPayloadError("message id must not be empty")
        try:
            response = self._service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
            if not isinstance(response, Mapping):
                raise GmailPayloadError("Gmail get response is not an object")
            return normalize_message(response)
        except GmailPayloadError:
            raise
        except dependencies.HttpError as error:
            raise GmailApiError(f"Gmail message retrieval failed (HTTP {error.resp.status})") from error
        except (dependencies.GoogleAuthError, OSError) as error:
            raise GmailApiError(f"Gmail message retrieval failed ({type(error).__name__})") from error

    def search(self, query: str, limit: int) -> list[GmailMessageCandidate]:
        return [self.get_message(message_id) for message_id in self.list_message_ids(query, limit)]
