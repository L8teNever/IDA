import logging
import os
import tempfile
import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from agents.base import AgentMessage
from agents.orchestrator import Orchestrator
from scheduler.task_scheduler import TaskScheduler
import config

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, orchestrator: Orchestrator, scheduler: TaskScheduler):
        self.orchestrator = orchestrator
        self.scheduler = scheduler
        self.app = Application.builder().token(config.TELEGRAM_TOKEN).build()
        self._transcriber = None
        self._setup_handlers()
        self._init_transcriber()

    def _init_transcriber(self):
        try:
            from faster_whisper import WhisperModel
            self._transcriber = WhisperModel("tiny", device="cpu", compute_type="int8")
            logger.info("Whisper tiny Modell geladen (Sprachtranskription bereit)")
        except ImportError:
            logger.warning("faster-whisper nicht installiert – Sprachnachrichten deaktiviert")

    async def _typing_loop(self, chat):
        try:
            while True:
                await chat.send_action("typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("hilfe", self._cmd_help))
        self.app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self.app.add_handler(CommandHandler("deljob", self._cmd_del_job))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self.app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        self.app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self._handle_document))

    def _is_allowed(self, user_id: int) -> bool:
        if not config.TELEGRAM_ALLOWED_USERS:
            return True
        return user_id in config.TELEGRAM_ALLOWED_USERS

    async def send_message(self, chat_id: int, text: str):
        await self.app.bot.send_message(chat_id=chat_id, text=text)

    async def _process_and_reply(self, update: Update, text: str, extra_context: str = None):
        user_id = update.effective_user.id
        combined = f"{extra_context}\n\n{text}" if extra_context else text

        message = AgentMessage(
            content=combined,
            metadata={
                "user_id": user_id,
                "username": update.effective_user.first_name,
                "chat_id": update.effective_chat.id,
            },
        )

        response = await self.orchestrator.process(message)

        if "schedule_command" in response.metadata:
            self._handle_schedule_command(
                schedule_line=response.metadata["schedule_command"],
                chat_id=update.effective_chat.id,
            )

        await update.message.reply_text(response.content)

    def _handle_schedule_command(self, schedule_line: str, chat_id: int):
        try:
            raw = schedule_line.replace("SCHEDULE:", "", 1).strip()
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 3:
                logger.warning(f"Ungültiger Schedule-Befehl: {schedule_line}")
                return
            cron_expr, job_id, description = parts[0], parts[1], parts[2]
            self.scheduler.add_recurring_job(
                job_id=job_id,
                cron_expression=cron_expr,
                chat_id=chat_id,
                message=description,
                description=description,
                priority="low",
            )
            logger.info(f"Job '{job_id}' über IDA erstellt: {cron_expr}")
        except Exception as e:
            logger.error(f"Fehler beim Erstellen des Scheduled Jobs: {e}")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "Hallo! Ich bin IDA, deine persönliche KI-Assistentin.\n"
            "Du kannst mir schreiben, Sprachnachrichten schicken oder Bilder zeigen.\n"
            "/hilfe für alle Befehle."
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "IDA - Befehle:\n\n"
            "/start – Begrüßung\n"
            "/jobs – Alle geplanten Aufgaben anzeigen\n"
            "/deljob <id> – Geplante Aufgabe löschen\n\n"
            "Du kannst mir auch einfach schreiben, Sprachnachrichten oder Bilder schicken!"
        )

    async def _cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        jobs = self.scheduler.list_jobs()
        if not jobs:
            await update.message.reply_text("Keine geplanten Aufgaben.")
            return
        lines = ["Geplante Aufgaben:\n"]
        for job in jobs:
            lines.append(f"• [{job['id']}] {job.get('description', '')}")
            if job.get("cron"):
                lines.append(f"  Zeitplan: {job['cron']}")
            if job.get("run_at"):
                lines.append(f"  Einmalig um: {job['run_at']}")
        await update.message.reply_text("\n".join(lines))

    async def _cmd_del_job(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        if not context.args:
            await update.message.reply_text("Verwendung: /deljob <job_id>")
            return
        job_id = context.args[0]
        found = self.scheduler.remove_job(job_id)
        if found:
            await update.message.reply_text(f"Job '{job_id}' wurde gelöscht.")
        else:
            await update.message.reply_text(f"Job '{job_id}' nicht gefunden.")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        typing_task = asyncio.create_task(self._typing_loop(update.message.chat))
        try:
            await self._process_and_reply(update, update.message.text)
        finally:
            typing_task.cancel()

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._transcriber:
            await update.message.reply_text("Sprachtranskription ist nicht verfügbar.")
            return

        typing_task = asyncio.create_task(self._typing_loop(update.message.chat))
        try:
            voice_file = await update.message.voice.get_file()
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
                await voice_file.download_to_drive(tmp.name)
                tmp_path = tmp.name

            try:
                segments, _ = self._transcriber.transcribe(tmp_path, language="de")
                text = " ".join(seg.text for seg in segments).strip()
                if not text:
                    await update.message.reply_text("Ich konnte die Sprachnachricht nicht transkribieren.")
                    return
                await update.message.reply_text(f'[Gehört: "{text}"]')
                await self._process_and_reply(update, text)
            finally:
                os.unlink(tmp_path)
        finally:
            typing_task.cancel()

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        
        typing_task = asyncio.create_task(self._typing_loop(update.message.chat))
        try:
            photo = update.message.photo[-1]
            photo_file = await photo.get_file()

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                await photo_file.download_to_drive(tmp.name)
                tmp_path = tmp.name

            try:
                import base64
                with open(tmp_path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode()

                caption = update.message.caption or "Was siehst du auf diesem Bild? Beschreibe es."

                vision_context = f"[Bild wurde gesendet. Base64-Daten für Vision-Modell verfügbar]"

                response = await self.orchestrator.process(AgentMessage(
                    content=caption,
                    metadata={
                        "user_id": update.effective_user.id,
                        "username": update.effective_user.first_name,
                        "chat_id": update.effective_chat.id,
                        "image_b64": image_b64,
                        "has_image": True,
                    }
                ))

                if response.metadata.get("schedule_command"):
                    self._handle_schedule_command(
                        schedule_line=response.metadata["schedule_command"],
                        chat_id=update.effective_chat.id,
                    )

                await update.message.reply_text(response.content)
            finally:
                os.unlink(tmp_path)
        finally:
            typing_task.cancel()

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_allowed(update.effective_user.id):
            return
        doc = update.message.document
        await update.message.reply_text(
            f"Dokument empfangen: {doc.file_name} ({doc.mime_type}). "
            "Dokumentverarbeitung ist noch nicht implementiert."
        )

    async def run(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram Bot gestartet und wartet auf Nachrichten")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
