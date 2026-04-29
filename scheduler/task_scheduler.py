import json
import logging
import os
from datetime import datetime
from typing import Callable, Awaitable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import config

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
        self.jobs: dict = {}
        self._send_message_callback: Callable[[int, str], Awaitable] = None
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self._load_jobs_metadata()

    def set_send_callback(self, callback: Callable[[int, str], Awaitable]):
        """Telegram-Sendefunktion registrieren, damit Jobs Nachrichten schicken können."""
        self._send_message_callback = callback

    def start(self):
        self.scheduler.start()
        logger.info("Scheduler gestartet")

    def stop(self):
        self.scheduler.shutdown(wait=False)

    def _is_night(self) -> bool:
        hour = datetime.now().hour
        return config.NIGHT_START_HOUR <= hour < config.NIGHT_END_HOUR

    def add_recurring_job(
        self,
        job_id: str,
        cron_expression: str,
        chat_id: int,
        message: str,
        description: str = "",
        priority: str = "low",
    ):
        """Wiederkehrenden Job hinzufügen. Niedrig-Prio Jobs laufen nur nachts."""

        async def execute():
            if priority == "low" and not self._is_night():
                logger.debug(f"Job '{job_id}' übersprungen (nicht Nachtzeit, Prio: low)")
                return
            if self._send_message_callback:
                await self._send_message_callback(chat_id, f"[Geplante Aufgabe] {message}")

        trigger = CronTrigger.from_crontab(cron_expression, timezone="Europe/Berlin")
        self.scheduler.add_job(execute, trigger, id=job_id, replace_existing=True)

        self.jobs[job_id] = {
            "id": job_id,
            "type": "recurring",
            "cron": cron_expression,
            "chat_id": chat_id,
            "message": message,
            "description": description,
            "priority": priority,
        }
        self._save_jobs()
        logger.info(f"Wiederkehrender Job hinzugefügt: {job_id} ({cron_expression})")

    def add_one_time_job(
        self,
        job_id: str,
        run_at: datetime,
        chat_id: int,
        message: str,
        description: str = "",
    ):
        """Einmaligen Job zu einem bestimmten Zeitpunkt hinzufügen."""

        async def execute():
            if self._send_message_callback:
                await self._send_message_callback(chat_id, f"[Erinnerung] {message}")
            self.jobs.pop(job_id, None)
            self._save_jobs()

        trigger = DateTrigger(run_date=run_at, timezone="Europe/Berlin")
        self.scheduler.add_job(execute, trigger, id=job_id, replace_existing=True)

        self.jobs[job_id] = {
            "id": job_id,
            "type": "one_time",
            "run_at": run_at.isoformat(),
            "chat_id": chat_id,
            "message": message,
            "description": description,
        }
        self._save_jobs()
        logger.info(f"Einmaliger Job hinzugefügt: {job_id} um {run_at}")

    def remove_job(self, job_id: str) -> bool:
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        existed = job_id in self.jobs
        self.jobs.pop(job_id, None)
        self._save_jobs()
        return existed

    def list_jobs(self) -> list:
        return list(self.jobs.values())

    def _save_jobs(self):
        with open(config.JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.jobs, f, indent=2, ensure_ascii=False)

    def _load_jobs_metadata(self):
        if os.path.exists(config.JOBS_FILE):
            try:
                with open(config.JOBS_FILE, encoding="utf-8") as f:
                    self.jobs = json.load(f)
                logger.info(f"{len(self.jobs)} gespeicherte Jobs geladen")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Jobs konnten nicht geladen werden: {e}")
                self.jobs = {}
        else:
            self.jobs = {}

    def restore_jobs_after_start(self):
        """Wiederkehrende Jobs nach Neustart aus jobs.json wiederherstellen."""
        for job in list(self.jobs.values()):
            if job["type"] == "recurring":
                self.add_recurring_job(
                    job_id=job["id"],
                    cron_expression=job["cron"],
                    chat_id=job["chat_id"],
                    message=job["message"],
                    description=job.get("description", ""),
                    priority=job.get("priority", "low"),
                )
            elif job["type"] == "one_time":
                run_at = datetime.fromisoformat(job["run_at"])
                if run_at > datetime.now():
                    self.add_one_time_job(
                        job_id=job["id"],
                        run_at=run_at,
                        chat_id=job["chat_id"],
                        message=job["message"],
                        description=job.get("description", ""),
                    )
                else:
                    self.jobs.pop(job["id"], None)
        self._save_jobs()
        logger.info("Jobs nach Neustart wiederhergestellt")
