import asyncio
import json
import logging
import os
import httpx
import uvicorn
import config
from agents.orchestrator import Orchestrator
from agents.workers.api_worker import APIWorker
from agents.workers.vision_worker import VisionWorker
from agents.workers.calendar_worker import CalendarWorker
from agents.workers.tasks_worker import TasksWorker
from agents.workers.contacts_worker import ContactsWorker
from agents.workers.untis_worker import UntisWorker
from scheduler.task_scheduler import TaskScheduler
from bot.handler import TelegramHandler
from web.server import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def wait_for_ollama(max_retries: int = 40):
    logger.info("Warte auf Ollama...")
    async with httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=5) as client:
        for attempt in range(max_retries):
            try:
                await client.get("/api/tags")
                logger.info("Ollama ist bereit")
                return
            except Exception:
                if attempt % 5 == 0:
                    logger.info(f"Ollama noch nicht bereit ({attempt + 1}/{max_retries})...")
                await asyncio.sleep(3)
    raise RuntimeError("Ollama nicht erreichbar")


async def pull_model(model: str):
    async with httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=600) as client:
        try:
            resp = await client.get("/api/tags")
            available = [m["name"] for m in resp.json().get("models", [])]
            if any(model.split(":")[0] in m for m in available):
                logger.info(f"Modell vorhanden: {model}")
                return
        except Exception as e:
            logger.warning(f"Modellprüfung fehlgeschlagen: {e}")

        logger.info(f"Lade Modell: {model} ...")
        async with client.stream("POST", "/api/pull", json={"name": model}) as r:
            async for line in r.aiter_lines():
                if line and '"status"' in line:
                    logger.debug(f"Pull {model}: {line}")
        logger.info(f"Modell geladen: {model}")


def _get_untis_interval() -> int:
    if config.UNTIS_CONFIG_FILE and os.path.exists(config.UNTIS_CONFIG_FILE):
        try:
            with open(config.UNTIS_CONFIG_FILE, encoding="utf-8") as f:
                return int(json.load(f).get("check_interval_minutes", 30))
        except Exception:
            pass
    return 30


async def run_web_server():
    uv_config = uvicorn.Config(
        app=web_app,
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        log_level="warning",
        loop="none",
    )
    await uvicorn.Server(uv_config).serve()


def _check_google_services() -> list[str]:
    """Synchronous Google API checks — run via asyncio.to_thread."""
    results = []
    try:
        from agents.workers._google_base import get_credentials, build_service
        get_credentials()  # raises RuntimeError if token missing/invalid
    except RuntimeError:
        return [
            "❌ Google Kalender – nicht verbunden",
            "❌ Google Tasks – nicht verbunden",
            "❌ Google Kontakte – nicht verbunden",
        ]
    except Exception as e:
        msg = f"❌ Google – Fehler: {str(e)[:50]}"
        return [msg, msg, msg]

    for label, api, version, call in [
        (
            "Google Kalender", "calendar", "v3",
            lambda svc: svc.calendarList().list(maxResults=1).execute(),
        ),
        (
            "Google Tasks", "tasks", "v1",
            lambda svc: svc.tasklists().list(maxResults=1).execute(),
        ),
        (
            "Google Kontakte", "people", "v1",
            lambda svc: svc.people().connections().list(
                resourceName="people/me", pageSize=1, personFields="names"
            ).execute(),
        ),
    ]:
        try:
            from agents.workers._google_base import build_service
            call(build_service(api, version))
            results.append(f"✅ {label}")
        except Exception as e:
            short = str(e)[:60].replace("\n", " ")
            results.append(f"❌ {label} – {short}")

    return results


async def run_startup_check() -> str:
    logger.info("Starte Dienste-Check...")
    lines = ["IDA gestartet ✓\n\nDienste-Check:"]

    # Ollama (already confirmed running at this point)
    lines.append(f"✅ Ollama ({config.MAIN_MODEL} / {config.WORKER_MODEL})")

    # Google (sync calls in thread to avoid blocking event loop)
    try:
        google_lines = await asyncio.to_thread(_check_google_services)
        lines.extend(google_lines)
    except Exception as e:
        lines.append(f"❌ Google – Check fehlgeschlagen: {e}")

    # WebUntis
    if os.path.exists(config.UNTIS_CONFIG_FILE):
        try:
            with open(config.UNTIS_CONFIG_FILE, encoding="utf-8") as f:
                untis_cfg = json.load(f)
            if untis_cfg.get("server") and untis_cfg.get("username"):
                lines.append("✅ WebUntis – konfiguriert")
            else:
                lines.append("⚠️ WebUntis – unvollständig (Server/Nutzer fehlen)")
        except Exception:
            lines.append("⚠️ WebUntis – Konfigurationsfehler")
    else:
        lines.append("⚠️ WebUntis – nicht konfiguriert")

    lines.append("\nIch bin bereit!")
    report = "\n".join(lines)
    logger.info(f"Dienste-Check abgeschlossen:\n{report}")
    return report


async def _send_startup_report(handler: TelegramHandler, report: str):
    if not config.TELEGRAM_ALLOWED_USERS:
        logger.warning("TELEGRAM_ALLOWED_USERS leer – Startup-Bericht kann nicht gesendet werden")
        return
    for uid in config.TELEGRAM_ALLOWED_USERS:
        try:
            await handler.send_message(uid, report)
        except Exception as e:
            logger.warning(f"Startup-Bericht an {uid} fehlgeschlagen: {e}")


async def watch_telegram_config(orchestrator, scheduler, untis_worker, handler_ref, startup_report: str = ""):
    import os
    current_token = config.TELEGRAM_TOKEN

    async def start_bot(token):
        config.TELEGRAM_TOKEN = token
        h = TelegramHandler(orchestrator, scheduler)
        scheduler.set_send_callback(h.send_message)
        untis_worker.set_send_callback(h.send_message)
        await h.run()
        logger.info("Telegram Bot gestartet")

        interval_min = _get_untis_interval()
        scheduler.scheduler.add_job(
            untis_worker.check_for_changes,
            "interval",
            minutes=interval_min,
            id="untis_background_check",
            replace_existing=True,
        )
        logger.info(f"Untis-Check alle {interval_min} Minuten gestartet")
        return h

    if current_token:
        try:
            handler_ref[0] = await start_bot(current_token)
            if startup_report:
                await _send_startup_report(handler_ref[0], startup_report)
        except Exception as e:
            logger.error(f"Telegram Bot konnte nicht gestartet werden: {e}")
    else:
        logger.warning(f"TELEGRAM_TOKEN nicht gesetzt – Bot inaktiv. Setup: http://localhost:{config.WEB_PORT}/setup")

    while True:
        await asyncio.sleep(5)
        new_token = ""
        env_file = config.USER_ENV if os.path.exists(config.USER_ENV) else ".env"
        if os.path.exists(env_file):
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TELEGRAM_TOKEN="):
                            new_token = line.split("=", 1)[1].strip()
            except Exception:
                pass

        if new_token != current_token:
            logger.info("Telegram Token hat sich geändert. Starte Bot neu...")
            current_token = new_token
            if handler_ref[0]:
                try:
                    await handler_ref[0].stop()
                except Exception as e:
                    logger.error(f"Fehler beim Stoppen des Bots: {e}")
                handler_ref[0] = None

            if current_token:
                try:
                    handler_ref[0] = await start_bot(current_token)
                except Exception as e:
                    logger.error(f"Telegram Bot konnte nicht gestartet werden: {e}")


async def main():
    logger.info("IDA startet...")
    logger.info(f"Web-Interface: http://localhost:{config.WEB_PORT}")

    # Web-Server zuerst starten – unabhängig von allem anderen
    web_task = asyncio.create_task(run_web_server())
    logger.info(f"Web-Interface erreichbar: http://localhost:{config.WEB_PORT}")

    await wait_for_ollama()

    for model in {config.MAIN_MODEL, config.WORKER_MODEL, "moondream"}:
        await pull_model(model)

    # Workers
    orchestrator = Orchestrator()
    orchestrator.register_worker(APIWorker())
    orchestrator.register_worker(VisionWorker())
    orchestrator.register_worker(CalendarWorker())
    orchestrator.register_worker(TasksWorker())
    orchestrator.register_worker(ContactsWorker())
    untis_worker = UntisWorker()
    orchestrator.register_worker(untis_worker)

    # Scheduler
    scheduler = TaskScheduler()
    scheduler.start()
    scheduler.restore_jobs_after_start()

    # Dienste-Check vor dem ersten Telegram-Start
    startup_report = await run_startup_check()

    # Telegram Bot Watchdog (sendet den Bericht nach dem Start)
    handler_ref = [None]
    watch_task = asyncio.create_task(
        watch_telegram_config(orchestrator, scheduler, untis_worker, handler_ref, startup_report)
    )

    try:
        await web_task
    except (KeyboardInterrupt, SystemExit):
        logger.info("IDA wird beendet...")
    finally:
        watch_task.cancel()
        if handler_ref[0]:
            await handler_ref[0].stop()
        scheduler.stop()
        logger.info("IDA beendet.")


if __name__ == "__main__":
    asyncio.run(main())
