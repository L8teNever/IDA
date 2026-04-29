import json
import logging
import os
from agents.base import BaseAgent, AgentMessage, AgentResponse
from agents.context import ContextStore, SharedContext
import config

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(config.DATA_DIR, "conversation_history.json")

SYSTEM_PROMPT = """Du bist IDA, eine persönliche KI-Assistentin. Du bist hilfsbereit, direkt und effizient.
Antworte immer auf Deutsch, es sei denn, der Nutzer schreibt in einer anderen Sprache.

Du hast Zugang zu spezialisierten Arbeitern:
{workers}

DELEGIEREN (ein Worker):
DELEGATE:<worker_name>:<aufgabe für den worker>

KETTE (mehrere Worker nacheinander):
CHAIN:<worker1>,<worker2>:<aufgabe>
Der zweite Worker bekommt das Ergebnis des ersten als Kontext.

WIEDERKEHRENDEN TASK PLANEN:
SCHEDULE:<cron>|<job_id>|<beschreibung>
<Bestätigungstext für den Nutzer>

Cron-Beispiele: "0 9 * * 1" = jeden Montag 9 Uhr | "0 8 * * *" = täglich 8 Uhr

{context}

Andernfalls beantworte direkt und freundlich auf Deutsch."""


class Orchestrator(BaseAgent):
    name = "ida"
    description = "IDA - Hauptkoordinatorin"

    def __init__(self):
        super().__init__()
        self.model = config.MAIN_MODEL
        self.workers: dict[str, BaseAgent] = {}
        self.conversation_history: dict[int, list] = {}
        self.context_store = ContextStore()
        self._load_history()

    # ── Persistenz ──────────────────────────────────────────────────────────

    def _load_history(self):
        if not os.path.exists(HISTORY_FILE):
            return
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            self.conversation_history = {int(k): v for k, v in raw.items()}
            logger.info(f"Gesprächskontext für {len(self.conversation_history)} Nutzer geladen")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"Konversationshistorie konnte nicht geladen werden: {e}")

    def _save_history(self):
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.conversation_history, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"Konversationshistorie konnte nicht gespeichert werden: {e}")

    # ── Worker-Registrierung ─────────────────────────────────────────────────

    def register_worker(self, worker: BaseAgent):
        self.workers[worker.name] = worker
        logger.info(f"Worker registriert: {worker.name} - {worker.description}")

    def _workers_description(self) -> str:
        if not self.workers:
            return "Keine Spezialisten verfügbar"
        return "\n".join(f"- {w.name}: {w.description}" for w in self.workers.values())

    # ── Hauptverarbeitung ────────────────────────────────────────────────────

    async def process(self, message: AgentMessage) -> AgentResponse:
        user_id = message.metadata.get("user_id", 0)
        ctx = self.context_store.get_for_user(user_id)

        # Bild direkt an VisionWorker, Ergebnis in Kontext eintragen
        if message.metadata.get("has_image") and "vision_worker" in self.workers:
            if user_id not in self.conversation_history:
                self.conversation_history[user_id] = []
            history = self.conversation_history[user_id]
            history.append({"role": "user", "content": f"[Bild gesendet] {message.content}"})
            vision_resp = await self.workers["vision_worker"].process(message)
            ctx.set("letztes_bild_analyse", vision_resp.content, worker="vision_worker")
            self._append_history(user_id, history, f"[Bildanalyse] {vision_resp.content}")
            return vision_resp

        if user_id not in self.conversation_history:
            self.conversation_history[user_id] = []
        history = self.conversation_history[user_id]
        history.append({"role": "user", "content": message.content})

        context_text = ctx.as_prompt_text()
        system = SYSTEM_PROMPT.format(
            workers=self._workers_description(),
            context=context_text,
        )
        raw = await self._chat(messages=history, system=system)

        # ── CHAIN: mehrere Worker nacheinander ──────────────────────────────
        if raw.startswith("CHAIN:"):
            return await self._handle_chain(raw, message, user_id, history, ctx, system)

        # ── DELEGATE: einzelner Worker ──────────────────────────────────────
        if raw.startswith("DELEGATE:"):
            return await self._handle_delegate(raw, message, user_id, history, ctx, system)

        # ── SCHEDULE: wiederkehrender Task ──────────────────────────────────
        if raw.startswith("SCHEDULE:"):
            lines = raw.split("\n", 1)
            user_message = lines[1].strip() if len(lines) > 1 else "Aufgabe wurde geplant."
            self._append_history(user_id, history, user_message)
            return AgentResponse(
                content=user_message,
                metadata={"schedule_command": lines[0]},
            )

        self._append_history(user_id, history, raw)
        return AgentResponse(content=raw)

    # ── Delegation ───────────────────────────────────────────────────────────

    async def _handle_delegate(
        self, raw: str, message: AgentMessage,
        user_id: int, history: list, ctx: SharedContext, system: str,
    ) -> AgentResponse:
        parts = raw[9:].split(":", 1)
        if len(parts) != 2:
            self._append_history(user_id, history, raw)
            return AgentResponse(content=raw)

        worker_name, task = parts[0].strip(), parts[1].strip()
        if worker_name not in self.workers:
            fallback = f"Unbekannter Worker: {worker_name}"
            self._append_history(user_id, history, fallback)
            return AgentResponse(content=fallback)

        logger.info(f"Delegiere an {worker_name}: {task[:80]}")
        worker_msg = AgentMessage(
            content=self._enrich_task(task, ctx),
            metadata={**message.metadata, "shared_context": ctx},
        )
        worker_resp = await self.workers[worker_name].process(worker_msg)
        ctx.set(f"{worker_name}_ergebnis", worker_resp.content, worker=worker_name)

        final = await self._summarize(message.content, worker_name, worker_resp.content, ctx, system)
        self._append_history(user_id, history, final)
        return AgentResponse(content=final)

    async def _handle_chain(
        self, raw: str, message: AgentMessage,
        user_id: int, history: list, ctx: SharedContext, system: str,
    ) -> AgentResponse:
        rest = raw[6:]  # nach "CHAIN:"
        parts = rest.split(":", 1)
        if len(parts) != 2:
            self._append_history(user_id, history, raw)
            return AgentResponse(content=raw)

        worker_names = [w.strip() for w in parts[0].split(",")]
        task = parts[1].strip()
        last_result = ""

        for worker_name in worker_names:
            if worker_name not in self.workers:
                logger.warning(f"Chain: Worker '{worker_name}' nicht gefunden, überspringe")
                continue
            logger.info(f"Chain-Schritt: {worker_name}")
            chain_task = task
            if last_result:
                chain_task = f"{task}\n\nVorheriges Ergebnis:\n{last_result}"
            worker_msg = AgentMessage(
                content=self._enrich_task(chain_task, ctx),
                metadata={**message.metadata, "shared_context": ctx},
            )
            worker_resp = await self.workers[worker_name].process(worker_msg)
            last_result = worker_resp.content
            ctx.set(f"{worker_name}_ergebnis", last_result, worker=worker_name)

        final = await self._summarize(message.content, "+".join(worker_names), last_result, ctx, system)
        self._append_history(user_id, history, final)
        return AgentResponse(content=final)

    # ── Hilfsmethoden ────────────────────────────────────────────────────────

    @staticmethod
    def _enrich_task(task: str, ctx: SharedContext) -> str:
        ctx_text = ctx.as_prompt_text()
        if ctx_text:
            return f"{task}\n\n{ctx_text}"
        return task

    async def _summarize(
        self, original: str, worker_name: str, result: str, ctx: SharedContext, system: str
    ) -> str:
        prompt = (
            f"Nutzeranfrage: {original}\n"
            f"Ergebnis von {worker_name}:\n{result}\n\n"
            "Fasse das Ergebnis prägnant und verständlich für den Nutzer zusammen."
        )
        return await self._chat(messages=[{"role": "user", "content": prompt}], system=system)

    def _append_history(self, user_id: int, history: list, response: str):
        history.append({"role": "assistant", "content": response})
        if len(history) > 30:
            self.conversation_history[user_id] = history[-30:]
        self._save_history()
