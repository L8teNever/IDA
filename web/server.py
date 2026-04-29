"""
IDA Web-Interface - läuft auf Port 8080
Erreichbar unter: http://localhost:8080
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

from agents.workers._google_base import GOOGLE_GOOGLE_SCOPES
GOOGLE_SCOPES = GOOGLE_GOOGLE_SCOPES
GOOGLE_CALLBACK_URL = f"http://localhost:{config.WEB_PORT}/auth/google/callback"

app = FastAPI(title="IDA Setup")
_oauth_flows: dict[str, object] = {}


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _read_env() -> dict:
    env = {}
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict):
    env_path = Path(".env")
    current = _read_env()
    current.update(updates)
    lines = [f"{k}={v}" for k, v in current.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_untis_config() -> dict:
    if os.path.exists(config.UNTIS_CONFIG_FILE):
        try:
            with open(config.UNTIS_CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_untis_config(data: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.UNTIS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def _ollama_status() -> bool:
    try:
        async with httpx.AsyncClient(base_url=config.OLLAMA_URL, timeout=3) as c:
            await c.get("/api/tags")
        return True
    except Exception:
        return False


def _google_status() -> dict:
    creds_ok = os.path.exists(os.path.join(config.DATA_DIR, "google_credentials.json"))
    token_ok = os.path.exists(os.path.join(config.DATA_DIR, "google_token.json"))
    connected = False
    if token_ok:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(
                os.path.join(config.DATA_DIR, "google_token.json"), GOOGLE_SCOPES
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            connected = creds.valid
        except Exception:
            pass
    return {"creds_file": creds_ok, "token_file": token_ok, "connected": connected}


def _untis_status() -> dict:
    cfg = _read_untis_config()
    configured = bool(cfg.get("server") and cfg.get("username"))
    memory_file = os.path.join(config.DATA_DIR, "memory_untis_worker.json")
    last_check = None
    if os.path.exists(memory_file):
        try:
            mem = json.load(open(memory_file, encoding="utf-8"))
            last_check = mem.get("last_check", "")
            if last_check:
                last_check = last_check[:16].replace("T", " ")
        except Exception:
            pass
    return {"configured": configured, "last_check": last_check}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ollama_ok = await _ollama_status()
    google = _google_status()
    untis = _untis_status()
    telegram_set = bool(config.TELEGRAM_TOKEN)

    job_count = 0
    if os.path.exists(config.JOBS_FILE):
        try:
            job_count = len(json.load(open(config.JOBS_FILE, encoding="utf-8")))
        except Exception:
            pass

    history_file = os.path.join(config.DATA_DIR, "conversation_history.json")
    user_count = 0
    if os.path.exists(history_file):
        try:
            user_count = len(json.load(open(history_file, encoding="utf-8")))
        except Exception:
            pass

    return templates.TemplateResponse("index.html", {
        "request": request,
        "ollama_ok": ollama_ok,
        "google": google,
        "untis": untis,
        "telegram_set": telegram_set,
        "job_count": job_count,
        "user_count": user_count,
        "main_model": config.MAIN_MODEL,
        "worker_model": config.WORKER_MODEL,
        "now": datetime.now().strftime("%d.%m.%Y %H:%M"),
    })


# ── Setup ─────────────────────────────────────────────────────────────────────

@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, saved: str = ""):
    env = _read_env()
    token = env.get("TELEGRAM_TOKEN", "")
    masked = ("*" * (len(token) - 6) + token[-6:]) if len(token) > 6 else ""
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "token_masked": masked,
        "token_set": bool(token),
        "allowed_users": env.get("TELEGRAM_ALLOWED_USERS", ""),
        "main_model": env.get("MAIN_MODEL", "llama3.2:3b"),
        "worker_model": env.get("WORKER_MODEL", "llama3.2:1b"),
        "night_start": env.get("NIGHT_START_HOUR", "2"),
        "night_end": env.get("NIGHT_END_HOUR", "6"),
        "saved": saved == "1",
    })


@app.post("/setup")
async def setup_save(
    telegram_token: str = Form(""),
    allowed_users: str = Form(""),
    main_model: str = Form("llama3.2:3b"),
    worker_model: str = Form("llama3.2:1b"),
    night_start: str = Form("2"),
    night_end: str = Form("6"),
):
    updates: dict = {}
    if telegram_token.strip():
        updates["TELEGRAM_TOKEN"] = telegram_token.strip()
    if allowed_users.strip() != "":
        updates["TELEGRAM_ALLOWED_USERS"] = allowed_users.strip()
    updates.update({
        "MAIN_MODEL": main_model.strip(),
        "WORKER_MODEL": worker_model.strip(),
        "NIGHT_START_HOUR": night_start.strip(),
        "NIGHT_END_HOUR": night_end.strip(),
    })
    _write_env(updates)
    return RedirectResponse("/setup?saved=1", status_code=303)


# ── Google Calendar ───────────────────────────────────────────────────────────

@app.get("/google", response_class=HTMLResponse)
async def google_page(request: Request, error: str = ""):
    return templates.TemplateResponse("google.html", {
        "request": request,
        "status": _google_status(),
        "callback_url": GOOGLE_CALLBACK_URL,
        "error": error,
    })


@app.post("/google/upload-credentials")
async def upload_credentials(credentials_file: UploadFile = File(...)):
    content = await credentials_file.read()
    try:
        json.loads(content)
    except json.JSONDecodeError:
        return RedirectResponse("/google?error=Ungültige+JSON-Datei", status_code=303)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(os.path.join(config.DATA_DIR, "google_credentials.json"), "wb") as f:
        f.write(content)
    return RedirectResponse("/google", status_code=303)


@app.get("/auth/google/start")
async def google_auth_start():
    creds_file = os.path.join(config.DATA_DIR, "google_credentials.json")
    if not os.path.exists(creds_file):
        return RedirectResponse("/google?error=Keine+Zugangsdaten+hochgeladen", status_code=303)
    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_secrets_file(creds_file, scopes=GOOGLE_SCOPES, redirect_uri=GOOGLE_CALLBACK_URL)
        auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
        _oauth_flows[state] = flow
        return RedirectResponse(auth_url)
    except Exception as e:
        return RedirectResponse(f"/google?error={str(e)[:120]}", status_code=303)


@app.get("/auth/google/callback")
async def google_auth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/google?error={error}", status_code=303)
    flow = _oauth_flows.pop(state, None)
    if not flow:
        return RedirectResponse("/google?error=OAuth+State+ungültig", status_code=303)
    try:
        flow.fetch_token(code=code)
        with open(os.path.join(config.DATA_DIR, "google_token.json"), "w") as f:
            f.write(flow.credentials.to_json())
    except Exception as e:
        return RedirectResponse(f"/google?error={str(e)[:120]}", status_code=303)
    return RedirectResponse("/google", status_code=303)


@app.post("/google/disconnect")
async def google_disconnect():
    token = os.path.join(config.DATA_DIR, "google_token.json")
    if os.path.exists(token):
        os.remove(token)
    return RedirectResponse("/google", status_code=303)


# ── WebUntis ──────────────────────────────────────────────────────────────────

@app.get("/untis", response_class=HTMLResponse)
async def untis_page(request: Request, saved: str = "", error: str = ""):
    cfg = _read_untis_config()
    pw = cfg.get("password", "")
    pw_masked = ("*" * (len(pw) - 3) + pw[-3:]) if len(pw) > 3 else ("*" * len(pw))
    return templates.TemplateResponse("untis.html", {
        "request": request,
        "cfg": cfg,
        "pw_masked": pw_masked,
        "pw_set": bool(pw),
        "status": _untis_status(),
        "saved": saved == "1",
        "error": error,
    })


@app.post("/untis")
async def untis_save(
    server: str = Form(""),
    school: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    check_interval: str = Form("30"),
    notify_chat_ids: str = Form(""),
):
    cfg = _read_untis_config()
    cfg["server"] = server.strip()
    cfg["school"] = school.strip()
    cfg["username"] = username.strip()
    if password.strip():
        cfg["password"] = password.strip()
    cfg["check_interval_minutes"] = int(check_interval.strip() or "30")
    cfg["notify_chat_ids"] = [
        int(x.strip()) for x in notify_chat_ids.split(",") if x.strip().lstrip("-").isdigit()
    ]
    _write_untis_config(cfg)
    return RedirectResponse("/untis?saved=1", status_code=303)


@app.post("/untis/test")
async def untis_test():
    cfg = _read_untis_config()
    if not cfg.get("server") or not cfg.get("username"):
        return RedirectResponse("/untis?error=Bitte+zuerst+konfigurieren", status_code=303)
    try:
        from agents.workers.untis_worker import UntisClient
        from datetime import datetime
        async with httpx.AsyncClient() as client:
            uc = UntisClient(cfg["server"], cfg["school"], cfg["username"], cfg["password"])
            await uc.login(client)
            lessons = await uc.get_timetable(client, datetime.now())
            await uc.logout(client)
        return RedirectResponse(
            f"/untis?saved=1&error=Verbindung+OK%2C+{len(lessons)}+Stunden+heute",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(f"/untis?error={str(e)[:150]}", status_code=303)


# ── Jobs ──────────────────────────────────────────────────────────────────────

@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    jobs = []
    if os.path.exists(config.JOBS_FILE):
        try:
            jobs = list(json.load(open(config.JOBS_FILE, encoding="utf-8")).values())
        except Exception:
            pass
    return templates.TemplateResponse("jobs.html", {"request": request, "jobs": jobs})


@app.post("/jobs/delete/{job_id}")
async def delete_job(job_id: str):
    if os.path.exists(config.JOBS_FILE):
        try:
            with open(config.JOBS_FILE, encoding="utf-8") as f:
                jobs = json.load(f)
            jobs.pop(job_id, None)
            with open(config.JOBS_FILE, "w", encoding="utf-8") as f:
                json.dump(jobs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Job löschen fehlgeschlagen: {e}")
    return RedirectResponse("/jobs", status_code=303)


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return {
        "ollama": await _ollama_status(),
        "google": _google_status(),
        "untis": _untis_status(),
        "telegram": bool(config.TELEGRAM_TOKEN),
    }
