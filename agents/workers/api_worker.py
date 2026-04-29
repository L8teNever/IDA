"""
API Worker - zuständig für externe HTTP/API-Anfragen.

Neue API-Funktionen hinzufügen:
1. Neue Methode in APIWorker definieren (z.B. async def _fetch_weather(...))
2. Methode in AVAILABLE_FUNCTIONS registrieren (am Ende der Klasse)
3. System-Prompt erweitern mit der neuen Funktion
"""

import json
import logging
import httpx
from agents.base import BaseAgent, AgentMessage, AgentResponse

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du bist ein API-Spezialist. Du analysierst Anfragen und führst die passende Aktion aus.

Verfügbare Funktionen:
{functions}

Antworte NUR mit einem JSON-Objekt, das die Funktion und ihre Parameter angibt:
{{"function": "<funktionsname>", "params": {{...}}}}

Wenn keine Funktion passt: {{"function": "none", "reason": "..."}}"""


class APIWorker(BaseAgent):
    name = "api_worker"
    description = "Zuständig für externe API-Anfragen und HTTP-Requests"

    def __init__(self):
        super().__init__()
        self.functions = self._register_functions()

    def _register_functions(self) -> dict:
        """
        Hier neue Funktionen registrieren.
        Format: {"funktionsname": {"description": "...", "params": {...}, "handler": self._methode}}
        """
        return {
            "http_get": {
                "description": "HTTP GET Request an eine URL senden",
                "params": {"url": "string", "headers": "dict (optional)"},
                "handler": self._http_get,
            },
            "http_post": {
                "description": "HTTP POST Request mit JSON-Body senden",
                "params": {"url": "string", "body": "dict", "headers": "dict (optional)"},
                "handler": self._http_post,
            },
        }

    def _functions_description(self) -> str:
        return "\n".join(
            f"- {name}: {info['description']} | Parameter: {info['params']}"
            for name, info in self.functions.items()
        )

    async def process(self, message: AgentMessage) -> AgentResponse:
        system = SYSTEM_PROMPT.format(functions=self._functions_description())
        raw = await self._chat(
            messages=[{"role": "user", "content": message.content}],
            system=system,
        )

        try:
            parsed = json.loads(raw.strip())
            func_name = parsed.get("function", "none")

            if func_name == "none":
                return AgentResponse(
                    content=f"Kein passender API-Aufruf gefunden: {parsed.get('reason', '')}",
                    success=False,
                )

            if func_name not in self.functions:
                return AgentResponse(content=f"Unbekannte Funktion: {func_name}", success=False)

            handler = self.functions[func_name]["handler"]
            result = await handler(parsed.get("params", {}))
            return AgentResponse(content=result)

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"API Worker JSON-Fehler: {e} | Antwort war: {raw}")
            return AgentResponse(content=raw)

    async def _http_get(self, params: dict) -> str:
        url = params.get("url", "")
        headers = params.get("headers", {})
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers)
            return f"Status {response.status_code}:\n{response.text[:3000]}"

    async def _http_post(self, params: dict) -> str:
        url = params.get("url", "")
        body = params.get("body", {})
        headers = params.get("headers", {})
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=body, headers=headers)
            return f"Status {response.status_code}:\n{response.text[:3000]}"
