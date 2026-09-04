"""Offline tests for send-only Gmail authorization and its bootstrap.

Nothing here touches Google. Every test supplies its own fake SDK objects
through the same dependency seam production uses, so the whole module runs on a
machine with no Google packages installed, no OAuth client file, no token, and
no network — which is also the guarantee that a broken credential is refused
before anything else happens rather than after a consent screen.
"""

import json
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.gmail_digest import oauth, oauth_bootstrap
from services.gmail_digest.oauth import (
    FORBIDDEN_SCOPES,
    GMAIL_SEND_SCOPE,
    GMAIL_SEND_SCOPES,
    GmailOAuthConfiguration,
    GmailOAuthError,
    load_send_credentials,
    validate_client_configuration,
    validate_send_only_scope,
    write_token_document,
)

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"
METADATA = "https://www.googleapis.com/auth/gmail.metadata"
FULL = "https://mail.google.com/"
TOKEN_JSON = json.dumps(
    {
        "token": "ya29.super-secret-access-token",
        "refresh_token": "1//super-secret-refresh-token",
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "GOCSPX-super-secret-client-secret",
        "scopes": [GMAIL_SEND_SCOPE],
    }
)


class FakeCredentials:
    """Just enough of google.oauth2.credentials.Credentials to be refused."""

    def __init__(self, *, scopes=GMAIL_SEND_SCOPES, valid=True, refresh_token="r"):
        self.scopes = list(scopes)
        self.valid = valid
        self.refresh_token = refresh_token
        self.refreshed = 0

    def refresh(self, request):
        self.refreshed += 1
        self.valid = True

    def to_json(self):
        return TOKEN_JSON


def client_document(**overrides):
    document = {
        "installed": {
            "client_id": "client.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    document.update(overrides)
    return document


def client_file(tmp_path, **overrides):
    path = tmp_path / "gmail-oauth-client.json"
    path.write_text(json.dumps(client_document(**overrides)), encoding="utf-8")
    return path


def dependencies(credentials=None, *, flow_credentials=None, service=object()):
    credentials_class = SimpleNamespace(
        from_authorized_user_file=lambda path, scopes: credentials
    )
    flow = SimpleNamespace(
        run_local_server=lambda port=0: flow_credentials,
        requested=None,
    )
    installed = SimpleNamespace(from_client_secrets_file=lambda path, scopes: flow)
    return SimpleNamespace(
        Credentials=credentials_class,
        InstalledAppFlow=installed,
        Request=lambda: object(),
        GoogleAuthError=RuntimeError,
        build=lambda *args, **kwargs: service,
    )


def configuration(tmp_path, *, token="gmail-token.json"):
    return GmailOAuthConfiguration.local(
        client_secret_path=client_file(tmp_path), token_path=tmp_path / token
    )


# --------------------------------------------------------------------------
# One scope, and no way to widen it
# --------------------------------------------------------------------------


def test_the_only_scope_this_phase_knows_is_gmail_send():
    assert GMAIL_SEND_SCOPES == (GMAIL_SEND_SCOPE,)
    assert GMAIL_SEND_SCOPE == "https://www.googleapis.com/auth/gmail.send"
    assert not set(GMAIL_SEND_SCOPES) & FORBIDDEN_SCOPES


@pytest.mark.parametrize("scope", [READONLY, MODIFY, METADATA, FULL])
def test_no_module_in_the_delivery_path_mentions_a_mailbox_scope(scope):
    delivery = Path("services/gmail_digest")
    sources = [
        path.read_text(encoding="utf-8")
        for path in delivery.glob("*.py")
        if path.name != "oauth.py"
    ]
    assert all(scope not in source for source in sources)


def test_the_bootstrap_flow_requests_gmail_send_and_nothing_else(tmp_path, monkeypatch):
    requested = []

    def from_client_secrets_file(path, scopes):
        requested.append(tuple(scopes))
        return SimpleNamespace(
            run_local_server=lambda port=0: FakeCredentials(),
        )

    resolved = dependencies()
    resolved.InstalledAppFlow = SimpleNamespace(
        from_client_secrets_file=from_client_secrets_file
    )
    monkeypatch.setattr(socket, "socket", _no_sockets)

    written = oauth_bootstrap.authorize_send_only(
        configuration(tmp_path), dependencies=resolved
    )

    assert requested == [(GMAIL_SEND_SCOPE,)]
    assert written.is_file()


@pytest.mark.parametrize(
    "granted",
    [
        (GMAIL_SEND_SCOPE, READONLY),
        (READONLY,),
        (GMAIL_SEND_SCOPE, MODIFY),
        (FULL,),
        (GMAIL_SEND_SCOPE, METADATA),
    ],
)
def test_a_credential_granting_more_than_send_is_refused(granted):
    with pytest.raises(GmailOAuthError):
        validate_send_only_scope(FakeCredentials(scopes=granted))


def test_a_credential_that_declares_no_scope_at_all_is_refused():
    with pytest.raises(GmailOAuthError):
        validate_send_only_scope(SimpleNamespace())


def test_every_scope_view_must_agree_that_it_is_send_only():
    credentials = FakeCredentials()
    credentials.granted_scopes = [GMAIL_SEND_SCOPE, READONLY]
    with pytest.raises(GmailOAuthError):
        validate_send_only_scope(credentials)


def test_an_over_scoped_credential_is_refused_before_the_token_is_written(tmp_path):
    target = configuration(tmp_path)
    resolved = dependencies()
    resolved.InstalledAppFlow = SimpleNamespace(
        from_client_secrets_file=lambda path, scopes: SimpleNamespace(
            run_local_server=lambda port=0: FakeCredentials(
                scopes=(GMAIL_SEND_SCOPE, READONLY)
            )
        )
    )

    with pytest.raises(GmailOAuthError):
        oauth_bootstrap.authorize_send_only(target, dependencies=resolved)

    assert not Path(target.token_path).exists()


# --------------------------------------------------------------------------
# The client configuration cannot escalate the consent screen
# --------------------------------------------------------------------------


def test_a_desktop_client_configuration_is_accepted(tmp_path):
    assert validate_client_configuration(client_file(tmp_path))


def test_a_web_client_configuration_is_refused(tmp_path):
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"web": client_document()["installed"]}), "utf-8")
    with pytest.raises(GmailOAuthError, match="Desktop"):
        validate_client_configuration(path)


def test_a_client_configuration_naming_another_scope_is_refused(tmp_path):
    path = client_file(tmp_path, scopes=[GMAIL_SEND_SCOPE, READONLY])
    with pytest.raises(GmailOAuthError, match="beyond gmail.send"):
        validate_client_configuration(path)


def test_a_scope_hidden_deeper_in_the_client_configuration_is_still_found(tmp_path):
    path = client_file(tmp_path, extras={"nested": {"granted": f"{FULL} openid"}})
    with pytest.raises(GmailOAuthError, match="beyond gmail.send"):
        validate_client_configuration(path)


def test_a_malformed_client_configuration_is_refused_without_quoting_it(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"installed": {"client_id": "abc", ', encoding="utf-8")
    with pytest.raises(GmailOAuthError) as error:
        validate_client_configuration(path)
    assert "abc" not in str(error.value)


# --------------------------------------------------------------------------
# The token file: atomic, owner-only, and never replaced by accident
# --------------------------------------------------------------------------


def test_the_token_write_is_atomic_and_leaves_no_partial_file(tmp_path):
    path = tmp_path / "nested" / "gmail-token.json"
    written = write_token_document(path, TOKEN_JSON)
    assert json.loads(written.read_text(encoding="utf-8"))["scopes"] == [
        GMAIL_SEND_SCOPE
    ]
    assert list(path.parent.iterdir()) == [written]


def test_a_failed_token_write_leaves_neither_a_reservation_nor_a_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "gmail-token.json"

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(GmailOAuthError):
        write_token_document(path, TOKEN_JSON)

    assert list(tmp_path.iterdir()) == []


def test_an_existing_authorization_is_never_replaced_without_force(tmp_path):
    path = tmp_path / "gmail-token.json"
    write_token_document(path, TOKEN_JSON)

    with pytest.raises(GmailOAuthError, match="--force"):
        write_token_document(path, '{"token":"other"}')

    assert json.loads(path.read_text(encoding="utf-8"))["scopes"] == [GMAIL_SEND_SCOPE]


def test_force_replaces_the_authorization_in_place(tmp_path):
    path = tmp_path / "gmail-token.json"
    write_token_document(path, TOKEN_JSON)

    write_token_document(path, '{"token":"replaced"}', overwrite=True)

    assert json.loads(path.read_text(encoding="utf-8")) == {"token": "replaced"}


def test_the_token_file_is_owner_readable_only(tmp_path):
    path = write_token_document(tmp_path / "gmail-token.json", TOKEN_JSON)
    assert not stat.S_IMODE(path.stat().st_mode) & 0o077
    assert oauth.file_is_owner_only(path)


def test_the_bootstrap_refuses_a_second_authorization_without_force(tmp_path):
    target = configuration(tmp_path)
    write_token_document(target.token_path, TOKEN_JSON)
    resolved = dependencies()
    resolved.InstalledAppFlow = SimpleNamespace(
        from_client_secrets_file=lambda path, scopes: SimpleNamespace(
            run_local_server=lambda port=0: FakeCredentials()
        )
    )

    with pytest.raises(GmailOAuthError, match="--force"):
        oauth_bootstrap.authorize_send_only(target, dependencies=resolved)


def test_an_authorization_without_a_refresh_token_is_refused(tmp_path):
    target = configuration(tmp_path)
    resolved = dependencies()
    resolved.InstalledAppFlow = SimpleNamespace(
        from_client_secrets_file=lambda path, scopes: SimpleNamespace(
            run_local_server=lambda port=0: FakeCredentials(refresh_token=None)
        )
    )

    with pytest.raises(GmailOAuthError, match="refresh token"):
        oauth_bootstrap.authorize_send_only(target, dependencies=resolved)

    assert not Path(target.token_path).exists()


# --------------------------------------------------------------------------
# Nothing secret is ever rendered
# --------------------------------------------------------------------------


def test_the_configuration_never_renders_its_paths_or_contents(tmp_path):
    target = configuration(tmp_path)
    rendered = f"{target!r} {target!s}"
    assert "gmail-token.json" not in rendered
    assert "gmail-oauth-client.json" not in rendered


def test_a_corrupt_token_is_reported_without_quoting_its_contents(tmp_path):
    target = configuration(tmp_path)
    Path(target.token_path).write_text(
        '{"refresh_token": "1//super-secret", ', encoding="utf-8"
    )

    with pytest.raises(GmailOAuthError) as error:
        load_send_credentials(target, dependencies=dependencies())

    assert "super-secret" not in str(error.value)


def test_the_bootstrap_prints_the_scope_and_the_path_and_no_credential(
    tmp_path, capsys, monkeypatch
):
    target = configuration(tmp_path)
    resolved = dependencies()
    resolved.InstalledAppFlow = SimpleNamespace(
        from_client_secrets_file=lambda path, scopes: SimpleNamespace(
            run_local_server=lambda port=0: FakeCredentials()
        )
    )
    monkeypatch.setattr(oauth_bootstrap, "_google_dependencies", lambda: resolved)

    code = oauth_bootstrap.main(
        [
            "--client-secret",
            str(target.client_secret_path),
            "--token",
            str(target.token_path),
        ]
    )

    printed = capsys.readouterr()
    assert code == 0
    assert GMAIL_SEND_SCOPE in printed.out
    assert "ya29." not in printed.out and "1//" not in printed.out
    assert "GOCSPX" not in printed.out and "GOCSPX" not in printed.err


# --------------------------------------------------------------------------
# Loading: every failure happens before any delivery work
# --------------------------------------------------------------------------


def test_a_missing_token_is_an_error_not_a_consent_flow(tmp_path):
    with pytest.raises(GmailOAuthError, match="oauth_bootstrap"):
        load_send_credentials(configuration(tmp_path), dependencies=dependencies())


def test_loading_never_opens_a_browser_or_a_flow(tmp_path):
    target = configuration(tmp_path)
    write_token_document(target.token_path, TOKEN_JSON)
    resolved = dependencies(FakeCredentials())

    def refuse(*args, **kwargs):
        raise AssertionError("delivery must never start a consent flow")

    resolved.InstalledAppFlow = SimpleNamespace(from_client_secrets_file=refuse)

    assert load_send_credentials(target, dependencies=resolved).valid


def test_an_expired_credential_is_refreshed_and_written_back(tmp_path):
    target = configuration(tmp_path)
    write_token_document(target.token_path, TOKEN_JSON)
    credentials = FakeCredentials(valid=False)

    loaded = load_send_credentials(target, dependencies=dependencies(credentials))

    assert loaded.refreshed == 1
    assert json.loads(Path(target.token_path).read_text(encoding="utf-8"))[
        "scopes"
    ] == [GMAIL_SEND_SCOPE]


def test_an_unrefreshable_credential_is_refused(tmp_path):
    target = configuration(tmp_path)
    write_token_document(target.token_path, TOKEN_JSON)
    credentials = FakeCredentials(valid=False, refresh_token=None)

    with pytest.raises(GmailOAuthError, match="expired"):
        load_send_credentials(target, dependencies=dependencies(credentials))


def test_a_revoked_grant_is_refused_without_quoting_google(tmp_path):
    target = configuration(tmp_path)
    write_token_document(target.token_path, TOKEN_JSON)
    credentials = FakeCredentials(valid=False)

    def revoked(request):
        raise RuntimeError('{"error": "invalid_grant", "token": "ya29.secret"}')

    credentials.refresh = revoked

    with pytest.raises(GmailOAuthError) as error:
        load_send_credentials(target, dependencies=dependencies(credentials))

    assert "ya29" not in str(error.value)


def test_a_stored_credential_that_grants_read_access_is_refused_on_load(tmp_path):
    target = configuration(tmp_path)
    write_token_document(target.token_path, TOKEN_JSON)
    credentials = FakeCredentials(scopes=(GMAIL_SEND_SCOPE, READONLY))

    with pytest.raises(GmailOAuthError):
        load_send_credentials(target, dependencies=dependencies(credentials))


def _no_sockets(*args, **kwargs):
    raise AssertionError("this module must never open a socket")
