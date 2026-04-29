"""
SharedContext - geteilter Arbeitsbereich zwischen allen Workern.

Jeder Nutzer hat seinen eigenen Kontext. Worker können Zwischenergebnisse
hinterlegen, die andere Worker dann abrufen können.

Beispiel:
  calendar_worker speichert gefundene Termine → api_worker kann darauf zugreifen
  ida fragt "Erstell einen Termin in der nächsten Lücke" → Kontext enthält die freien Zeiten
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ContextEntry:
    value: Any
    worker: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class SharedContext:
    def __init__(self):
        self._store: dict[str, ContextEntry] = {}

    def set(self, key: str, value: Any, worker: str = ""):
        self._store[key] = ContextEntry(value=value, worker=worker)

    def get(self, key: str, default=None) -> Any:
        entry = self._store.get(key)
        return entry.value if entry else default

    def clear(self):
        self._store.clear()

    def as_prompt_text(self) -> str:
        """Zusammenfassung für den LLM-Prompt - zeigt was Worker bereits ermittelt haben."""
        if not self._store:
            return ""
        lines = ["[Bereits ermittelte Informationen:]"]
        for key, entry in self._store.items():
            val = entry.value
            if isinstance(val, str):
                preview = val[:400]
            elif isinstance(val, list):
                preview = f"{len(val)} Einträge: " + ", ".join(str(v)[:50] for v in val[:3])
            else:
                preview = str(val)[:400]
            lines.append(f"- {key} (von {entry.worker}): {preview}")
        return "\n".join(lines)


class ContextStore:
    """Hält einen SharedContext pro Nutzer (in-memory, nicht persistiert)."""

    def __init__(self):
        self._contexts: dict[int, SharedContext] = {}

    def get_for_user(self, user_id: int) -> SharedContext:
        if user_id not in self._contexts:
            self._contexts[user_id] = SharedContext()
        return self._contexts[user_id]

    def clear_for_user(self, user_id: int):
        self._contexts.pop(user_id, None)
