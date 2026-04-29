# IDA – Persönliche KI-Assistentin

IDA ist ein Telegram-Bot mit lokalem KI-Modell (Ollama) und einem erweiterbaren Worker-System. Sie läuft vollständig auf deinem eigenen Rechner – keine Cloud, keine externen KI-Dienste außer Google APIs.

---

## Hardware-Voraussetzungen

| Komponente | Mindest | Empfohlen |
|---|---|---|
| RAM | 16 GB | **32 GB** |
| CPU | Quad-Core | **i7 / Ryzen 7** |
| GPU | nicht nötig | NVIDIA (optional, deutlich schneller) |
| Speicher | 10 GB frei | 20 GB frei |

Die voreingestellten Modelle (`llama3.2:3b` + `llama3.2:1b` + `moondream`) belegen zusammen ca. 5 GB RAM und laufen flüssig auf einem i7 ohne GPU.

---

## Schnellstart

### Voraussetzungen
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installiert
- Telegram-Konto

### 1. Telegram Bot erstellen
1. In Telegram: [@BotFather](https://t.me/BotFather) schreiben → `/newbot`
2. Token kopieren
3. Deine Nutzer-ID herausfinden: [@userinfobot](https://t.me/userinfobot)

### 2. Starten
```bash
cd IDA
docker-compose up -d
```

Beim ersten Start werden alle Modelle automatisch heruntergeladen (~4–6 GB, dauert einige Minuten je nach Internetverbindung).

### 3. Konfigurieren
Web-Interface öffnen: **http://localhost:8080**

- **Einstellungen** → Telegram-Token und Nutzer-ID eintragen → Speichern
- `docker-compose restart ida-bot`

### 4. Loslegen
Bot in Telegram anschreiben. IDA ist bereit.

---

## Was IDA kann

### Direkt im Chat
| Eingabe | Was passiert |
|---|---|
| Textnachricht | IDA antwortet |
| Sprachnachricht | Wird transkribiert (Whisper), dann beantwortet |
| Foto | IDA analysiert das Bild (moondream) |

### Google Kalender
- „Was habe ich diese Woche?"
- „Erstell morgen um 10 Uhr einen Termin: Zahnarzt"
- „Verschieb den Zahnarzt auf Donnerstag 14 Uhr"
- „Wann habe ich heute noch zwei Stunden frei?"

### Google Tasks
- „Zeig meine offenen Aufgaben"
- „Füge Aufgabe hinzu: Steuererklärung, fällig 31. Mai"
- „Markier die Steuererklärung als erledigt"

### Google Kontakte
- „Suche Max Mustermann"
- „Leg einen neuen Kontakt an: Anna Müller, anna@example.com, 01234 56789"
- „Ändere die E-Mail von Anna auf neu@example.com"

### Stundenplan (WebUntis)
- „Was habe ich morgen in der Schule?"
- „Zeig mir den Stundenplan für die ganze Woche"
- „Gibt es diese Woche Ausfälle?"
- Automatische Benachrichtigung wenn eine Stunde ausfällt

### Automatisierung
- „Erinnere mich jeden Montag um 8 Uhr an die Wochenplanung"
- „Schick mir jeden Freitag eine Zusammenfassung"
- `/jobs` – alle geplanten Aufgaben anzeigen
- `/deljob <id>` – geplante Aufgabe löschen

### HTTP-APIs
- „Ruf die OpenWeather-API auf für das Wetter in Berlin"
- „Mach einen GET-Request an https://api.example.com/data"

---

## Web-Interface (http://localhost:8080)

| Seite | Funktion |
|---|---|
| **Dashboard** | Status aller Komponenten auf einen Blick |
| **Einstellungen** | Telegram-Token, Modelle, Nachtzeit-Fenster |
| **Google Kalender** | OAuth-Anmeldung, credentials.json hochladen |
| **WebUntis** | Server, Schulname, Zugangsdaten, Prüfintervall |
| **Geplante Tasks** | Alle Jobs anzeigen und löschen |

---

## Google APIs einrichten

### 1. Google Cloud Console
1. [console.cloud.google.com](https://console.cloud.google.com) → Projekt erstellen
2. Folgende APIs aktivieren:
   - **Google Calendar API**
   - **Google Tasks API**
   - **People API** (für Kontakte)
3. „Anmeldedaten" → „OAuth-Client-ID" → Typ: **Desktop-App**
4. `credentials.json` herunterladen

### 2. Im Web-Interface
1. http://localhost:8080/google öffnen
2. `credentials.json` hochladen
3. „Mit Google verbinden" klicken → Google-Konto auswählen → Zugriff erlauben

### 3. WebUntis
1. http://localhost:8080/untis öffnen
2. Server und Schulname aus der WebUntis-URL ablesen:
   `https://`**`hepta`**`.webuntis.com/WebUntis/?school=`**`meine-schule`**
3. Zugangsdaten eintragen → Speichern → Verbindung testen

---

## Konfiguration (.env)

| Variable | Standard | Beschreibung |
|---|---|---|
| `TELEGRAM_TOKEN` | – | Bot-Token von @BotFather |
| `TELEGRAM_ALLOWED_USERS` | – | Erlaubte User-IDs, kommagetrennt (leer = alle) |
| `MAIN_MODEL` | `llama3.2:3b` | IDA-Hauptmodell |
| `WORKER_MODEL` | `llama3.2:1b` | Worker-Modell (kann mehrfach gleichzeitig laufen) |
| `NIGHT_START_HOUR` | `2` | Beginn Nachtfenster für niedrig-prio Tasks |
| `NIGHT_END_HOUR` | `6` | Ende Nachtfenster |
| `WEB_PORT` | `8080` | Port des Web-Interfaces |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama-Adresse (im Docker-Netz) |

---

## Telegram-Befehle

| Befehl | Funktion |
|---|---|
| `/start` | Begrüßung |
| `/hilfe` | Alle Befehle anzeigen |
| `/jobs` | Geplante Aufgaben anzeigen |
| `/deljob <id>` | Geplante Aufgabe löschen |

---

## Datenspeicherung

Alle Daten liegen im Ordner `data/` auf deinem Rechner (per Docker-Volume gemountet):

| Datei | Inhalt |
|---|---|
| `conversation_history.json` | Gesprächsverlauf pro Nutzer |
| `jobs.json` | Geplante wiederkehrende Tasks |
| `google_credentials.json` | Google OAuth App-Zugangsdaten |
| `google_token.json` | Google Auth-Token (automatisch erneuert) |
| `untis_config.json` | WebUntis-Zugangsdaten und Einstellungen |
| `memory_*.json` | Persistentes Gedächtnis jedes Workers |

---

## Logs

```bash
docker-compose logs -f ida-bot
docker-compose logs -f ollama
```

## Neustart / Update

```bash
# Neu starten
docker-compose restart ida-bot

# Auf neue Version aktualisieren
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

Die Daten in `data/` bleiben bei Updates erhalten.
