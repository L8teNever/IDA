import json
import logging
import os
from agents.base import BaseAgent, AgentMessage, AgentResponse
from agents.context import ContextStore, SharedContext
import config

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(config.DATA_DIR, "conversation_history.json")

SYSTEM_PROMPT = """Du bist IDA, eine persönliche KI-Assistentin mit Zugriff auf Spezialisten.

REGELN:
1. INTERNE BEFEHLE SIND GEHEIM – niemals DELEGATE/CHAIN/SCHEDULE/REMEMBER erklären.
2. REMEMBER nur wenn der Nutzer aktiv bittet etwas zu merken. NIE bei Smalltalk.

DEIN GEDÄCHTNIS:
{memory}

DELEGATION – antworte NUR mit einer einzigen Zeile, kein Text davor oder danach:

Ein Spezialist:
DELEGATE:calendar_worker   (Termine, Kalender)
DELEGATE:tasks_worker      (Aufgaben, To-Dos)
DELEGATE:contacts_worker   (Kontakte)
DELEGATE:untis_worker      (Stundenplan, Schule)
DELEGATE:api_worker        (HTTP-Anfragen, externe APIs)
DELEGATE:vision_worker     (Bilder analysieren)

Mehrere Spezialisten (wenn Frage mehrere Bereiche betrifft):
CHAIN:calendar_worker,tasks_worker      (z.B. "Termine und Aufgaben heute")
CHAIN:calendar_worker,contacts_worker   (z.B. "Wann treffe ich mich mit Anna?")

Geplante Jobs: SCHEDULE:0 9 * * 1|job_id|Beschreibung

Verfügbare Worker:
{workers}

Aktueller Kontext:
{context}"""


class Orchestrator(BaseAgent):
    name = "ida"
    description = "IDA - Hauptkoordinatorin"

    def __init__(self):
        super().__init__()
        self.model = config.MAIN_MODEL
        self.workers: dict[str, BaseAgent] = {}
        self.conversation_history: dict[int, list] = {}
        self.longterm_memory: dict[int, list] = {}
        self.context_store = ContextStore()
        self._load_history()
        self._load_memory()

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

    def _load_memory(self):
        mem_file = os.path.join(config.DATA_DIR, "longterm_memory.json")
        if os.path.exists(mem_file):
            try:
                with open(mem_file, "r", encoding="utf-8") as f:
                    self.longterm_memory = {int(k): v for k, v in json.load(f).items()}
            except Exception as e:
                logger.warning(f"Gedächtnis konnte nicht geladen werden: {e}")

    def _save_memory(self):
        mem_file = os.path.join(config.DATA_DIR, "longterm_memory.json")
        try:
            with open(mem_file, "w", encoding="utf-8") as f:
                json.dump(self.longterm_memory, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Gedächtnis konnte nicht gespeichert werden: {e}")

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

            user_memory = self.longterm_memory.get(user_id, [])
            memory_str = "\n".join(f"- {m}" for m in user_memory) if user_memory else "Noch keine Fakten gespeichert."
            context_text = ctx.as_prompt_text()
            system = SYSTEM_PROMPT.format(
                workers=self._workers_description(),
                context=context_text,
                memory=memory_str,
            )
            
            vision_resp = await self.workers["vision_worker"].process(message)
            ctx.set("letztes_bild_analyse", vision_resp.content, worker="vision_worker")
            
            final = await self._summarize(message.content, "vision_worker", vision_resp.content, ctx, system)
            self._append_history(user_id, history, final)
            return AgentResponse(content=final)

        if user_id not in self.conversation_history:
            self.conversation_history[user_id] = []
        history = self.conversation_history[user_id]
        history.append({"role": "user", "content": message.content})

        user_memory = self.longterm_memory.get(user_id, [])
        memory_str = "\n".join(f"- {m}" for m in user_memory) if user_memory else "Noch keine Fakten gespeichert."

        context_text = ctx.as_prompt_text()
        system = SYSTEM_PROMPT.format(
            workers=self._workers_description(),
            context=context_text,
            memory=memory_str
        )
        raw = await self._chat(messages=history, system=system, num_predict=150)

        import re

        # ── REMEMBER: Fakten speichern ──────────────────────────────────────
        rem_matches = re.findall(r'(REMEMBER:\s*([^\n]+))', raw)
        if rem_matches:
            if user_id not in self.longterm_memory:
                self.longterm_memory[user_id] = []
            for full_match, fact in rem_matches:
                fact = fact.strip()
                if fact and fact not in self.longterm_memory[user_id]:
                    self.longterm_memory[user_id].append(fact)
                raw = raw.replace(full_match, "").strip()
            self._save_memory()
            if not raw:
                raw = "Alles klar, das habe ich mir dauerhaft gemerkt!"

        # ── CHAIN: mehrere Worker nacheinander ──────────────────────────────
        chain_match = re.search(r'(CHAIN:[^\n]+)', raw)
        if chain_match:
            return await self._handle_chain(chain_match.group(1), message, user_id, history, ctx, system)

        # ── DELEGATE: einzelner Worker ──────────────────────────────────────
        del_match = re.search(r'(DELEGATE:[^\n]+)', raw)
        if del_match:
            return await self._handle_delegate(del_match.group(1), message, user_id, history, ctx, system)

        # ── SCHEDULE: wiederkehrender Task ──────────────────────────────────
        sched_match = re.search(r'(SCHEDULE:[^\n]+)(?:\n(.*))?', raw, re.DOTALL)
        if sched_match:
            sched_cmd = sched_match.group(1)
            user_message = sched_match.group(2).strip() if sched_match.group(2) else "Aufgabe wurde geplant."
            self._append_history(user_id, history, user_message)
            return AgentResponse(
                content=user_message,
                metadata={"schedule_command": sched_cmd},
            )

        self._append_history(user_id, history, raw)
        return AgentResponse(content=raw)

    # ── Delegation ───────────────────────────────────────────────────────────

    async def _handle_delegate(
        self, raw: str, message: AgentMessage,
        user_id: int, history: list, ctx: SharedContext, system: str,
    ) -> AgentResponse:
        worker_name = raw[9:].split(":")[0].strip()
        if worker_name not in self.workers:
            fallback = f"Unbekannter Worker: {worker_name}"
            self._append_history(user_id, history, fallback)
            return AgentResponse(content=fallback)

        logger.info(f"Delegiere an {worker_name}: {message.content[:80]}")
        worker_msg = AgentMessage(
            content=self._enrich_task(message.content, ctx),
            metadata={**message.metadata, "shared_context": ctx},
        )
        worker_resp = await self.workers[worker_name].process(worker_msg)
        ctx.set(f"{worker_name}_ergebnis", worker_resp.content, worker=worker_name)

        self._append_history(user_id, history, worker_resp.content)
        return AgentResponse(content=worker_resp.content)

    async def _handle_chain(
        self, raw: str, message: AgentMessage,
        user_id: int, history: list, ctx: SharedContext, system: str,
    ) -> AgentResponse:
        rest = raw[6:]  # nach "CHAIN:"
        worker_names = [w.strip() for w in rest.split(":")[0].split(",")]
        all_results: list[tuple[str, str]] = []

        for worker_name in worker_names:
            if worker_name not in self.workers:
                logger.warning(f"Chain: Worker '{worker_name}' nicht gefunden, überspringe")
                continue
            logger.info(f"Chain-Schritt: {worker_name}")
            chain_input = message.content
            if all_results:
                prev = "\n\n".join(f"[{n}]\n{r}" for n, r in all_results)
                chain_input = f"{message.content}\n\nBereits ermittelt:\n{prev}"
            worker_msg = AgentMessage(
                content=self._enrich_task(chain_input, ctx),
                metadata={**message.metadata, "shared_context": ctx},
            )
            worker_resp = await self.workers[worker_name].process(worker_msg)
            all_results.append((worker_name, worker_resp.content))
            ctx.set(f"{worker_name}_ergebnis", worker_resp.content, worker=worker_name)

        combined = "\n\n".join(r for _, r in all_results)
        self._append_history(user_id, history, combined)
        return AgentResponse(content=combined)

    # ── Hilfsmethoden ────────────────────────────────────────────────────────

    @staticmethod
    def _enrich_task(task: str, ctx: SharedContext) -> str:
        ctx_text = ctx.as_prompt_text()
        if ctx_text:
            return f"{task}\n\n{ctx_text}"
        return task

    def _append_history(self, user_id: int, history: list, response: str):
        history.append({"role": "assistant", "content": response})
        if len(history) > 10:
            self.conversation_history[user_id] = history[-10:]
        self._save_history()
