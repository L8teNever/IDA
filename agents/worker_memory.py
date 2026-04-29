"""
WorkerMemory - persistentes Gedächtnis pro Worker.

Jeder Worker bekommt automatisch eine eigene WorkerMemory-Instanz (via BaseAgent).
Daten überleben Neustarts und Updates.

Datei: data/memory_{worker_name}.json
"""

import json
import logging
import os
from typing import Any
import config

logger = logging.getLogger(__name__)


class WorkerMemory:
    def __init__(self, worker_name: str):
        self._file = os.path.join(config.DATA_DIR, f"memory_{worker_name}.json")
        self._data: dict = self._load()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value
        self._save()

    def delete(self, key: str):
        if key in self._data:
            del self._data[key]
            self._save()

    def all(self) -> dict:
        return dict(self._data)

    def _load(self) -> dict:
        if not os.path.exists(self._file):
            return {}
        try:
            with open(self._file, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"WorkerMemory [{self._file}] konnte nicht geladen werden: {e}")
            return {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, default=str)
        except OSError as e:
            logger.warning(f"WorkerMemory [{self._file}] konnte nicht gespeichert werden: {e}")
