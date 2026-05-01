"""
Google Calendar Worker - liest, erstellt, bearbeitet und löscht Kalendereinträge.
Zugangsdaten: data/google_credentials.json + data/google_token.json
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from agents.base import BaseAgent, AgentMessage, AgentResponse
from agents.workers._google_base import build_service, _parse_json_response

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("Europe/Berlin")

SYSTEM_PROMPT = """Heute: {now}. Antworte NUR mit einem einzigen JSON-Objekt, kein Text drumherum.

Termine heute: {{"action":"list_events","params":{{"days":1}}}}
Termine diese Woche: {{"action":"list_events","params":{{"days":7}}}}
Termin erstellen: {{"action":"create_event","params":{{"title":"...","start":"2025-04-29T10:00:00","end":"2025-04-29T11:00:00"}}}}
Termin bearbeiten: {{"action":"update_event","params":{{"event_id":"...","title":"...","start":"...","end":"..."}}}}
Termin löschen: {{"action":"delete_event","params":{{"event_id":"..."}}}}
Freie Zeit: {{"action":"find_free","params":{{"date":"2025-04-29","duration_minutes":60}}}}

Datum: YYYY-MM-DDTHH:MM:SS, Zeitzone Europe/Berlin.{context}"""


class CalendarWorker(BaseAgent):
    name = "calendar_worker"
    description = "Liest, erstellt, bearbeitet und löscht Google Kalender Termine"

    def __init__(self):
        super().__init__()
        self._service = None

    def _svc(self):
        if not self._service:
            self._service = build_service("calendar", "v3")
        return self._service

    async def process(self, message: AgentMessage) -> AgentResponse:
        try:
            self._svc()
        except RuntimeError as e:
            return AgentResponse(content=str(e), success=False)

        # Event-Cache aus WorkerMemory für Kontext
        event_cache: dict = self.memory.get("event_cache", {})
        context = ""
        if event_cache:
            lines = ["\nBekannte Ereignis-IDs:"]
            for eid, ev in list(event_cache.items())[-10:]:
                lines.append(f"- {eid}: {ev.get('summary','?')} ({ev.get('start_str','')})")
            context = "\n".join(lines)

        now_str = datetime.now(LOCAL_TZ).strftime("%A, %d.%m.%Y %H:%M Uhr")
        system = SYSTEM_PROMPT.format(now=now_str, context=context)
        raw = await self._chat(messages=[{"role": "user", "content": message.content}], system=system, num_predict=150)

        parsed = _parse_json_response(raw)
        if not parsed:
            return AgentResponse(content=raw)

        action = parsed.get("action", "")
        params = parsed.get("params", {})
        try:
            if action == "list_events":
                return AgentResponse(content=self._list_events(params))
            elif action == "create_event":
                return AgentResponse(content=self._create_event(params))
            elif action == "update_event":
                return AgentResponse(content=self._update_event(params))
            elif action == "delete_event":
                return AgentResponse(content=self._delete_event(params))
            elif action == "find_free":
                return AgentResponse(content=self._find_free(params))
            else:
                return AgentResponse(content=f"Unbekannte Aktion: {action}", success=False)
        except HttpError as e:
            return AgentResponse(content=f"Google Kalender Fehler: {e}", success=False)

    def _list_events(self, params: dict) -> str:
        days = params.get("days", 7)
        now = datetime.now(LOCAL_TZ)
        if days <= 1:
            until = now.replace(hour=23, minute=59, second=59, microsecond=0)
            header = "Termine heute noch:"
            empty_msg = "Heute stehen keine weiteren Termine an."
        else:
            until = now + timedelta(days=days)
            header = f"Termine der nächsten {days} Tage:"
            empty_msg = f"Keine Termine in den nächsten {days} Tagen."
        result = self._svc().events().list(
            calendarId="primary", timeMin=now.isoformat(), timeMax=until.isoformat(),
            maxResults=50, singleEvents=True, orderBy="startTime",
        ).execute()
        events = result.get("items", [])

        cache: dict = {}
        if not events:
            return empty_msg
        lines = [header + "\n"]
        for ev in events:
            start = ev["start"].get("dateTime", ev["start"].get("date", ""))
            start_str = _fmt_dt(start)
            title = ev.get("summary", "(Kein Titel)")
            loc = ev.get("location", "")
            cache[ev["id"]] = {"summary": title, "start_str": start_str}
            line = f"• {start_str}: {title}"
            if loc:
                line += f" ({loc})"
            lines.append(line)
        self.memory.set("event_cache", cache)
        return "\n".join(lines)

    def _create_event(self, params: dict) -> str:
        start_s, end_s = params.get("start", ""), params.get("end", "")
        if not start_s or not end_s:
            return "Fehler: Start- und Endzeit erforderlich."
        body = {
            "summary": params.get("title", "Neuer Termin"),
            "start": {"dateTime": _to_rfc3339(start_s), "timeZone": "Europe/Berlin"},
            "end": {"dateTime": _to_rfc3339(end_s), "timeZone": "Europe/Berlin"},
        }
        if params.get("description"):
            body["description"] = params["description"]
        if params.get("location"):
            body["location"] = params["location"]
        ev = self._svc().events().insert(calendarId="primary", body=body).execute()
        cache: dict = self.memory.get("event_cache", {})
        cache[ev["id"]] = {"summary": body["summary"], "start_str": _fmt_dt(start_s)}
        self.memory.set("event_cache", cache)
        return f"Termin erstellt: {body['summary']} am {_fmt_dt(start_s)}\nID: {ev['id']}"

    def _update_event(self, params: dict) -> str:
        eid = params.get("event_id", "")
        if not eid:
            return "Fehler: event_id fehlt. Bitte zuerst Termine anzeigen."
        ev = self._svc().events().get(calendarId="primary", eventId=eid).execute()
        if params.get("title"):
            ev["summary"] = params["title"]
        if params.get("description"):
            ev["description"] = params["description"]
        if params.get("location"):
            ev["location"] = params["location"]
        if params.get("start"):
            ev["start"] = {"dateTime": _to_rfc3339(params["start"]), "timeZone": "Europe/Berlin"}
        if params.get("end"):
            ev["end"] = {"dateTime": _to_rfc3339(params["end"]), "timeZone": "Europe/Berlin"}
        updated = self._svc().events().update(calendarId="primary", eventId=eid, body=ev).execute()
        return f"Termin aktualisiert: {updated.get('summary', eid)}"

    def _delete_event(self, params: dict) -> str:
        eid = params.get("event_id", "")
        if not eid:
            return "Fehler: event_id fehlt."
        cache: dict = self.memory.get("event_cache", {})
        name = cache.get(eid, {}).get("summary", eid)
        self._svc().events().delete(calendarId="primary", eventId=eid).execute()
        cache.pop(eid, None)
        self.memory.set("event_cache", cache)
        return f"Termin gelöscht: {name}"

    def _find_free(self, params: dict) -> str:
        date_s = params.get("date", datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
        duration = int(params.get("duration_minutes", 60))
        day_start = datetime.fromisoformat(f"{date_s}T08:00:00").replace(tzinfo=LOCAL_TZ)
        day_end = datetime.fromisoformat(f"{date_s}T20:00:00").replace(tzinfo=LOCAL_TZ)
        body = {"timeMin": day_start.isoformat(), "timeMax": day_end.isoformat(), "items": [{"id": "primary"}]}
        busy_slots = self._svc().freebusy().query(body=body).execute().get("calendars", {}).get("primary", {}).get("busy", [])
        free, cursor = [], day_start
        for b in busy_slots:
            bs = datetime.fromisoformat(b["start"]).astimezone(LOCAL_TZ)
            be = datetime.fromisoformat(b["end"]).astimezone(LOCAL_TZ)
            if (bs - cursor).total_seconds() >= duration * 60:
                free.append(f"• {cursor.strftime('%H:%M')} – {bs.strftime('%H:%M')}")
            cursor = max(cursor, be)
        if (day_end - cursor).total_seconds() >= duration * 60:
            free.append(f"• {cursor.strftime('%H:%M')} – {day_end.strftime('%H:%M')}")
        return (f"Freie Zeiten am {date_s} (mind. {duration} Min.):\n" + "\n".join(free)) if free else f"Keine freien Blöcke von {duration} Min. am {date_s}."


def _to_rfc3339(dt_str: str) -> str:
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.isoformat()


def _fmt_dt(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ).strftime("%a %d.%m.%Y %H:%M")
    except Exception:
        return dt_str
