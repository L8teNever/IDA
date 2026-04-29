"""
WebUntis Worker - Stundenplan abrufen, Ausfälle erkennen und benachrichtigen.

API: WebUntis JSON-RPC 2.0
Docs: https://untis-sr.ch/wp-content/uploads/2019/11/2018-09-20-WebUntis_JSON_RPC_API.pdf

Config: data/untis_config.json
Gedächtnis: data/memory_untis_worker.json (letzter bekannter Stundenplan)
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Optional
from zoneinfo import ZoneInfo

import httpx

from agents.base import BaseAgent, AgentMessage, AgentResponse
import config

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo("Europe/Berlin")
UNTIS_CONFIG_FILE = config.UNTIS_CONFIG_FILE

LESSON_CODES = {
    "cancelled": "Ausfall",
    "irregular": "Vertretung / Änderung",
    "": "Normal",
}

SYSTEM_PROMPT = """Du bist ein Stundenplan-Spezialist. Heute ist {now}.

Verfügbare Aktionen – antworte NUR mit JSON:

Stundenplan anzeigen:
{{"action": "get_timetable", "params": {{"date": "2025-04-29"}}}}

Woche anzeigen:
{{"action": "get_week", "params": {{"date": "2025-04-29"}}}}

Ausfälle dieser Woche:
{{"action": "get_cancellations", "params": {{"date": "2025-04-29"}}}}

Nächste Stunde:
{{"action": "next_lesson", "params": {{}}}}

Wichtig: date immer YYYY-MM-DD, bei relativem Datum (morgen, nächsten Montag) korrekt berechnen."""


class UntisClient:
    """Async WebUntis JSON-RPC Client."""

    def __init__(self, server: str, school: str, username: str, password: str):
        self.base_url = f"https://{server}/WebUntis/jsonrpc.do"
        self.school = school
        self.username = username
        self.password = password
        self.session_id: Optional[str] = None
        self.person_id: Optional[int] = None
        self.person_type: Optional[int] = None
        self._req_id = 0

    def _next_id(self) -> str:
        self._req_id += 1
        return str(self._req_id)

    async def _call(self, client: httpx.AsyncClient, method: str, params: dict) -> dict:
        payload = {
            "id": self._next_id(),
            "method": method,
            "params": params,
            "jsonrpc": "2.0",
        }
        headers = {}
        if self.session_id:
            headers["Cookie"] = f"JSESSIONID={self.session_id}"

        resp = await client.post(
            self.base_url,
            params={"school": self.school},
            json=payload,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Untis Fehler: {data['error'].get('message', data['error'])}")
        return data.get("result", {})

    async def login(self, client: httpx.AsyncClient):
        result = await self._call(client, "authenticate", {
            "user": self.username,
            "password": self.password,
            "client": "IDA-Bot",
        })
        self.session_id = result["sessionId"]
        self.person_id = result.get("personId")
        self.person_type = result.get("personType", 5)

    async def logout(self, client: httpx.AsyncClient):
        try:
            await self._call(client, "logout", {})
        except Exception:
            pass
        self.session_id = None

    async def get_timetable(self, client: httpx.AsyncClient, date: datetime) -> list[dict]:
        date_int = int(date.strftime("%Y%m%d"))
        result = await self._call(client, "getTimetable", {
            "id": self.person_id,
            "type": self.person_type,
            "startDate": date_int,
            "endDate": date_int,
        })
        return result if isinstance(result, list) else []

    async def get_timetable_range(
        self, client: httpx.AsyncClient, start: datetime, end: datetime
    ) -> list[dict]:
        result = await self._call(client, "getTimetable", {
            "id": self.person_id,
            "type": self.person_type,
            "startDate": int(start.strftime("%Y%m%d")),
            "endDate": int(end.strftime("%Y%m%d")),
        })
        return result if isinstance(result, list) else []


def _load_untis_config() -> Optional[dict]:
    if not config.UNTIS_CONFIG_FILE or not __import__("os").path.exists(config.UNTIS_CONFIG_FILE):
        return None
    try:
        with open(config.UNTIS_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _format_time(t: int) -> str:
    s = str(t).zfill(4)
    return f"{s[:2]}:{s[2:]}"


def _format_lesson(lesson: dict) -> str:
    time_s = _format_time(lesson.get("startTime", 0))
    time_e = _format_time(lesson.get("endTime", 0))
    subjects = ", ".join(s.get("longName", s.get("name", "?")) for s in lesson.get("su", []))
    teachers = ", ".join(t.get("name", "") for t in lesson.get("te", []))
    rooms = ", ".join(r.get("name", "") for r in lesson.get("ro", []))
    code = lesson.get("code", "")
    status = f" [{LESSON_CODES.get(code, code)}]" if code else ""
    parts = [f"{time_s}–{time_e}", subjects or "?"]
    if teachers:
        parts.append(teachers)
    if rooms:
        parts.append(f"Raum {rooms}")
    return "".join(["• "] + [" | ".join(parts)] + [status])


class UntisWorker(BaseAgent):
    name = "untis_worker"
    description = "Zeigt Stundenplan, erkennt Ausfälle und Vertretungen (WebUntis)"

    def __init__(self):
        super().__init__()
        self._send_callback: Optional[Callable[[int, str], Awaitable]] = None

    def set_send_callback(self, callback: Callable[[int, str], Awaitable]):
        self._send_callback = callback

    def _make_client(self, cfg: dict) -> UntisClient:
        return UntisClient(
            server=cfg["server"],
            school=cfg["school"],
            username=cfg["username"],
            password=cfg["password"],
        )

    async def process(self, message: AgentMessage) -> AgentResponse:
        cfg = _load_untis_config()
        if not cfg or not cfg.get("server"):
            return AgentResponse(
                content="Untis ist nicht konfiguriert. Bitte im Web-Interface einrichten: "
                        f"http://localhost:{config.WEB_PORT}/untis",
                success=False,
            )

        now_str = datetime.now(LOCAL_TZ).strftime("%A, %d.%m.%Y %H:%M Uhr")
        system = SYSTEM_PROMPT.format(now=now_str)
        raw = await self._chat(
            messages=[{"role": "user", "content": message.content}],
            system=system,
        )

        try:
            raw_clean = raw.strip().lstrip("```json").rstrip("```").strip()
            parsed = json.loads(raw_clean)
        except json.JSONDecodeError:
            return AgentResponse(content=raw)

        action = parsed.get("action", "")
        params = parsed.get("params", {})

        try:
            async with httpx.AsyncClient() as client:
                uc = self._make_client(cfg)
                await uc.login(client)
                try:
                    result = await self._dispatch(uc, client, action, params)
                finally:
                    await uc.logout(client)
            return AgentResponse(content=result)
        except Exception as e:
            logger.error(f"Untis Fehler: {e}")
            return AgentResponse(content=f"Untis Fehler: {e}", success=False)

    async def _dispatch(
        self, uc: UntisClient, client: httpx.AsyncClient, action: str, params: dict
    ) -> str:
        now = datetime.now(LOCAL_TZ)

        if action == "get_timetable":
            date = datetime.fromisoformat(params.get("date", now.strftime("%Y-%m-%d")))
            lessons = await uc.get_timetable(client, date)
            return self._format_day(lessons, date)

        elif action == "get_week":
            date = datetime.fromisoformat(params.get("date", now.strftime("%Y-%m-%d")))
            mon = date - timedelta(days=date.weekday())
            fri = mon + timedelta(days=4)
            lessons = await uc.get_timetable_range(client, mon, fri)
            return self._format_week(lessons, mon)

        elif action == "get_cancellations":
            date = datetime.fromisoformat(params.get("date", now.strftime("%Y-%m-%d")))
            mon = date - timedelta(days=date.weekday())
            fri = mon + timedelta(days=4)
            lessons = await uc.get_timetable_range(client, mon, fri)
            cancelled = [l for l in lessons if l.get("code") in ("cancelled", "irregular")]
            if not cancelled:
                return "Diese Woche gibt es keine Ausfälle oder Änderungen."
            lines = ["Ausfälle/Änderungen diese Woche:\n"]
            for l in sorted(cancelled, key=lambda x: (x.get("date", 0), x.get("startTime", 0))):
                d = str(l.get("date", ""))
                date_str = f"{d[6:8]}.{d[4:6]}.{d[:4]}" if len(d) == 8 else d
                lines.append(f"{date_str}: {_format_lesson(l)}")
            return "\n".join(lines)

        elif action == "next_lesson":
            lessons = await uc.get_timetable(client, now)
            current_time = int(now.strftime("%H%M"))
            upcoming = [l for l in lessons if l.get("startTime", 0) > current_time]
            if not upcoming:
                tomorrow = now + timedelta(days=1)
                lessons = await uc.get_timetable(client, tomorrow)
                upcoming = lessons
            if not upcoming:
                return "Keine weiteren Stunden heute oder morgen gefunden."
            nxt = sorted(upcoming, key=lambda x: x.get("startTime", 0))[0]
            return f"Nächste Stunde:\n{_format_lesson(nxt)}"

        return f"Unbekannte Aktion: {action}"

    @staticmethod
    def _format_day(lessons: list, date: datetime) -> str:
        day_str = date.strftime("%A, %d.%m.%Y")
        if not lessons:
            return f"Keine Stunden am {day_str}."
        lines = [f"Stundenplan {day_str}:\n"]
        for l in sorted(lessons, key=lambda x: x.get("startTime", 0)):
            lines.append(_format_lesson(l))
        return "\n".join(lines)

    @staticmethod
    def _format_week(lessons: list, monday: datetime) -> str:
        by_day: dict[int, list] = {}
        for l in lessons:
            d = l.get("date", 0)
            by_day.setdefault(d, []).append(l)

        lines = [f"Stundenplan KW {monday.strftime('%V')}:\n"]
        for i in range(5):
            day = monday + timedelta(days=i)
            day_int = int(day.strftime("%Y%m%d"))
            day_lessons = sorted(by_day.get(day_int, []), key=lambda x: x.get("startTime", 0))
            lines.append(f"\n{day.strftime('%A %d.%m')}:")
            if not day_lessons:
                lines.append("  (frei)")
            else:
                for l in day_lessons:
                    lines.append(f"  {_format_lesson(l)}")
        return "\n".join(lines)

    # ── Hintergrundcheck für Ausfälle ─────────────────────────────────────────

    async def check_for_changes(self):
        """
        Vergleicht aktuellen Stundenplan mit gespeichertem Stand.
        Bei Änderungen: Telegram-Benachrichtigung senden.
        Wird vom Scheduler periodisch aufgerufen.
        """
        cfg = _load_untis_config()
        if not cfg or not cfg.get("server"):
            return

        now = datetime.now(LOCAL_TZ)
        today_int = int(now.strftime("%Y%m%d"))
        friday = now + timedelta(days=(4 - now.weekday()))
        friday_int = int(friday.strftime("%Y%m%d"))

        try:
            async with httpx.AsyncClient() as client:
                uc = self._make_client(cfg)
                await uc.login(client)
                try:
                    lessons = await uc.get_timetable_range(client, now, friday)
                finally:
                    await uc.logout(client)
        except Exception as e:
            logger.warning(f"Untis Check fehlgeschlagen: {e}")
            return

        current_key = {str(l["id"]): l.get("code", "") for l in lessons}
        last_key: dict = self.memory.get("last_timetable_state", {})

        changes: list[str] = []
        for l in lessons:
            lid = str(l["id"])
            new_code = l.get("code", "")
            old_code = last_key.get(lid, "__new__")
            if old_code == "__new__" and new_code in ("cancelled", "irregular"):
                changes.append(f"Neu: {_format_lesson(l)}")
            elif old_code != new_code and new_code in ("cancelled", "irregular"):
                changes.append(f"Geändert: {_format_lesson(l)}")

        self.memory.set("last_timetable_state", current_key)
        self.memory.set("last_check", now.isoformat())

        if changes and self._send_callback:
            text = "Untis-Änderungen erkannt:\n\n" + "\n".join(changes)
            chat_ids = cfg.get("notify_chat_ids") or config.TELEGRAM_ALLOWED_USERS
            for cid in chat_ids:
                try:
                    await self._send_callback(int(cid), text)
                except Exception as e:
                    logger.warning(f"Benachrichtigung an {cid} fehlgeschlagen: {e}")
