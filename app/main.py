import asyncio
import hashlib
import hmac
import html
import logging
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from itsdangerous import BadData, URLSafeTimedSerializer
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.middleware.sessions import SessionMiddleware

from . import alerts, bot, claude, collector, config, db, fixer, singbox, updater

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
        fixer.recover()
        updater.recover()
        singbox.recover()
        tasks = [asyncio.create_task(collector.loop()), asyncio.create_task(alerts.sender_loop()),
                 asyncio.create_task(bot.loop())]
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


LUCI_HOST = urlsplit(config.LUCI_URL).hostname
LUCI_PATHS = ("/luci/", "/cgi-bin/", "/luci-static/", "/ubus")


def on_luci_host(request: Request) -> bool:
    return bool(LUCI_HOST) and request.headers.get("host", "").split(":")[0].lower() == LUCI_HOST


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if on_luci_host(request) and not (request.url.path == "/enter" or request.url.path.startswith(LUCI_PATHS)):
        # the LuCI address serves routers' pages and nothing of the dashboard itself
        return PlainTextResponse("not found\n", status_code=404)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if config.COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith("/luci/"):
        # LuCI needs inline scripts, and its stray absolute requests are routed by Referer
        response.headers["Referrer-Policy"] = "same-origin"
        return response
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/") or request.url.path in ("/", "/login"):
        response.headers["Cache-Control"] = "no-store"
    return response


# --- auth ----------------------------------------------------------------------

_failures: dict[str, list[float]] = {}
_lock_alerted: dict[str, float] = {}
MAX_FAILURES, FAILURE_WINDOW = 5, 300
# Sessions are signed cookies, so they cannot be revoked one by one; binding them to the
# password hash makes a password change (or a new SESSION_SECRET) sign everybody out.
SESSION_VERSION = hashlib.sha256((config.ADMIN_PASSWORD_HASH + config.SESSION_SECRET).encode()).hexdigest()[:16]


def authed(request: Request) -> bool:
    session = request.session
    return bool(session.get("user")) and session.get("v") == SESSION_VERSION


def _notify(text):
    """Security events go to the owner's Telegram through the normal outbox."""
    db.x("INSERT INTO outbox (ts, text) VALUES (?, ?)", (db.now(), text))


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def require_user(request: Request):
    if not authed(request):
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
        if time.time() - _lock_alerted.get(ip, 0) > 3600:
            _lock_alerted[ip] = time.time()
            _notify(f"🚫 <b>Подбор пароля к дашборду</b>\n{MAX_FAILURES} неудачных попыток входа с адреса "
                    f"<code>{html.escape(ip)}</code>, вход с него закрыт на 5 минут.")
        raise HTTPException(429, "Слишком много попыток. Подождите 5 минут.")
    if int(request.headers.get("content-length") or 0) > 4096:
        raise HTTPException(413, "too large")
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
    request.session.update(user=config.ADMIN_USER, v=SESSION_VERSION, t=int(time.time()))
    agent = request.headers.get("user-agent", "")[:120]
    _notify(f"🔑 <b>Вход в дашборд</b>\nАдрес <code>{html.escape(ip)}</code>\n{html.escape(agent)}\n"
            "Если это не вы — смените пароль: все сеансы закроются.")
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/")
async def index(request: Request):
    if not authed(request):
        return RedirectResponse("/login", 302)
    return FileResponse(STATIC / "index.html")


@app.get("/login")
async def login_page(request: Request):
    if authed(request):
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
        "forkop": updater.overview(),
        "singbox": singbox.overview(),
        "claude": bool((await claude.auth_status()).get("loggedIn")) if config.COLLECTOR_ENABLED else False,
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
    device["job"] = _job(fixer.for_device(device_id))
    device["forkop_update"] = updater.last_for(device_id)
    device["singbox"] = singbox.state(row)
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


# --- repairs -------------------------------------------------------------------

def _job(job):
    if not job:
        return None
    return {k: job[k] for k in ("id", "ts", "updated", "stage", "request", "error", "plan", "results", "usage")}


@app.post("/api/devices/{device_id}/fix", dependencies=[Depends(require_write)])
async def fix(device_id: str):
    if not (await claude.auth_status()).get("loggedIn"):
        raise HTTPException(409, "Claude не авторизован: отправьте боту /login")
    job_id, error = fixer.submit_device(device_id)
    if error:
        raise HTTPException(409, error)
    return {"job": job_id}


@app.post("/api/jobs/{job_id}/{action}", dependencies=[Depends(require_write)])
async def job_action(job_id: int, action: str):
    if action == "confirm":
        done = fixer.confirm(job_id)
    elif action == "cancel":
        done = fixer.cancel(job_id)
    elif action == "deepen":
        done = fixer.get(job_id) is not None and fixer.deepen(job_id) is not None
    else:
        raise HTTPException(404, "not found")
    if not done:
        raise HTTPException(409, "задача уже неактуальна")
    return {"ok": True}


@app.post("/api/forkop/update", dependencies=[Depends(require_write)])
async def forkop_update(request: Request):
    """{"devices": [ids]} for chosen routers, {"devices": "all"} for every outdated one."""
    body = await request.json()
    devices = body.get("devices")
    if devices != "all" and not (isinstance(devices, list) and devices):
        raise HTTPException(400, "bad request")
    flags = [f for f in updater.FLAGS if body.get(f)]
    count, error = updater.start(None if devices == "all" else set(map(str, devices)), flags)
    if error:
        raise HTTPException(409, error)
    return {"count": count}


@app.post("/api/forkop/cancel", dependencies=[Depends(require_write)])
async def forkop_cancel():
    return {"skipped": updater.cancel()}


@app.post("/api/devices/{device_id}/singbox", dependencies=[Depends(require_write)])
async def singbox_action(device_id: str, request: Request):
    """{"action": "check" | "update" | "x" | "extended" | "compressed"}"""
    error = singbox.start(device_id, str((await request.json()).get("action")))
    if error:
        raise HTTPException(409, error)
    return {"ok": True}


@app.post("/api/singbox/update", dependencies=[Depends(require_write)])
async def singbox_update(request: Request):
    """{} updates sing-box wherever a newer version is waiting; {"build": "x" | "extended" | "compressed"}
    installs that build on every router that has another one."""
    count, error = singbox.start_all(str((await request.json()).get("build") or "update"))
    if error:
        raise HTTPException(409, error)
    return {"count": count}


@app.post("/api/singbox/cancel", dependencies=[Depends(require_write)])
async def singbox_cancel():
    return {"skipped": singbox.cancel()}


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


# --- LuCI proxy ------------------------------------------------------------------
# /luci/<device id>/... is fetched from the router's web interface over tailscale,
# so the admin pages open in the browser without joining the tailnet. LuCI builds
# its URLs from absolute paths; they get the prefix in HTML, redirects and cookies.

# With LUCI_URL set, the dashboard only hands the browser over: /luci/<id>/ here answers
# with a redirect to <LUCI_URL>/enter?t=<one-time ticket>, and that address keeps its own
# cookie. The cookie names one router, the one opened last, so a page from router A can
# reach only router A through the proxy.
LUCI_COOKIE, LUCI_TICKET_SECONDS, LUCI_SESSION_SECONDS = "luci_session", 60, 4 * 3600
_tickets = URLSafeTimedSerializer(config.SESSION_SECRET, salt="luci-ticket")
_luci_cookies = URLSafeTimedSerializer(config.SESSION_SECRET, salt="luci-cookie")
_used_tickets: dict[str, float] = {}


def luci_allowed(request: Request, device_id: str) -> bool:
    """May this request reach this router's LuCI?"""
    if not LUCI_HOST:
        return authed(request)
    if not on_luci_host(request):
        return False
    try:
        data = _luci_cookies.loads(request.cookies.get(LUCI_COOKIE, ""), max_age=LUCI_SESSION_SECONDS)
    except BadData:
        return False
    return data.get("d") == device_id and data.get("v") == SESSION_VERSION


@app.get("/enter")
async def luci_enter(request: Request, t: str = ""):
    """Trades the dashboard's one-time ticket for this address's own cookie."""
    now = time.time()
    for ticket in [k for k, expires in _used_tickets.items() if expires < now]:
        del _used_tickets[ticket]
    try:
        data = _tickets.loads(t, max_age=LUCI_TICKET_SECONDS)
    except BadData:
        data = None
    if not on_luci_host(request) or not data or t in _used_tickets or data.get("v") != SESSION_VERSION:
        raise HTTPException(403, "forbidden")
    _used_tickets[t] = now + LUCI_TICKET_SECONDS
    response = RedirectResponse(f"/luci/{data['d']}/", 302)
    response.set_cookie(LUCI_COOKIE, _luci_cookies.dumps({"d": data["d"], "v": SESSION_VERSION}),
                        max_age=LUCI_SESSION_SECONDS, httponly=True, secure=config.COOKIE_SECURE, samesite="lax")
    return response


LUCI_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE"]
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
        "transfer-encoding", "upgrade"}
_REQUEST_DROP = _HOP | {"host", "cookie", "origin", "referer", "accept-encoding", "x-real-ip",
                        "x-forwarded-for", "x-forwarded-proto"}
_RESPONSE_DROP = _HOP | {"content-length", "content-encoding", "location", "set-cookie"}
_LUCI_ABS = re.compile(rb"""(["'=(]\s*)(\\?/)(cgi-bin|luci-static|ubus)(?=[\\/"'?])""")
_luci_client = httpx.AsyncClient(verify=False, timeout=httpx.Timeout(120, connect=15))
_luci_scheme: dict[str, str] = {}


def _luci_location(value: str, ip: str, prefix: str) -> str:
    target = urlsplit(value)
    if target.hostname == ip or (not target.netloc and value.startswith("/")):
        return prefix + (target.path or "/") + (f"?{target.query}" if target.query else "")
    return value


@app.api_route("/luci/{device_id}", methods=["GET"])
async def luci_root(device_id: str):
    return RedirectResponse(f"/luci/{device_id}/", 302)


@app.api_route("/luci/{device_id}/{path:path}", methods=LUCI_METHODS)
async def luci(device_id: str, path: str, request: Request):
    if LUCI_HOST and not on_luci_host(request):
        # the dashboard's own address never serves a router's page
        if request.method != "GET":
            raise HTTPException(404, "not found")
        if not authed(request):
            return RedirectResponse("/login", 302)
        if not db.one("SELECT 1 FROM devices WHERE id = ? AND present = 1", (device_id,)):
            raise HTTPException(404, "not found")
        ticket = _tickets.dumps({"d": device_id, "v": SESSION_VERSION})
        return RedirectResponse(f"{config.LUCI_URL}/enter?t={ticket}", 302)
    if not luci_allowed(request, device_id):
        if LUCI_HOST:
            return PlainTextResponse("Сеанс истёк или открыт другой роутер. Откройте LuCI заново из дашборда: "
                                     f"{config.PUBLIC_URL}\n", status_code=403)
        if request.method == "GET":
            return RedirectResponse("/login", 302)
        raise HTTPException(401, "unauthorized")
    row = db.one("SELECT ip FROM devices WHERE id = ? AND present = 1", (device_id,))
    if not row or not row["ip"]:
        raise HTTPException(404, "not found")
    ip, prefix = row["ip"], f"/luci/{device_id}"
    raw = request.scope.get("raw_path", b"").decode("latin-1")
    upstream_path = raw[len(prefix):] if raw.startswith(prefix + "/") else "/" + path
    scheme = _luci_scheme.get(device_id, "http")
    url = f"{scheme}://{ip}{upstream_path}" + (f"?{request.url.query}" if request.url.query else "")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _REQUEST_DROP}
    # neither the dashboard session nor the proxy's own cookie may reach a router
    cookies = [c for c in request.headers.get("cookie", "").split("; ")
               if c and not c.startswith(("monit_session=", LUCI_COOKIE + "="))]
    if cookies:
        headers["cookie"] = "; ".join(cookies)
    headers["accept-encoding"] = "identity"
    body = request.stream() if request.method in ("POST", "PUT") else None
    if body is not None and upstream_path.startswith("/ubus"):
        # LuCI pages find their own files on the router by a path built from the URL they
        # were loaded from ("/www" + "/luci/<id>/luci-static/..."); the router has no such
        # directory, the list comes back empty and e.g. the status overview stays blank.
        body = (await request.body()).replace(b"/www" + prefix.encode(), b"/www")
        headers.pop("content-length", None)
    try:
        upstream = await _luci_client.send(
            _luci_client.build_request(request.method, url, headers=headers, content=body), stream=True)
    except httpx.HTTPError as e:
        return PlainTextResponse(f"Роутер не отвечает: {type(e).__name__}\n", status_code=502)

    out = [(k, v) for k, v in upstream.headers.multi_items() if k.lower() not in _RESPONSE_DROP]
    location = upstream.headers.get("location")
    if location:
        if scheme == "http" and location.startswith(f"https://{ip}"):
            # LuCI redirects to HTTPS on this router; talk to it that way from now on
            _luci_scheme[device_id] = "https"
        out.append(("location", _luci_location(location, ip, prefix)))
    for cookie in upstream.headers.get_list("set-cookie"):
        cookie = re.sub(r"(?i);\s*domain=[^;]*", "", cookie)
        out.append(("set-cookie", re.sub(r"(?i)(;\s*path=)/", rf"\1{prefix}/", cookie)))

    if upstream.headers.get("content-type", "").startswith("text/html"):
        try:
            content = await upstream.aread()
        except httpx.HTTPError as e:
            return PlainTextResponse(f"Роутер не отвечает: {type(e).__name__}\n", status_code=502)
        finally:
            await upstream.aclose()
        mark = prefix.encode()
        content = _LUCI_ABS.sub(lambda m: m[1] + mark.replace(b"/", m[2]) + m[2] + m[3], content)
        response = Response(content, status_code=upstream.status_code)
    else:
        length = upstream.headers.get("content-length")
        if length:
            out.append(("content-length", length))
        response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                     background=BackgroundTask(upstream.aclose))
    del response.headers["content-type"]
    for k, v in out:
        response.headers.append(k, v)
    return response


async def luci_stray(request: Request):
    """An absolute LuCI URL that escaped rewriting: send it back under the prefix of the page it came from."""
    origin = re.match(r"/luci/([^/]+)(?=/)", urlsplit(request.headers.get("referer", "")).path)
    if not origin or not luci_allowed(request, origin[1]):
        raise HTTPException(404, "not found")
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(origin[0] + request.scope.get("raw_path", b"").decode("latin-1") + query, 307)


for _path in ("/cgi-bin/{rest:path}", "/luci-static/{rest:path}", "/ubus", "/ubus/{rest:path}"):
    app.add_api_route(_path, luci_stray, methods=LUCI_METHODS)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
