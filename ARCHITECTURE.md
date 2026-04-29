# IDA – Architektur & Erweiterungsguide

Dieses Dokument erklärt den aktuellen Aufbau des Systems, alle Design-Entscheidungen und wie neue Worker oder Funktionen korrekt hinzugefügt werden. Es richtet sich an Entwickler und an KI-Assistenten, die dieses Projekt weiterentwickeln sollen.

---

## Systemüberblick

```
Nutzer
  │
  │  Telegram (Text / Sprache / Bild)
  ▼
bot/handler.py  ──────────────────────────────────────────────
  │ AgentMessage(content, metadata)                           │
  ▼                                                           │
agents/orchestrator.py  (IDA – Hauptgehirn)           scheduler/task_scheduler.py
  │                                                           │
  │  Ollama llama3.2:3b                                       │  APScheduler
  │  Entscheidet: direkt antworten / DELEGATE / CHAIN         │  wiederkehrende + einmalige Jobs
  │                                                           │
  ├── DELEGATE:calendar_worker:...                            │
  ├── DELEGATE:tasks_worker:...                               │
  ├── DELEGATE:contacts_worker:...                            │
  ├── DELEGATE:untis_worker:...                               │
  ├── DELEGATE:api_worker:...                                 │
  ├── DELEGATE:vision_worker:...                              │
  └── CHAIN:worker1,worker2:...                               │
          │                                                   │
          ▼                                                   │
  agents/workers/*.py  (Spezialisten)                         │
    jeder Worker:                                             │
    - self.memory (WorkerMemory, persistent)                  │
    - self.client (Ollama async)                              │
    - process(AgentMessage) → AgentResponse                   │
          │                                                   │
          ▼                                                   │
  Externe Dienste:                                            │
    Google Calendar API                                       │
    Google Tasks API                                          │
    People API (Kontakte)                                     │
    WebUntis JSON-RPC API                                     │
    HTTP (beliebige REST-APIs)                                │

web/server.py  (FastAPI, Port 8080)
  Konfiguration via Browser – unabhängig vom Bot-Betrieb
```

---

## Verzeichnisstruktur

```
IDA/
├── main.py                    Einstiegspunkt: startet alle Komponenten
├── config.py                  Alle Einstellungen aus .env
├── docker-compose.yml         Ollama + IDA-Bot Container
├── Dockerfile
├── requirements.txt
├── setup_google_auth.py       Einmalige Google-Auth (lokale Alternative zum Web-UI)
│
├── agents/
│   ├── base.py                BaseAgent, AgentMessage, AgentResponse
│   ├── worker_memory.py       WorkerMemory – persistenter Key-Value-Speicher pro Worker
│   ├── context.py             SharedContext – Datenübergabe zwischen Workern pro Anfrage
│   ├── orchestrator.py        IDA – Hauptgehirn, routet Anfragen zu Workern
│   └── workers/
│       ├── _google_base.py    Gemeinsame Google Auth + Scopes (HIER neue Google-Scopes eintragen)
│       ├── api_worker.py      HTTP-Requests an externe APIs
│       ├── calendar_worker.py Google Kalender
│       ├── tasks_worker.py    Google Tasks
│       ├── contacts_worker.py Google Kontakte (People API)
│       ├── untis_worker.py    WebUntis Stundenplan
│       └── vision_worker.py   Bildanalyse (moondream via Ollama)
│
├── bot/
│   └── handler.py             Telegram: Text, Sprache (Whisper), Fotos, Befehle
│
├── scheduler/
│   └── task_scheduler.py      APScheduler-Wrapper, Prioritäten, Nachtzeit-Fenster
│
├── web/
│   ├── server.py              FastAPI Web-Interface
│   └── templates/             Jinja2 HTML-Templates (Bootstrap 5, Dark Theme)
│
└── data/                      Laufzeitdaten (Docker-Volume, bleibt bei Updates erhalten)
    ├── conversation_history.json
    ├── jobs.json
    ├── google_credentials.json
    ├── google_token.json
    ├── untis_config.json
    └── memory_<worker_name>.json   (eines pro Worker)
```

---

## Datenfluss einer Nutzeranfrage

```
1. Nutzer schickt Telegram-Nachricht

2. bot/handler.py
   - Erlaubnis prüfen (TELEGRAM_ALLOWED_USERS)
   - Bei Sprache: faster-whisper transkribiert → Text
   - Bei Bild: Base64 in metadata packen, has_image=True setzen
   - AgentMessage(content=text, metadata={user_id, chat_id, username, ...}) erstellen
   - orchestrator.process(message) aufrufen

3. agents/orchestrator.py
   - Hat das Bild-Flag? → direkt an vision_worker delegieren
   - Konversationshistorie für user_id laden (aus self.conversation_history)
   - SharedContext für user_id laden (aus self.context_store)
   - System-Prompt mit Workerliste + SharedContext-Zusammenfassung befüllen
   - Ollama (MAIN_MODEL) mit voller Konversationshistorie aufrufen
   - Antwort auswerten:
     * DELEGATE:<name>:<aufgabe>  → einen Worker aufrufen
     * CHAIN:<w1>,<w2>:<aufgabe>  → Worker hintereinander aufrufen
     * SCHEDULE:<cron>|<id>|<desc>\n<text>  → Job erstellen + Text zurückgeben
     * Alles andere → direkt als Antwort zurückgeben
   - Nach Delegation: Ergebnis zusammenfassen (zweiter Ollama-Call)
   - Antwort in Konversationshistorie schreiben + auf Disk speichern

4. agents/workers/*.py (falls delegiert)
   - AgentMessage mit Aufgabenbeschreibung + SharedContext empfangen
   - self.memory.get(...) für Worker-spezifische persistente Daten
   - Ollama (WORKER_MODEL) für Intent-Parsing: natürliche Sprache → JSON-Aktion
   - Externe API aufrufen (Google, WebUntis, HTTP, ...)
   - Ergebnis in SharedContext schreiben (ctx.set("key", value, worker=self.name))
   - AgentResponse(content=ergebnis) zurückgeben

5. Orchestrator fasst das Worker-Ergebnis zusammen und antwortet
```

---

## Worker-System im Detail

### BaseAgent (agents/base.py)

Jeder Worker erbt von `BaseAgent`. Pflichtfelder:

```python
class MeinWorker(BaseAgent):
    name = "mein_worker"          # eindeutiger Bezeichner – IDA referenziert ihn so
    description = "Kurzbeschreibung was dieser Worker macht"  # wird IDA im Prompt gezeigt

    async def process(self, message: AgentMessage) -> AgentResponse:
        ...
```

`BaseAgent.__init__()` stellt automatisch bereit:
- `self.client` – Ollama AsyncClient
- `self.model` – WORKER_MODEL aus config (überschreibbar)
- `self.memory` – WorkerMemory (persistent, pro Worker)
- `self._chat(messages, system)` – Ollama-Chat-Methode

### WorkerMemory (agents/worker_memory.py)

Persistenter Key-Value-Speicher. Datei: `data/memory_{worker.name}.json`.

```python
# Lesen
cached_ids = self.memory.get("event_cache", {})

# Schreiben (wird sofort auf Disk geschrieben)
self.memory.set("event_cache", {"abc123": {"title": "Meeting"}})

# Löschen
self.memory.delete("event_cache")
```

Wofür: IDs von zuletzt abgerufenen Ressourcen cachen (Kalender-Events, Tasks, Kontakte, Stundenplan-Stand), damit Folgeoperationen ohne neue Abfrage funktionieren.

### SharedContext (agents/context.py)

Temporärer Datenkanal zwischen Workern *innerhalb einer Anfrage*. Wird nicht persistiert.

```python
# In einem Worker Ergebnis hinterlegen:
ctx = message.metadata.get("shared_context")
if ctx:
    ctx.set("freie_zeiten", result_text, worker=self.name)

# Orchestrator zeigt SharedContext-Inhalt im System-Prompt → IDA weiß was bereits ermittelt wurde
```

Wofür: CHAIN-Aufrufe, bei denen Worker 1 etwas ermittelt und Worker 2 darauf aufbaut.

---

## Einen neuen Worker hinzufügen

### Schritt 1 – Datei erstellen

```python
# agents/workers/mein_worker.py

from agents.base import BaseAgent, AgentMessage, AgentResponse
from agents.workers._google_base import _parse_json_response  # optional, für JSON-Parsing

SYSTEM_PROMPT = """Du bist ein Spezialist für X. Heute ist {now}.
Antworte NUR mit einem JSON-Objekt.

Aktion A:
{{"action": "a", "params": {{"key": "value"}}}}

Aktion B:
{{"action": "b", "params": {{...}}}}
{context}"""

class MeinWorker(BaseAgent):
    name = "mein_worker"
    description = "Kurzbeschreibung – wird IDA als Delegation-Option angezeigt"

    def __init__(self):
        super().__init__()
        # optionaler eigener Service-Aufbau

    async def process(self, message: AgentMessage) -> AgentResponse:
        # 1. Worker-Gedächtnis für Kontext aufbauen
        cache = self.memory.get("cache", {})
        context = f"\nBekannte IDs: {cache}" if cache else ""

        # 2. Ollama: natürliche Sprache → JSON-Aktion
        system = SYSTEM_PROMPT.format(now="...", context=context)
        raw = await self._chat(
            messages=[{"role": "user", "content": message.content}],
            system=system,
        )

        # 3. JSON parsen
        parsed = _parse_json_response(raw)
        if not parsed:
            return AgentResponse(content=raw)

        # 4. Aktion ausführen
        action = parsed.get("action", "")
        params = parsed.get("params", {})

        if action == "a":
            result = self._do_a(params)
            self.memory.set("last_result", result)  # im Gedächtnis merken
            return AgentResponse(content=result)
        elif action == "b":
            return AgentResponse(content=self._do_b(params))

        return AgentResponse(content=f"Unbekannte Aktion: {action}", success=False)

    def _do_a(self, params: dict) -> str:
        ...

    def _do_b(self, params: dict) -> str:
        ...
```

### Schritt 2 – In main.py registrieren

```python
from agents.workers.mein_worker import MeinWorker

# In main():
orchestrator.register_worker(MeinWorker())
```

Das ist alles. IDA kennt den Worker ab sofort und kann ihn automatisch per `DELEGATE:mein_worker:...` beauftragen.

### Schritt 3 (optional) – Hintergrundcheck einrichten

Wenn der Worker periodisch prüfen soll (wie der Untis-Worker):

```python
# In main(), nach scheduler.start():
scheduler.scheduler.add_job(
    mein_worker.check_for_changes,
    "interval",
    minutes=30,
    id="mein_worker_check",
    replace_existing=True,
)
```

### Schritt 4 (optional) – Einstellungen im Web-Interface

1. Route in `web/server.py` hinzufügen (GET + POST `/mein-worker`)
2. Template `web/templates/mein_worker.html` erstellen
3. Link in `web/templates/base.html` in der Sidebar eintragen

---

## Einen neuen Google-Worker hinzufügen

Google-Worker verwenden `_google_base.py` für Auth. Ablauf:

### 1. Scope eintragen (agents/workers/_google_base.py)

```python
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/drive",      # ← neu eintragen
]
```

**Wichtig:** Danach muss der Nutzer das Google-Token einmal erneuern:
Web-Interface → Google Kalender → Verbindung trennen → neu verbinden.

### 2. API aktivieren

In der Google Cloud Console die neue API aktivieren (z.B. Google Drive API).

### 3. Worker schreiben

```python
from agents.workers._google_base import build_service, _parse_json_response

class DriveWorker(BaseAgent):
    name = "drive_worker"
    description = "Verwaltet Google Drive Dateien"

    def __init__(self):
        super().__init__()
        self._service = None

    def _svc(self):
        if not self._service:
            self._service = build_service("drive", "v3")
        return self._service
    ...
```

---

## Scheduling-System

### Prioritäten

```
Direkte Telegram-Nachricht  →  sofort verarbeitet (kein Warten)
Geplanter Task (priority="low")  →  nur im Nachtfenster (NIGHT_START_HOUR–NIGHT_END_HOUR)
Geplanter Task (priority="high")  →  sofort bei Fälligkeit
Einmaliger Job (one_time)  →  zum angegebenen Zeitpunkt
```

### IDA erstellt Jobs automatisch

Wenn ein Nutzer sagt „Erinnere mich jeden Montag um 9 Uhr an X", antwortet IDA mit:
```
SCHEDULE:0 9 * * 1|montag_erinnerung|X
Ich erinnere dich jeden Montag um 9 Uhr an X.
```

Der `TelegramHandler` parst die `SCHEDULE:`-Zeile und ruft `scheduler.add_recurring_job()` auf.

### Jobs überleben Neustarts

`data/jobs.json` wird bei jeder Änderung geschrieben. `scheduler.restore_jobs_after_start()` in `main.py` stellt alle Jobs nach Neustart wieder her.

---

## Persistenz – was wird wo gespeichert

| Was | Wo | Wann geschrieben |
|---|---|---|
| Gesprächshistorie | `data/conversation_history.json` | Nach jeder Antwort |
| Geplante Jobs | `data/jobs.json` | Bei jeder Job-Änderung |
| Worker-Gedächtnis | `data/memory_{name}.json` | Bei jedem `memory.set()` |
| Google Token | `data/google_token.json` | Bei OAuth + automatischer Erneuerung |
| Google Credentials | `data/google_credentials.json` | Manuell hochgeladen |
| Untis-Config | `data/untis_config.json` | Beim Speichern im Web-Interface |
| SharedContext | im RAM | Nur für Dauer einer Anfrage |

Alles in `data/` ist per Docker-Volume auf dem Host-Rechner – Update des Containers löscht nichts.

---

## Modell-Strategie

| Modell | Verwendung | RAM | Geschwindigkeit (i7 CPU) |
|---|---|---|---|
| `llama3.2:3b` | IDA Hauptgehirn (Orchestrator) | ~2 GB | ~5 tok/s |
| `llama3.2:1b` | Worker Intent-Parsing | ~0.6 GB | ~12 tok/s |
| `moondream` | Bildanalyse | ~1.7 GB | ~3 tok/s |
| `whisper-tiny` | Sprachtranskription | ~150 MB | sehr schnell |

Ollama hält die Modelle im RAM (`OLLAMA_KEEP_ALIVE=10m`). Mehrere Worker-Instanzen können parallel laufen, weil das Worker-Modell klein ist.

---

## Invarianten – was nicht gebrochen werden darf

1. **`BaseAgent.__init__()` immer aufrufen** mit `super().__init__()`. Sonst fehlen `self.memory`, `self.client`, `self.model`.

2. **`worker.name` ist der Routing-Schlüssel.** IDA referenziert Worker ausschließlich über `name`. Umbenennen bricht bestehende Delegation.

3. **`self.memory.set()` ist synchron und schreibt sofort auf Disk.** Nicht in Loops ohne Notwendigkeit aufrufen.

4. **Google-Scopes sind additiv.** Niemals einen Scope aus `GOOGLE_SCOPES` entfernen – das invalidiert das Token aller Nutzer.

5. **Konversationshistorie ist auf 30 Nachrichten begrenzt** (älteste werden gekürzt). Nie erhöhen ohne RAM-Auswirkung zu bedenken.

6. **Orchestrator-Antworten `DELEGATE:`, `CHAIN:`, `SCHEDULE:` sind Präfixe.** Sie werden im Handler ausgewertet bevor der Text an den Nutzer geht. Kein anderer Text darf mit diesen Präfixen beginnen.

7. **SharedContext lebt nur pro Anfrage** (RAM, nicht persistent). Nicht für langfristige Daten verwenden – dafür WorkerMemory.

---

## Geplante Erweiterungen / Vorschläge

Diese Features sind noch nicht implementiert, passen aber gut in die bestehende Architektur:

| Feature | Worker | Notizen |
|---|---|---|
| Google Drive | `drive_worker` | Dateien suchen, lesen, erstellen |
| E-Mail (Gmail) | `gmail_worker` | Lesen, verfassen, senden |
| Wetter | `weather_worker` | OpenWeather API, kein Google-Auth nötig |
| News | `news_worker` | RSS oder News-API |
| Smart Home | `home_worker` | Home Assistant REST-API |
| Spotify / Musik | `music_worker` | Spotify API |
| Erinnerungen mit Kontext | – | UntisWorker → CalendarWorker CHAIN: freie Zeit + Termin erstellen |

### Nächste sinnvolle Architektur-Upgrades

- **Worker-Konfiguration im Web-Interface:** Jeder Worker hat eine eigene Einstellungsseite (analog zu WebUntis). Vorlage: `web/templates/untis.html`.
- **Streaming-Antworten:** Ollama unterstützt Streaming. Für lange Antworten wäre `send_chat_action("typing")` mit partiellem Update sinnvoll.
- **Multi-User-Isolation:** Aktuell teilen alle Nutzer dieselben Worker-Instanzen (aber getrennte Konversationshistorien). Für vollständige Isolation: Worker-Pool pro Nutzer.
- **Webhook statt Polling:** `python-telegram-bot` unterstützt Webhooks. Bei stabilem Server performanter als Long-Polling.

---

## Wie eine KI dieses Projekt korrekt erweitern soll

Wenn du (als KI) dieses Projekt weiterentwickelst, beachte:

1. **Lies zuerst `agents/base.py` und einen bestehenden Worker** (z.B. `tasks_worker.py`). Das ist das kanonische Muster.

2. **Für neue Worker:** Muster aus `tasks_worker.py` kopieren, `name`, `description`, `SYSTEM_PROMPT` und die Aktionen anpassen. In `main.py` registrieren. Fertig.

3. **Für neue Google-Dienste:** Scope in `_google_base.py` eintragen, API in Google Cloud aktivieren, Worker wie `tasks_worker.py` bauen.

4. **Nichts umbenennen** was in `DELEGATE:`-Befehlen vorkommt (Worker `name`-Felder).

5. **Kontext-Weitergabe:** Wenn ein Worker Daten für einen anderen bereitstellt, `SharedContext.set()` verwenden, nicht neue Felder in `AgentMessage.metadata` erfinden.

6. **Persistenz via WorkerMemory**, nicht via globale Variablen oder Dateien außerhalb von `data/`.

7. **Web-Interface-Änderungen:** Neue Routen in `web/server.py`, neue Templates analog zu bestehenden. Sidebar-Link in `web/templates/base.html`.
