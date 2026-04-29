"""
Einmalige Einrichtung der Google API Zugangsdaten (Kalender, Tasks, Kontakte).

Anleitung:
1. Gehe zu: https://console.cloud.google.com/
2. Projekt erstellen → folgende APIs aktivieren:
   - Google Calendar API
   - Google Tasks API
   - People API (für Kontakte)
3. "APIs & Dienste" → "Anmeldedaten" → "OAuth-Client-ID" → Typ: Desktop-App
4. credentials.json herunterladen → als data/google_credentials.json speichern
5. Dieses Script ausführen: python setup_google_auth.py

Das Token wird in data/google_token.json gespeichert und automatisch erneuert.
Alternativ: Im Web-Interface http://localhost:8080/google anmelden.
"""

import os
import sys

DATA_DIR = os.getenv("DATA_DIR", "./data")
CREDENTIALS_FILE = os.path.join(DATA_DIR, "google_credentials.json")
TOKEN_FILE = os.path.join(DATA_DIR, "google_token.json")


def main():
    try:
        from agents.workers._google_base import GOOGLE_SCOPES
    except ImportError:
        GOOGLE_SCOPES = [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/tasks",
            "https://www.googleapis.com/auth/contacts",
        ]

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("Fehler: Google-Bibliotheken nicht installiert.")
        print("Bitte ausführen: pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"FEHLER: {CREDENTIALS_FILE} nicht gefunden!")
        print()
        print("Anleitung:")
        print("1. https://console.cloud.google.com/ öffnen")
        print("2. Projekt → folgende APIs aktivieren:")
        print("   - Google Calendar API")
        print("   - Google Tasks API")
        print("   - People API")
        print("3. Anmeldedaten → OAuth-Client-ID (Desktop-App) → credentials.json herunterladen")
        print(f"4. Als '{CREDENTIALS_FILE}' speichern")
        print("5. Dieses Script erneut ausführen")
        sys.exit(1)

    # Prüfen ob bestehender Token alle Scopes hat
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, GOOGLE_SCOPES)
            if creds.valid:
                print("Token ist gültig mit allen Berechtigungen.")
                print(f"Scopes: {GOOGLE_SCOPES}")
                return
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as f:
                    f.write(creds.to_json())
                print("Token erneuert.")
                return
        except Exception as e:
            print(f"Bestehender Token ungültig ({e}), erstelle neuen...")

    print(f"Fordere Berechtigungen an für:")
    for s in GOOGLE_SCOPES:
        print(f"  - {s.split('/')[-1]}")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\nErfolgreich! Token gespeichert: {TOKEN_FILE}")
    print("IDA hat jetzt Zugriff auf: Kalender, Tasks und Kontakte.")


if __name__ == "__main__":
    main()
