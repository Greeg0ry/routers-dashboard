import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import alerts, collector, config, db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx logs full request URLs at INFO, which would put the bot token in the journal
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncssh").setLevel(logging.WARNING)
log = logging.getLogger("monitor")

STATIC = config.BASE_DIR / "static"
hasher = PasswordHasher()

if not config.SESSION_SECRET or not config.ADMIN_PASSWORD_HASH:
    raise SystemExit("SESSION_SECRET and ADMIN_PASSWORD_HASH must be set")


@asynccontextmanager
async def lifespan(app):
    tasks = []
    if config.COLLECTOR_ENABLED:
        tasks = [asyncio.create_task(collector.loop()), asyncio.create_task(alerts.sender_loop())]
    else:
        collector.state["history"] = collector.overview_history()
        collector.state["last_cycle"] = (db.one("SELECT MAX(ts) AS ts FROM samples")["ts"] or 0)
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="monit_session",
    max_age=30 * 86400, same_site="lax", https_only=config.COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/") or request.url.path in ("/", "/login"):
        response.headers["Cache-Control"] = "no-store"
    return response


# --- auth ----------------------------------------------------------------------

_failures: dict[str, list[float]] = {}
MAX_FAILURES, FAILURE_WINDOW = 5, 300


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def require_user(request: Request):
    if not request.session.get("user"):
        raise HTTPException(401, "unauthorized")


def require_write(request: Request):
    require_user(request)
    # a cross-site form cannot set a custom header, so this blocks CSRF
    if request.headers.get("x-requested-with") != "monit":
        raise HTTPException(403, "forbidden")


@app.post("/api/login")
async def login(request: Request):
    ip = client_ip(request)
    recent = [t for t in _failures.get(ip, []) if t > time.time() - FAILURE_WINDOW]
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Слишком много попыток. Подождите 5 минут.")
    try:
        body = await request.json()
        username, password = str(body["username"]), str(body["password"])
    except Exception:
        raise HTTPException(400, "bad request")
    ok = hmac.compare_digest(username.encode(), config.ADMIN_USER.encode())
    try:
        hasher.verify(config.ADMIN_PASSWORD_HASH, password)
    except (VerificationError, Exception):
        ok = False
    if not ok:
        _failures[ip] = recent + [time.time()]
        log.warning("failed login from %s", ip)
        raise HTTPException(401, "Неверный логин или пароль")
    _failures.pop(ip, None)
    request.session["user"] = config.ADMIN_USER
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/")
async def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", 302)
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", 302)
    return FileResponse(STATIC / "login.html")


# --- api -----------------------------------------------------------------------

def _device(row, checks):
    return {
        "id": row["id"], "name": row["name"], "hostname": row["hostname"], "ip": row["ip"],
        "online": bool(row["online"]), "last_seen": row["last_seen"], "muted": bool(row["muted"]),
        "probe_ts": row["probe_ts"], "probe_error": row["probe_error"], "info": db.loads(row["info"]),
        "agent": collector.agent_fresh(row["id"]),
        "checks": {
            c["name"]: {"status": c["status"], "detail": c["detail"], "since": c["since"],
                        "down": bool(c["down"]), "muted": bool(c["muted"])}
            for c in checks
        },
    }


def _events(rows):
    return [{"ts": r["ts"], "device_id": r["device_id"], "device": r["device_name"], "check": r["check_name"],
             "kind": r["kind"], "detail": r["detail"], "duration": r["duration"]} for r in rows]


@app.get("/api/overview", dependencies=[Depends(require_user)])
async def overview():
    by_device = {}
    for c in db.q("SELECT * FROM checks"):
        by_device.setdefault(c["device_id"], []).append(c)
    devices = [_device(r, by_device.get(r["id"], []))
               for r in db.q("SELECT * FROM devices WHERE present = 1 ORDER BY name")]
    history = collector.state["history"]
    for d in devices:
        d["history"] = history.get(d["id"], [])
    return {
        "now": db.now(),
        "last_cycle": collector.state["last_cycle"],
        "running": collector.state["running"],
        "error": collector.state["error"],
        "interval": config.POLL_INTERVAL,
        "checks": collector.CHECKS,
        "devices": devices,
        "events": _events(db.q("SELECT * FROM events ORDER BY id DESC LIMIT 40")),
    }


@app.get("/api/devices/{device_id}", dependencies=[Depends(require_user)])
async def device_detail(device_id: str, hours: int = 24):
    hours = hours if hours in (24, 168, 720) else 24
    row = db.one("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not row:
        raise HTTPException(404, "not found")
    device = _device(row, db.q("SELECT * FROM checks WHERE device_id = ?", (device_id,)))
    device["history"] = collector.device_history(device_id, hours)
    device["agent_command"] = collector.install_command(device_id)
    device["events"] = _events(db.q("SELECT * FROM events WHERE device_id = ? ORDER BY id DESC LIMIT 50", (device_id,)))
    return device


@app.post("/api/devices/{device_id}/mute", dependencies=[Depends(require_write)])
async def mute(device_id: str, request: Request):
    body = await request.json()
    muted = 1 if body.get("muted") else 0
    check = body.get("check")
    if check:
        if check not in collector.CHECKS:
            raise HTTPException(400, "bad check")
        db.x("UPDATE checks SET muted = ? WHERE device_id = ? AND name = ?", (muted, device_id, check))
    else:
        db.x("UPDATE devices SET muted = ? WHERE id = ?", (muted, device_id))
    return {"ok": True}


@app.post("/api/devices/{device_id}/probe", dependencies=[Depends(require_write)])
async def probe(device_id: str):
    if not await collector.probe_now(device_id):
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.post("/api/refresh", dependencies=[Depends(require_write)])
async def refresh():
    collector.wake.set()
    return {"ok": True}


# --- router agent (authenticated by per-device token, not by session) -----------

@app.post("/api/ingest")
async def ingest(request: Request):
    device_id = request.headers.get("x-device", "")
    if not collector.agent_auth(device_id, request.headers.get("x-token", "")):
        raise HTTPException(403, "forbidden")
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(413, "too large")
    if not collector.ingest(device_id, body.decode("utf-8", "replace")):
        raise HTTPException(400, "bad report")
    return PlainTextResponse("ok\n", headers={"X-Probe-Sha": collector.PROBE_SHA})


@app.get("/api/agent/probe")
async def agent_probe(request: Request):
    if not collector.agent_auth(request.headers.get("x-device", ""), request.headers.get("x-token", "")):
        raise HTTPException(403, "forbidden")
    return PlainTextResponse(collector.PROBE)


@app.get("/api/agent/install/{device_id}/{token}")
async def agent_install(device_id: str, token: str):
    if not collector.agent_auth(device_id, token):
        raise HTTPException(403, "forbidden")
    return PlainTextResponse(collector.install_script(device_id))


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
