"""Offline tests for generic read-only Gmail access."""

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from services.collector.gmail.client import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    GmailApiError,
    GmailAuthenticationError,
    GmailClient,
    GmailConfiguration,
    GmailConfigurationError,
    GmailPayloadError,
    _load_credentials,
    normalize_message,
)


class FakeGoogleAuthError(Exception):
    pass


class FakeHttpError(Exception):
    def __init__(self, status: int, detail: str = "upstream detail") -> None:
        super().__init__(detail)
        self.resp = SimpleNamespace(status=status)


def dependencies(credentials: Mock | None = None) -> SimpleNamespace:
    credentials_class = Mock()
    if credentials is not None:
        credentials_class.from_authorized_user_file.return_value = credentials
    return SimpleNamespace(
        Credentials=credentials_class,
        InstalledAppFlow=Mock(),
        Request=Mock,
        GoogleAuthError=FakeGoogleAuthError,
        HttpError=FakeHttpError,
        build=Mock(),
    )


def encoded(value: str, charset: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(value.encode(charset)).decode().rstrip("=")


def message(payload: dict, **values: object) -> dict:
    return {"id": "msg-1", "threadId": "thread-1", "payload": payload, **values}


def test_only_exact_readonly_scope_is_configured() -> None:
    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert GMAIL_READONLY_SCOPE == GMAIL_SCOPES[0]


def test_missing_client_credentials_is_configuration_error(tmp_path: Path) -> None:
    config = GmailConfiguration(tmp_path / "missing.json", tmp_path / "token.json")
    with pytest.raises(GmailConfigurationError, match="does not exist"):
        _load_credentials(config)


def test_existing_valid_token_is_reused(tmp_path: Path) -> None:
    client_file, token_file = tmp_path / "client.json", tmp_path / "token.json"
    client_file.write_text("{}")
    token_file.write_text("{}")
    token_file.chmod(0o644)
    credentials = Mock(
        valid=True,
        scopes=[GMAIL_READONLY_SCOPE],
        granted_scopes=[GMAIL_READONLY_SCOPE],
    )
    deps = dependencies(credentials)
    with patch("services.collector.gmail.client._google_dependencies", return_value=deps):
        assert _load_credentials(GmailConfiguration(client_file, token_file)) is credentials
    deps.InstalledAppFlow.from_client_secrets_file.assert_not_called()
    deps.Credentials.from_authorized_user_file.assert_called_once_with(str(token_file))
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_existing_token_permission_failure_is_authentication_error(
    tmp_path: Path,
) -> None:
    client_file, token_file = tmp_path / "client.json", tmp_path / "token.json"
    client_file.write_text("{}")
    token_file.write_text("{}")
    with patch(
        "services.collector.gmail.client._google_dependencies",
        return_value=dependencies(),
    ), patch.object(Path, "chmod", side_effect=PermissionError):
        with pytest.raises(GmailAuthenticationError, match="PermissionError"):
            _load_credentials(GmailConfiguration(client_file, token_file))


@pytest.mark.parametrize(
    "stored_scopes",
    [
        [GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify"],
        ["https://mail.google.com/"],
    ],
)
def test_existing_token_with_additional_or_broader_scope_is_rejected(
    tmp_path: Path, stored_scopes: list[str]
) -> None:
    client_file, token_file = tmp_path / "client.json", tmp_path / "token.json"
    client_file.write_text("{}")
    token_file.write_text("{}")
    credentials = Mock(valid=True, scopes=stored_scopes, granted_scopes=stored_scopes)
    with patch(
        "services.collector.gmail.client._google_dependencies",
        return_value=dependencies(credentials),
    ):
        with pytest.raises(GmailAuthenticationError, match="only gmail.readonly"):
            _load_credentials(GmailConfiguration(client_file, token_file))


def test_existing_token_with_additional_granted_scope_is_rejected(
    tmp_path: Path,
) -> None:
    client_file, token_file = tmp_path / "client.json", tmp_path / "token.json"
    client_file.write_text("{}")
    token_file.write_text("{}")
    credentials = Mock(
        valid=True,
        scopes=[GMAIL_READONLY_SCOPE],
        granted_scopes=[
            GMAIL_READONLY_SCOPE,
            "https://www.googleapis.com/auth/gmail.modify",
        ],
    )
    with patch(
        "services.collector.gmail.client._google_dependencies",
        return_value=dependencies(credentials),
    ):
        with pytest.raises(GmailAuthenticationError, match="only gmail.readonly"):
            _load_credentials(GmailConfiguration(client_file, token_file))


def test_expired_token_is_refreshed_and_saved(tmp_path: Path) -> None:
    client_file, token_file = tmp_path / "client.json", tmp_path / "token.json"
    client_file.write_text("{}")
    token_file.write_text("{}")
    credentials = Mock(
        valid=False,
        expired=True,
        refresh_token="fictitious-refresh-token",
        scopes=[GMAIL_READONLY_SCOPE],
        granted_scopes=[GMAIL_READONLY_SCOPE],
    )
    credentials.to_json.return_value = '{"token":"fictitious"}'

    def refresh(_request: object) -> None:
        credentials.valid = True

    credentials.refresh.side_effect = refresh
    with patch("services.collector.gmail.client._google_dependencies", return_value=dependencies(credentials)):
        assert _load_credentials(GmailConfiguration(client_file, token_file)) is credentials
    credentials.refresh.assert_called_once()
    assert token_file.read_text() == '{"token":"fictitious"}'
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_plain_html_nested_headers_and_attachment_extraction() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "sUbJeCt", "value": "Fictitious role"},
            {"name": "FROM", "value": "Alerts <jobs@example.test>"},
        ],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encoded("plain body")}},
                    {"mimeType": "text/html", "body": {"data": encoded("<p>html body</p>")}},
                ],
            },
            {
                "mimeType": "text/plain",
                "filename": "private.txt",
                "body": {"data": encoded("attachment secret")},
            },
            {"mimeType": "application/pdf", "body": {"attachmentId": "attachment-1"}},
        ],
    }
    result = normalize_message(message(payload, internalDate="1704067200000", snippet="short"))
    assert result.sender == "Alerts <jobs@example.test>"
    assert result.subject == "Fictitious role"
    assert result.body_text == "plain body"
    assert result.body_html == "<p>html body</p>"
    assert result.received_at == "2024-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("mime_type", "expected_text", "expected_html"),
    [("text/plain", "hello", None), ("text/html", None, "hello")],
)
def test_simple_body_types(mime_type: str, expected_text: str | None, expected_html: str | None) -> None:
    result = normalize_message(message({"mimeType": mime_type, "body": {"data": encoded("hello")}}))
    assert (result.body_text, result.body_html) == (expected_text, expected_html)


@pytest.mark.parametrize(
    ("charset", "value"),
    [
        ("utf-8", "Développeuse €"),
        ("iso-8859-1", "Développeuse à Montréal"),
        ("windows-1252", "Opportunity — Paris"),
    ],
)
def test_body_respects_declared_charset(charset: str, value: str) -> None:
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "cOnTeNt-TyPe", "value": f"text/plain; charset={charset}"}],
        "body": {"data": encoded(value, charset)},
    }
    assert normalize_message(message(payload)).body_text == value


def test_body_without_charset_uses_utf8_fallback() -> None:
    value = "Alerte ingénieur"
    payload = {"mimeType": "text/plain", "body": {"data": encoded(value)}}
    assert normalize_message(message(payload)).body_text == value


def test_unknown_charset_uses_controlled_utf8_fallback() -> None:
    value = "Alerte ingénieur"
    payload = {
        "mimeType": "text/plain",
        "headers": [
            {"name": "Content-Type", "value": "text/plain; charset=not-a-real-charset"}
        ],
        "body": {"data": encoded(value)},
    }
    assert normalize_message(message(payload)).body_text == value


def test_missing_body_and_fields_do_not_crash() -> None:
    result = normalize_message({"id": "msg-1"})
    assert result.body_text is None and result.body_html is None
    assert result.sender is None and result.subject is None


def test_invalid_base64url_is_payload_error() -> None:
    with pytest.raises(GmailPayloadError):
        normalize_message(message({"mimeType": "text/plain", "body": {"data": "%%%"}}))


def chained_service(responses: list[dict]) -> tuple[Mock, Mock]:
    service, messages_api = Mock(), Mock()
    service.users.return_value.messages.return_value = messages_api
    messages_api.list.side_effect = [SimpleNamespace(execute=Mock(return_value=item)) for item in responses]
    return service, messages_api


def test_listing_passes_query_limit_and_paginates() -> None:
    service, api = chained_service([
        {"messages": [{"id": "one"}], "nextPageToken": "next"},
        {"messages": [{"id": "two"}]},
    ])
    with patch("services.collector.gmail.client._google_dependencies", return_value=dependencies()):
        assert GmailClient(service).list_message_ids("label:Opportunity-Radar", 2) == ["one", "two"]
    assert api.list.call_args_list[0].kwargs == {
        "userId": "me", "q": "label:Opportunity-Radar", "maxResults": 2, "pageToken": None
    }
    assert api.list.call_args_list[1].kwargs["pageToken"] == "next"


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5])
def test_invalid_client_limits_are_rejected(limit: object) -> None:
    with pytest.raises(ValueError):
        with patch("services.collector.gmail.client._google_dependencies", return_value=dependencies()):
            GmailClient(Mock()).list_message_ids("query", limit)  # type: ignore[arg-type]


def test_api_http_error_is_translated_without_google_message() -> None:
    service, api = chained_service([])
    api.list.side_effect = FakeHttpError(403, "token-value")
    with patch("services.collector.gmail.client._google_dependencies", return_value=dependencies()):
        with pytest.raises(GmailApiError, match="HTTP 403") as caught:
            GmailClient(service).list_message_ids("query", 1)
    assert "token-value" not in str(caught.value)


def test_get_requests_full_message_and_normalizes() -> None:
    service, api = chained_service([])
    api.get.return_value.execute.return_value = message(
        {"mimeType": "text/plain", "body": {"data": encoded("body")}}
    )
    with patch("services.collector.gmail.client._google_dependencies", return_value=dependencies()):
        result = GmailClient(service).get_message("msg-1")
    assert result.body_text == "body"
    api.get.assert_called_once_with(userId="me", id="msg-1", format="full")
