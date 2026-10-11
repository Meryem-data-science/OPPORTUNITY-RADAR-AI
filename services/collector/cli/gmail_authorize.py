"""Manually renew the local, read-only OAuth grant used for LinkedIn alerts.

This command performs consent only. It neither reads messages nor starts the
collector, and is never called by the unattended refresh process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from services.collector.gmail.client import (
    GMAIL_SCOPES,
    GmailAuthenticationError,
    GmailConfiguration,
    GmailConfigurationError,
    _google_dependencies,
    _validate_exact_gmail_scopes,
)


MAX_CLIENT_FILE_BYTES = 64 * 1024
AUTHORIZATION_TIMEOUT_SECONDS = 300
GOOGLE_AUTH_URIS = frozenset({
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
})
GOOGLE_TOKEN_URIS = frozenset({
    "https://oauth2.googleapis.com/token",
    "https://accounts.google.com/o/oauth2/token",
})


def _desktop_client(configuration: GmailConfiguration) -> dict:
    """Validate local client material without displaying any of its values."""
    client_path = configuration.client_secret_path.resolve()
    token_path = configuration.token_path.resolve()
    if client_path == token_path:
        raise GmailConfigurationError(
            "Le client OAuth et le jeton doivent etre deux fichiers distincts."
        )
    if not client_path.is_file():
        raise GmailConfigurationError(
            "Le fichier du client OAuth est absent. Voir docs/connexion-linkedin.md."
        )
    if client_path.stat().st_size > MAX_CLIENT_FILE_BYTES:
        raise GmailConfigurationError("Le fichier du client OAuth est trop volumineux.")
    try:
        payload = json.loads(client_path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise GmailConfigurationError("Le fichier du client OAuth n'est pas un JSON valide.") from error
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict) or "web" in payload:
        raise GmailConfigurationError(
            "Choisir un client OAuth 'Application de bureau', pas 'Application Web'. "
            "Aucun domaine achete n'est necessaire pour ce collecteur local."
        )
    if (
        installed.get("auth_uri") not in GOOGLE_AUTH_URIS
        or installed.get("token_uri") not in GOOGLE_TOKEN_URIS
        or not isinstance(installed.get("client_id"), str)
        or not installed["client_id"].strip()
        or not isinstance(installed.get("client_secret"), str)
        or not installed["client_secret"].strip()
    ):
        raise GmailConfigurationError(
            "Le client OAuth doit provenir du telechargement officiel Google."
        )
    return payload


def _store_token_atomically(token_path: Path, token_json: str) -> None:
    """Keep the previous token intact until the new grant has been validated."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=token_path.parent,
            prefix=".gmail-authorization-", suffix=".tmp", delete=False,
        ) as token_file:
            temporary_path = Path(token_file.name)
            os.chmod(temporary_path, 0o600)
            token_file.write(token_json)
            token_file.flush()
            os.fsync(token_file.fileno())
        os.replace(temporary_path, token_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def authorize(configuration: GmailConfiguration) -> None:
    """Request a fresh consent grant, independently of an expired old token."""
    payload = _desktop_client(configuration)
    dependencies = _google_dependencies()
    flow = dependencies.InstalledAppFlow.from_client_config(payload, GMAIL_SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1",
        bind_addr="127.0.0.1",
        port=0,
        timeout_seconds=AUTHORIZATION_TIMEOUT_SECONDS,
        authorization_prompt_message=(
            "Connectez-vous dans le navigateur Google qui vient de s'ouvrir. "
            "Si necessaire, ouvrez ce lien dans votre navigateur : {url}"
        ),
        success_message=(
            "Google a renvoye l'autorisation. Revenez a la fenetre du lanceur "
            "pour connaitre le resultat de son enregistrement."
        ),
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
    )
    if not credentials or not credentials.valid or not credentials.refresh_token:
        raise GmailAuthenticationError(
            "Google n'a pas fourni une autorisation renouvelable pour la collecte automatique."
        )
    _validate_exact_gmail_scopes(credentials)
    _store_token_atomically(configuration.token_path, credentials.to_json())


def main() -> int:
    print("Connexion des alertes LinkedIn - lecture seule de Gmail.")
    print("Ce lanceur n'envoie aucun email et ne lance pas la collecte.")
    try:
        configuration = GmailConfiguration.from_environment()
        authorize(configuration)
    except GmailConfigurationError as error:
        # Only our fixed configuration messages are shown, never file contents.
        if str(error).startswith("GMAIL_"):
            print(
                "La configuration Gmail est incomplete. Voir docs/connexion-linkedin.md.",
                file=sys.stderr,
            )
        else:
            print(str(error), file=sys.stderr)
        return 2
    except ImportError:
        print("Les dependances Google sont absentes de .venv. Voir docs/connexion-linkedin.md.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Connexion interrompue. Le lanceur n'a pas remplace le jeton existant.", file=sys.stderr)
        return 1
    except Exception as error:
        # OAuth/provider errors can contain sensitive data; expose their type only.
        print(
            f"Connexion non terminee ({type(error).__name__}). "
            "Le lanceur n'a pas remplace le jeton existant. "
            "Voir docs/connexion-linkedin.md.",
            file=sys.stderr,
        )
        return 1
    print("Autorisation Gmail en lecture seule enregistree localement.")
    print("L'agent pourra lire les alertes LinkedIn au prochain cycle automatique.")
    print("Cette autorisation ne garantit pas que des alertes LinkedIn sont deja presentes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
