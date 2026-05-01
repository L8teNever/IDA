"""
Gemeinsame Google API Hilfsfunktionen für alle Google-Worker.

Scopes werden hier zentral verwaltet. Wenn ein neuer Google-Worker hinzukommt:
1. Scope hier in GOOGLE_SCOPES eintragen
2. Nutzer muss sich einmalig neu anmelden (Disconnect + Connect im Web-Interface)
"""

import logging
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts",
]

TOKEN_FILE = os.path.join(config.DATA_DIR, "google_token.json")
CREDENTIALS_FILE = os.path.join(config.DATA_DIR, "google_credentials.json")

_NOT_SETUP_MSG = (
    "Google ist nicht eingerichtet oder die Berechtigungen sind unvollständig.\n"
    f"Bitte im Web-Interface anmelden: http://localhost:{config.WEB_PORT}/google\n"
    "(Falls bereits verbunden: Trennen → neu verbinden, um alle Berechtigungen zu erteilen.)"
)


def get_credentials() -> Credentials:
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(_NOT_SETUP_MSG)
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
                logger.info("Google Token erneuert")
            except Exception as e:
                raise RuntimeError(f"Google Token konnte nicht erneuert werden: {e}\n{_NOT_SETUP_MSG}")
        else:
            raise RuntimeError(_NOT_SETUP_MSG)
    return creds


def build_service(api: str, version: str):
    return build(api, version, credentials=get_credentials())


def _parse_json_response(raw: str) -> Optional[dict]:
    """Erstes vollständiges JSON-Objekt aus LLM-Antwort extrahieren."""
    import json
    try:
        return json.loads(raw.strip())
    except Exception:
        pass
    start = raw.find('{')
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(raw[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except Exception:
                    return None
    return None
