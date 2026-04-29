import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED_USERS = [
    int(x.strip()) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if x.strip()
]

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MAIN_MODEL = os.getenv("MAIN_MODEL", "llama3.2:3b")
WORKER_MODEL = os.getenv("WORKER_MODEL", "llama3.2:1b")

NIGHT_START_HOUR = int(os.getenv("NIGHT_START_HOUR", "2"))
NIGHT_END_HOUR = int(os.getenv("NIGHT_END_HOUR", "6"))

DATA_DIR = os.getenv("DATA_DIR", "./data")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
UNTIS_CONFIG_FILE = os.path.join(DATA_DIR, "untis_config.json")

WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
