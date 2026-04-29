import asyncio
import json
import logging
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
    if config.UNTIS_CONFIG_FILE:
        import os
        if os.path.exists(config.UNTIS_CONFIG_FILE):
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


async def watch_telegram_config(orchestrator, scheduler, untis_worker, handler_ref):
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
        except Exception as e:
            logger.error(f"Telegram Bot konnte nicht gestartet werden: {e}")
    else:
        logger.warning(f"TELEGRAM_TOKEN nicht gesetzt – Bot inaktiv. Setup: http://localhost:{config.WEB_PORT}/setup")

    while True:
        await asyncio.sleep(5)
        new_token = ""
        if os.path.exists(".env"):
            try:
                with open(".env", "r", encoding="utf-8") as f:
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

    # Web-Server IMMER als erstes starten – unabhängig von Telegram oder Ollama
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

    # Telegram Bot Watchdog
    handler_ref = [None]
    watch_task = asyncio.create_task(watch_telegram_config(orchestrator, scheduler, untis_worker, handler_ref))

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
