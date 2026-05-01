"""
Google Tasks Worker - verwaltet Aufgabenlisten und Aufgaben.

Benötigte Google API: Google Tasks API (aktivieren unter console.cloud.google.com)
Scope: https://www.googleapis.com/auth/tasks (in _google_base.py enthalten)
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from agents.base import BaseAgent, AgentMessage, AgentResponse
from agents.workers._google_base import build_service, _parse_json_response

logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("Europe/Berlin")

SYSTEM_PROMPT = """Du bist ein Google Tasks Spezialist. Heute ist {now}.
Antworte NUR mit einem JSON-Objekt.

Alle Aufgabenlisten anzeigen:
{{"action": "list_tasklists", "params": {{}}}}

Aufgaben in einer Liste anzeigen:
{{"action": "list_tasks", "params": {{"tasklist_id": "@default"}}}}

Aufgabe erstellen:
{{"action": "create_task", "params": {{"title": "...", "tasklist_id": "@default", "due": "2025-04-30", "notes": "..."}}}}

Aufgabe als erledigt markieren:
{{"action": "complete_task", "params": {{"task_id": "...", "tasklist_id": "@default"}}}}

Aufgabe bearbeiten:
{{"action": "update_task", "params": {{"task_id": "...", "tasklist_id": "@default", "title": "...", "due": "2025-05-01", "notes": "..."}}}}

Aufgabe löschen:
{{"action": "delete_task", "params": {{"task_id": "...", "tasklist_id": "@default"}}}}

Regeln:
- tasklist_id "@default" = Standard-Liste. Andere Listen-IDs aus list_tasklists.
- due als YYYY-MM-DD (ohne Uhrzeit)
- task_id aus vorherigen Abfragen
{context}"""


class TasksWorker(BaseAgent):
    name = "tasks_worker"
    description = "Verwaltet Google Tasks: Aufgaben erstellen, bearbeiten, erledigen, löschen"

    def __init__(self):
        super().__init__()
        self._service = None

    def _svc(self):
        if not self._service:
            self._service = build_service("tasks", "v1")
        return self._service

    async def process(self, message: AgentMessage) -> AgentResponse:
        try:
            self._svc()
        except RuntimeError as e:
            return AgentResponse(content=str(e), success=False)

        # Kontext aus Gedächtnis aufbauen
        task_cache: dict = self.memory.get("task_cache", {})
        list_cache: dict = self.memory.get("list_cache", {})
        context_lines = []
        if list_cache:
            context_lines.append("\nBekannte Aufgabenlisten:")
            for lid, lname in list_cache.items():
                context_lines.append(f"  {lid}: {lname}")
        if task_cache:
            context_lines.append("\nBekannte Aufgaben-IDs:")
            for tid, tinfo in list(task_cache.items())[-10:]:
                context_lines.append(f"  {tid}: {tinfo.get('title','?')} [{tinfo.get('status','?')}]")
        context = "\n".join(context_lines)

        now_str = datetime.now(LOCAL_TZ).strftime("%A, %d.%m.%Y %H:%M Uhr")
        system = SYSTEM_PROMPT.format(now=now_str, context=context)
        raw = await self._chat(messages=[{"role": "user", "content": message.content}], system=system, num_predict=150)

        parsed = _parse_json_response(raw)
        if not parsed:
            return AgentResponse(content=raw)

        action = parsed.get("action", "")
        params = parsed.get("params", {})
        try:
            if action == "list_tasklists":
                return AgentResponse(content=self._list_tasklists())
            elif action == "list_tasks":
                return AgentResponse(content=self._list_tasks(params))
            elif action == "create_task":
                return AgentResponse(content=self._create_task(params))
            elif action == "complete_task":
                return AgentResponse(content=self._complete_task(params))
            elif action == "update_task":
                return AgentResponse(content=self._update_task(params))
            elif action == "delete_task":
                return AgentResponse(content=self._delete_task(params))
            else:
                return AgentResponse(content=f"Unbekannte Aktion: {action}", success=False)
        except HttpError as e:
            return AgentResponse(content=f"Google Tasks Fehler: {e}", success=False)

    def _list_tasklists(self) -> str:
        result = self._svc().tasklists().list(maxResults=20).execute()
        items = result.get("items", [])
        if not items:
            return "Keine Aufgabenlisten vorhanden."
        cache = {tl["id"]: tl["title"] for tl in items}
        self.memory.set("list_cache", cache)
        lines = ["Aufgabenlisten:\n"]
        for tl in items:
            lines.append(f"• {tl['title']}\n  ID: {tl['id']}")
        return "\n".join(lines)

    def _list_tasks(self, params: dict) -> str:
        lid = params.get("tasklist_id", "@default")
        result = self._svc().tasks().list(tasklist=lid, showCompleted=False, maxResults=30).execute()
        items = result.get("items", [])
        if not items:
            return "Keine offenen Aufgaben in dieser Liste."
        cache: dict = self.memory.get("task_cache", {})
        lines = ["Offene Aufgaben:\n"]
        for t in items:
            due = t.get("due", "")
            due_str = f" (fällig: {due[:10]})" if due else ""
            notes = f"\n  {t['notes']}" if t.get("notes") else ""
            lines.append(f"• {t['title']}{due_str}{notes}\n  ID: {t['id']}")
            cache[t["id"]] = {"title": t["title"], "status": t.get("status", ""), "list_id": lid}
        self.memory.set("task_cache", cache)
        return "\n".join(lines)

    def _create_task(self, params: dict) -> str:
        lid = params.get("tasklist_id", "@default")
        body: dict = {"title": params.get("title", "Neue Aufgabe"), "status": "needsAction"}
        if params.get("notes"):
            body["notes"] = params["notes"]
        if params.get("due"):
            due_str = params["due"]
            if "T" not in due_str:
                due_str += "T00:00:00.000Z"
            body["due"] = due_str
        task = self._svc().tasks().insert(tasklist=lid, body=body).execute()
        cache: dict = self.memory.get("task_cache", {})
        cache[task["id"]] = {"title": body["title"], "status": "needsAction", "list_id": lid}
        self.memory.set("task_cache", cache)
        due_info = f" (fällig: {params['due']})" if params.get("due") else ""
        return f"Aufgabe erstellt: {body['title']}{due_info}\nID: {task['id']}"

    def _complete_task(self, params: dict) -> str:
        tid = params.get("task_id", "")
        lid = params.get("tasklist_id", "@default")
        if not tid:
            return "Fehler: task_id fehlt. Bitte zuerst Aufgaben anzeigen."
        task = self._svc().tasks().get(tasklist=lid, task=tid).execute()
        task["status"] = "completed"
        self._svc().tasks().update(tasklist=lid, task=tid, body=task).execute()
        cache: dict = self.memory.get("task_cache", {})
        name = cache.get(tid, {}).get("title", tid)
        if tid in cache:
            cache[tid]["status"] = "completed"
            self.memory.set("task_cache", cache)
        return f"Aufgabe erledigt: {name}"

    def _update_task(self, params: dict) -> str:
        tid = params.get("task_id", "")
        lid = params.get("tasklist_id", "@default")
        if not tid:
            return "Fehler: task_id fehlt."
        task = self._svc().tasks().get(tasklist=lid, task=tid).execute()
        if params.get("title"):
            task["title"] = params["title"]
        if params.get("notes"):
            task["notes"] = params["notes"]
        if params.get("due"):
            due_str = params["due"]
            if "T" not in due_str:
                due_str += "T00:00:00.000Z"
            task["due"] = due_str
        updated = self._svc().tasks().update(tasklist=lid, task=tid, body=task).execute()
        cache: dict = self.memory.get("task_cache", {})
        if tid in cache:
            cache[tid]["title"] = updated.get("title", tid)
            self.memory.set("task_cache", cache)
        return f"Aufgabe aktualisiert: {updated.get('title', tid)}"

    def _delete_task(self, params: dict) -> str:
        tid = params.get("task_id", "")
        lid = params.get("tasklist_id", "@default")
        if not tid:
            return "Fehler: task_id fehlt."
        cache: dict = self.memory.get("task_cache", {})
        name = cache.get(tid, {}).get("title", tid)
        self._svc().tasks().delete(tasklist=lid, task=tid).execute()
        cache.pop(tid, None)
        self.memory.set("task_cache", cache)
        return f"Aufgabe gelöscht: {name}"
