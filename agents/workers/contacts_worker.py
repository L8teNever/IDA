"""
Google Contacts Worker - sucht, erstellt, bearbeitet und löscht Kontakte.

Benötigte Google API: People API (aktivieren unter console.cloud.google.com)
Scope: https://www.googleapis.com/auth/contacts (in _google_base.py enthalten)
"""

import logging
from typing import Optional

from googleapiclient.errors import HttpError

from agents.base import BaseAgent, AgentMessage, AgentResponse
from agents.workers._google_base import build_service, _parse_json_response

logger = logging.getLogger(__name__)

PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,biographies"

SYSTEM_PROMPT = """Du bist ein Google Kontakte Spezialist.
Antworte NUR mit einem JSON-Objekt.

Kontakte suchen:
{{"action": "search", "params": {{"query": "Max Mustermann"}}}}

Alle Kontakte auflisten (max 30):
{{"action": "list", "params": {{}}}}

Kontakt anlegen:
{{"action": "create", "params": {{"given_name": "Max", "family_name": "Mustermann", "email": "max@example.com", "phone": "+49 123 456789", "company": "Musterfirma", "notes": "..."}}}}

Kontakt bearbeiten:
{{"action": "update", "params": {{"resource_name": "people/c1234567890", "given_name": "...", "family_name": "...", "email": "...", "phone": "...", "company": "...", "notes": "..."}}}}

Kontakt löschen:
{{"action": "delete", "params": {{"resource_name": "people/c1234567890"}}}}

Regeln:
- resource_name aus vorherigen Suchergebnissen
- Beim Bearbeiten: nur angegebene Felder werden geändert, andere bleiben erhalten
{context}"""


class ContactsWorker(BaseAgent):
    name = "contacts_worker"
    description = "Sucht, erstellt, bearbeitet und löscht Google Kontakte"

    def __init__(self):
        super().__init__()
        self._service = None

    def _svc(self):
        if not self._service:
            self._service = build_service("people", "v1")
        return self._service

    async def process(self, message: AgentMessage) -> AgentResponse:
        try:
            self._svc()
        except RuntimeError as e:
            return AgentResponse(content=str(e), success=False)

        contact_cache: dict = self.memory.get("contact_cache", {})
        context_lines = []
        if contact_cache:
            context_lines.append("\nBekannte Kontakt-IDs aus letzter Suche:")
            for rname, info in list(contact_cache.items())[-10:]:
                context_lines.append(f"  {rname}: {info.get('name','?')} ({info.get('email','')})")
        context = "\n".join(context_lines)

        system = SYSTEM_PROMPT.format(context=context)
        raw = await self._chat(messages=[{"role": "user", "content": message.content}], system=system, num_predict=150)

        parsed = _parse_json_response(raw)
        if not parsed:
            return AgentResponse(content=raw)

        action = parsed.get("action", "")
        params = parsed.get("params", {})
        try:
            if action == "search":
                return AgentResponse(content=self._search(params))
            elif action == "list":
                return AgentResponse(content=self._list_contacts())
            elif action == "create":
                return AgentResponse(content=self._create(params))
            elif action == "update":
                return AgentResponse(content=self._update(params))
            elif action == "delete":
                return AgentResponse(content=self._delete(params))
            else:
                return AgentResponse(content=f"Unbekannte Aktion: {action}", success=False)
        except HttpError as e:
            return AgentResponse(content=f"Google Kontakte Fehler: {e}", success=False)

    def _search(self, params: dict) -> str:
        query = params.get("query", "")
        if not query:
            return "Fehler: Suchbegriff fehlt."
        result = self._svc().people().searchContacts(
            query=query, readMask=PERSON_FIELDS
        ).execute()
        results = result.get("results", [])
        if not results:
            return f"Keine Kontakte für '{query}' gefunden."
        return self._format_and_cache(
            [r.get("person", {}) for r in results],
            f"Suchergebnisse für '{query}':"
        )

    def _list_contacts(self) -> str:
        result = self._svc().people().connections().list(
            resourceName="people/me",
            pageSize=30,
            personFields=PERSON_FIELDS,
            sortOrder="FIRST_NAME_ASCENDING",
        ).execute()
        contacts = result.get("connections", [])
        if not contacts:
            return "Keine Kontakte vorhanden."
        return self._format_and_cache(contacts, "Kontakte:")

    def _create(self, params: dict) -> str:
        body: dict = {}
        names = {}
        if params.get("given_name"):
            names["givenName"] = params["given_name"]
        if params.get("family_name"):
            names["familyName"] = params["family_name"]
        if names:
            body["names"] = [names]
        if params.get("email"):
            body["emailAddresses"] = [{"value": params["email"]}]
        if params.get("phone"):
            body["phoneNumbers"] = [{"value": params["phone"]}]
        if params.get("company"):
            body["organizations"] = [{"name": params["company"]}]
        if params.get("notes"):
            body["biographies"] = [{"value": params["notes"], "contentType": "TEXT_PLAIN"}]

        contact = self._svc().people().createContact(body=body).execute()
        rname = contact.get("resourceName", "")
        full_name = _get_name(contact)
        cache: dict = self.memory.get("contact_cache", {})
        cache[rname] = {"name": full_name, "email": params.get("email", ""), "etag": contact.get("etag", "")}
        self.memory.set("contact_cache", cache)
        return f"Kontakt erstellt: {full_name}\nID: {rname}"

    def _update(self, params: dict) -> str:
        rname = params.get("resource_name", "")
        if not rname:
            return "Fehler: resource_name fehlt. Bitte zuerst Kontakt suchen."

        contact = self._svc().people().get(
            resourceName=rname, personFields=PERSON_FIELDS
        ).execute()

        update_fields = []

        if params.get("given_name") or params.get("family_name"):
            existing = contact.get("names", [{}])[0]
            existing["givenName"] = params.get("given_name", existing.get("givenName", ""))
            existing["familyName"] = params.get("family_name", existing.get("familyName", ""))
            contact["names"] = [existing]
            update_fields.append("names")

        if params.get("email"):
            contact["emailAddresses"] = [{"value": params["email"]}]
            update_fields.append("emailAddresses")

        if params.get("phone"):
            contact["phoneNumbers"] = [{"value": params["phone"]}]
            update_fields.append("phoneNumbers")

        if params.get("company"):
            contact["organizations"] = [{"name": params["company"]}]
            update_fields.append("organizations")

        if params.get("notes"):
            contact["biographies"] = [{"value": params["notes"], "contentType": "TEXT_PLAIN"}]
            update_fields.append("biographies")

        if not update_fields:
            return "Keine Felder zum Aktualisieren angegeben."

        updated = self._svc().people().updateContact(
            resourceName=rname,
            updatePersonFields=",".join(update_fields),
            body=contact,
        ).execute()

        full_name = _get_name(updated)
        cache: dict = self.memory.get("contact_cache", {})
        if rname in cache:
            cache[rname]["name"] = full_name
            cache[rname]["etag"] = updated.get("etag", "")
            self.memory.set("contact_cache", cache)
        return f"Kontakt aktualisiert: {full_name}"

    def _delete(self, params: dict) -> str:
        rname = params.get("resource_name", "")
        if not rname:
            return "Fehler: resource_name fehlt."
        cache: dict = self.memory.get("contact_cache", {})
        name = cache.get(rname, {}).get("name", rname)
        self._svc().people().deleteContact(resourceName=rname).execute()
        cache.pop(rname, None)
        self.memory.set("contact_cache", cache)
        return f"Kontakt gelöscht: {name}"

    def _format_and_cache(self, contacts: list, header: str) -> str:
        cache: dict = self.memory.get("contact_cache", {})
        lines = [header + "\n"]
        for c in contacts:
            rname = c.get("resourceName", "")
            name = _get_name(c)
            emails = [e.get("value", "") for e in c.get("emailAddresses", [])]
            phones = [p.get("value", "") for p in c.get("phoneNumbers", [])]
            orgs = [o.get("name", "") for o in c.get("organizations", [])]
            parts = [name]
            if emails:
                parts.append(", ".join(emails))
            if phones:
                parts.append(", ".join(phones))
            if orgs:
                parts.append(orgs[0])
            lines.append(f"• {' | '.join(parts)}\n  ID: {rname}")
            cache[rname] = {"name": name, "email": emails[0] if emails else "", "etag": c.get("etag", "")}
        self.memory.set("contact_cache", cache)
        return "\n".join(lines)


def _get_name(contact: dict) -> str:
    names = contact.get("names", [])
    if names:
        n = names[0]
        return f"{n.get('givenName','')} {n.get('familyName','')}".strip()
    return "(Kein Name)"
