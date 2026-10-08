import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from urllib.parse import urlsplit

import asyncssh

from . import alerts, config, db
from .alerts import human

log = logging.getLogger("monitor.collector")

CHECKS = ["reach", "internet", "zapret", "forkop", "google", "youtube", "chatgpt", "discord"]
# Checked as a pool of addresses; a failure counts once it has lasted SERVICE_FAIL_SECONDS.
SERVICES = ("google", "youtube", "chatgpt", "discord")
# A failing parent explains its children, so only the parent is alerted.
PARENT = {
    "internet": "reach",
    "zapret": "reach",
    "forkop": "reach",
    "google": "internet",
    "youtube": "internet",
    "chatgpt": "internet",
    "discord": "internet",
}
PROBE = (config.BASE_DIR / "probe.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
PROBE_SHA = hashlib.sha256(PROBE.encode()).hexdigest()[:16]
AGENT = (config.BASE_DIR / "agent.sh").read_text(encoding="utf-8").replace("\r\n", "\n")

state = {"last_cycle": 0, "running": False, "history": {}, "error": None}
wake = asyncio.Event()
_good_password: dict[str, str] = {}
_auth_retry_at: dict[str, int] = {}
_ssh_fail_since: dict[str, int] = {}


# --- tailscale -----------------------------------------------------------------

def _parse_ts(value):
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp()) if dt.year > 2000 else None


async def tailscale_peers():
    proc = await asyncio.create_subprocess_exec(
        "tailscale", "status", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 20)
    if proc.returncode != 0:
        raise RuntimeError(f"tailscale status: {err.decode(errors='replace')[:200]}")
    peers = []
    for p in json.loads(out).get("Peer", {}).values():
        if p.get("OS") != "linux":
            continue
        # HostName is not unique in this tailnet (many "openwrt-main"), DNSName is.
        name = (p.get("DNSName") or "").split(".")[0] or p.get("HostName", "")
        if not name or name in config.EXCLUDE:
            continue
        ip = next((i for i in p.get("TailscaleIPs", []) if "." in i), None)
        if not ip:
            continue
        online = bool(p.get("Online"))
        peers.append({
            "id": p["ID"],
            "name": name,
            "hostname": p.get("HostName", ""),
            "ip": ip,
            "online": online,
            "last_seen": db.now() if online else _parse_ts(p.get("LastSeen")),
        })
    return peers


# --- ssh -----------------------------------------------------------------------

def _parse_kv(text):
    kv = {}
    for line in text.splitlines():
        key, sep, value = line.partition("\t")
        if sep:
            kv[key] = value.strip()
    return kv


async def ssh_run(dev, command, timeout, input=None):
    """Runs a command on the router. Returns (result, error); error is None, "auth", or a short description."""
    key = [config.SSH_KEY] if os.path.exists(config.SSH_KEY) else None
    passwords = list(config.SSH_PASSWORDS)
    known = _good_password.get(dev["id"])
    if known in passwords:
        passwords.remove(known)
        passwords.insert(0, known)
    for password in passwords or [None]:
        try:
            async with asyncssh.connect(
                dev["ip"], username=config.SSH_USER, password=password, client_keys=key,
                known_hosts=None, agent_path=None, connect_timeout=15, login_timeout=25,
            ) as conn:
                result = await conn.run(command, input=input, timeout=timeout, check=False)
                if password:
                    _good_password[dev["id"]] = password
                return result, None
        except asyncssh.PermissionDenied:
            continue
        except (asyncio.TimeoutError, TimeoutError):
            return None, "таймаут"
        except ConnectionRefusedError:
            return None, "порт закрыт"
        except (OSError, asyncssh.Error) as e:
            return None, (str(e) or type(e).__name__)[:80]
    return None, "auth"


async def ssh_probe(dev):
    """Returns (kv, error). error is None, "auth", or a short description."""
    result, error = await ssh_run(dev, "sh -s", config.PROBE_TIMEOUT, input=PROBE)
    if error:
        return None, error
    kv = _parse_kv(str(result.stdout or ""))
    if not kv:
        return None, "пустой ответ роутера"
    return kv, None


# --- evaluation ----------------------------------------------------------------

def _pool(kv, name):
    """One service pool from the probe: [(host, http_code, ms)]."""
    parts = kv.get("pool_" + name, "").split()
    pool = []
    for url, code, seconds in zip(parts[0::3], parts[1::3], parts[2::3]):
        try:
            ms = int(float(seconds) * 1000)
        except ValueError:
            ms = None
        pool.append((urlsplit(url).hostname or url, code, ms))
    return pool


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _eval_pool(kv, name):
    """Any HTTP answer means the address is reachable; the service needs all of them."""
    pool = _pool(kv, name)
    if not pool:
        return "unknown", "проба не вернула данных"
    dead = [host for host, code, _ in pool if code == "000"]
    if dead:
        return "fail", f"нет соединения ({len(dead)} из {len(pool)}): " + ", ".join(dead)
    return "ok", f"{len(pool)} адр. · {pool[0][2]} мс"


def _eval_sites(kv, res, info):
    if kv.get("curl") != "1":
        for name in ("internet", *SERVICES):
            res[name] = ("unknown", "на роутере нет curl")
        return
    if "pool_internet" not in kv:
        # a router agent still running the previous probe; it updates itself on this report
        for name in ("internet", *SERVICES):
            res[name] = ("unknown", "проба на роутере обновляется")
        return
    ya = _pool(kv, "internet")
    google = _pool(kv, "google")
    if ya and ya[0][1] != "000":
        res["internet"] = ("ok", f"ya.ru · {ya[0][2]} мс")
        info["latency"] = ya[0][2]
    elif any(code != "000" for _, code, _ in google):
        res["internet"] = ("ok", "ya.ru не ответил, google.com отвечает")
    else:
        res["internet"] = ("fail", "ya.ru и google.com не отвечают")

    # Voice (UDP) and the video CDN nodes are not covered by any pool.
    for name in SERVICES:
        res[name] = _eval_pool(kv, name)

    # chatgpt.com itself answers curl with a Cloudflare 403 even when it works,
    # so judge by the API (403 = country blocked) and the exit country.
    api = next((code for host, code, _ in _pool(kv, "chatgpt") if host == "api.openai.com"), "000")
    trace = dict(p.split("=", 1) for p in kv.get("site_gpttrace", "").split() if "=" in p)
    loc = trace.get("loc")
    if loc:
        info["gpt_loc"] = loc
    if res["chatgpt"][0] == "ok":
        if api == "403" or loc == "RU":
            res["chatgpt"] = ("fail", f"страна не поддерживается ({loc or '?'}) — трафик идёт мимо прокси")
        elif loc:
            res["chatgpt"] = ("ok", f"выход через {loc} · {res['chatgpt'][1]}")


def _eval_zapret(kv, res, info):
    variants = [z for z in ("zapret", "zapret2") if kv.get(z + "_installed") == "1"]
    info["zapret"] = variants
    if not variants:
        res["zapret"] = ("na", "не установлен")
        return
    active = [z for z in variants if kv.get(z + "_enabled") == "1" or _int(kv.get(z + "_procs")) > 0]
    if not active:
        res["zapret"] = ("fail", f"{' / '.join(variants)}: служба выключена")
        return
    problems, notes = [], []
    for z in active:
        procs = _int(kv.get(z + "_procs"))
        if procs == 0:
            problems.append(f"{z}: процесс nfqws не запущен")
        if kv.get(z + "_nft") != "1":
            problems.append(f"{z}: нет nft-таблицы")
        note = f"{z} · {procs} проц."
        if kv.get(z + "_enabled") != "1":
            note += " · автозапуск выключен"
        notes.append(note)
    res["zapret"] = ("fail", "; ".join(problems)) if problems else ("ok", ", ".join(notes))


def _eval_forkop(kv, res, info):
    if kv.get("forkop_installed") != "1":
        res["forkop"] = ("na", "не установлен")
        return
    status = db.loads(kv.get("forkop_status"))
    nft = db.loads(kv.get("forkop_nft"))
    fakeip = db.loads(kv.get("forkop_fakeip"))
    version = (kv.get("forkop_version") or "").strip()[:40]
    if version:
        info["forkop_version"] = version

    groups = []
    proxies = db.loads(kv.get("forkop_clash")).get("proxies") or {}
    for name, p in proxies.items():
        if name == "GLOBAL" or p.get("type") not in ("Selector", "URLTest"):
            continue
        current = p.get("now")
        history = (proxies.get(current) or {}).get("history") or []
        groups.append({
            "name": name,
            "now": current,
            "delay": history[-1].get("delay") if history else None,
        })
    info["proxies"] = groups[:8]

    problems = []
    if status and not status.get("running"):
        problems.append("служба остановлена")
    if _int(kv.get("singbox_procs")) == 0:
        problems.append("sing-box не запущен")
    if nft and not nft.get("table_exist"):
        problems.append("нет nft-таблицы")
    if fakeip and fakeip.get("fakeip") is False:
        problems.append("FakeIP DNS не работает")
    if status and status.get("dns_configured") == 0:
        problems.append("DNS не настроен")
    if problems:
        res["forkop"] = ("fail", "; ".join(problems))
        return
    notes = []
    if version:
        notes.append(version)
    if kv.get("forkop_enabled") == "0":
        notes.append("автозапуск выключен")
    if not status:
        notes.append("статус не получен")
    res["forkop"] = ("ok", " · ".join(notes) or "работает")


def evaluate(kv):
    res = {"reach": ("ok", "на связи, опрос по SSH")}
    info = {"source": "ssh", "model": kv.get("model", ""), "release": kv.get("release", ""),
            "uptime": _int(kv.get("uptime"), None), "load": kv.get("load", "")}
    mem = kv.get("mem", "").split()
    if len(mem) == 2:
        info["mem_total"], info["mem_avail"] = _int(mem[0]), _int(mem[1])
    overlay = kv.get("overlay", "").split()
    if len(overlay) == 2:
        info["ovl_total"], info["ovl_avail"] = _int(overlay[0]), _int(overlay[1])
    _eval_sites(kv, res, info)
    _eval_zapret(kv, res, info)
    _eval_forkop(kv, res, info)
    return res, info


# --- state ---------------------------------------------------------------------

def apply(dev, results, ts):
    """Stores one poll result, returns (downs, ups) that should be notified."""
    downs, ups = [], []
    row = db.one("SELECT muted FROM devices WHERE id = ?", (dev["id"],))
    dev_muted = bool(row and row["muted"])
    prev_rows = {r["name"]: r for r in db.q("SELECT * FROM checks WHERE device_id = ?", (dev["id"],))}
    down_now = {}
    for name in CHECKS:
        status, detail = results[name]
        prev = prev_rows.get(name)
        since = prev["since"] if prev and prev["status"] == status else ts
        streak = ((prev["streak"] if prev else 0) + 1) if status == "fail" else 0
        service = name in SERVICES
        confirmed = ts - since >= config.SERVICE_FAIL_SECONDS if service else streak >= config.FAIL_THRESHOLD
        down = prev["down"] if prev else 0
        down_since = prev["down_since"] if prev else None
        alerted = prev["alerted"] if prev else 0
        muted = prev["muted"] if prev else 0

        if status == "fail":
            if not down and confirmed:
                down, down_since = 1, since
                db.x("INSERT INTO events (ts, device_id, device_name, check_name, kind, detail) VALUES (?,?,?,?,?,?)",
                     (ts, dev["id"], dev["name"], name, "down", detail))
        elif down and status == "ok" and service and ts - since < config.SERVICE_RECOVER_SECONDS:
            # a single good answer between failures is not a recovery yet
            pass
        elif down:
            # unknown / n/a clear the failure silently: there is nothing to confirm
            if status == "ok":
                duration = since - (down_since or since)
                db.x("INSERT INTO events (ts, device_id, device_name, check_name, kind, detail, duration) VALUES (?,?,?,?,?,?,?)",
                     (ts, dev["id"], dev["name"], name, "up", detail, duration))
                if alerted:
                    ups.append((dev["name"], name, duration))
            down, down_since, alerted = 0, None, 0

        if down and not alerted:
            if not (dev_muted or muted or down_now.get(PARENT.get(name))):
                downs.append((dev["name"], name, detail))
                alerted = 1
        down_now[name] = down

        db.x(
            "INSERT INTO checks (device_id, name, status, detail, since, streak, down, down_since, alerted, muted, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (device_id, name) DO UPDATE SET "
            "status=excluded.status, detail=excluded.detail, since=excluded.since, streak=excluded.streak, "
            "down=excluded.down, down_since=excluded.down_since, alerted=excluded.alerted, updated=excluded.updated",
            (dev["id"], name, status, detail, since, streak, down, down_since, alerted, muted, ts),
        )
    db.x("INSERT INTO samples (ts, device_id, data) VALUES (?,?,?)",
         (ts, dev["id"], json.dumps({k: v[0] for k, v in results.items()})))
    return downs, ups


def _no_data(reach):
    results = {name: ("unknown", "нет данных") for name in CHECKS}
    results["reach"] = reach
    return results


async def probe_one(dev, sem, force=False):
    """Returns (results, info, error), or None when the router's own agent is reporting."""
    if not force and agent_fresh(dev["id"]):
        return None
    if not dev["online"]:
        ago = f" · был в сети {human(db.now() - dev['last_seen'])} назад" if dev["last_seen"] else ""
        _ssh_fail_since.pop(dev["id"], None)
        return _no_data(("fail", "не в сети tailscale" + ago)), None, None
    if not force and _auth_retry_at.get(dev["id"], 0) > db.now():
        return _no_data(("unknown", "неверный пароль SSH")), None, "auth"
    async with sem:
        # An idle tailscale peer can take a while to bring its path up, so one
        # timed-out connect is retried before the router is called unreachable.
        for _ in range(2):
            try:
                kv, error = await asyncio.wait_for(ssh_probe(dev), config.PROBE_TIMEOUT + 60)
            except (asyncio.TimeoutError, TimeoutError):
                kv, error = None, "таймаут"
            if error != "таймаут":
                break
    if error == "auth":
        # do not hammer the router with wrong passwords on every cycle
        _auth_retry_at[dev["id"]] = db.now() + config.AUTH_RETRY_SECONDS
        return _no_data(("unknown", "неверный пароль SSH")), None, "auth"
    _auth_retry_at.pop(dev["id"], None)
    if error:
        # Seen in this tailnet: the peer answers tailscale pings while its tunnel
        # passes no traffic for a while. The router itself is usually fine, so it
        # only counts as a failure once it has lasted SSH_GRACE_SECONDS.
        since = _ssh_fail_since.setdefault(dev["id"], db.now())
        lasted = db.now() - since
        detail = f"в tailscale online, но по SSH недоступен ({error})"
        if lasted < config.SSH_GRACE_SECONDS:
            return _no_data(("unknown", detail)), None, error
        return _no_data(("fail", f"{detail} уже {human(lasted)}")), None, error
    _ssh_fail_since.pop(dev["id"], None)
    results, info = evaluate(kv)
    return results, info, None


def _upsert_device(dev, ts):
    db.x(
        "INSERT INTO devices (id, name, hostname, ip, online, last_seen, present, first_seen) VALUES (?,?,?,?,?,?,1,?) "
        "ON CONFLICT (id) DO UPDATE SET name=excluded.name, hostname=excluded.hostname, ip=excluded.ip, "
        "online=excluded.online, last_seen=COALESCE(excluded.last_seen, devices.last_seen), present=1",
        (dev["id"], dev["name"], dev["hostname"], dev["ip"], int(dev["online"]), dev["last_seen"], ts),
    )


def _store(dev, results, info, error, ts):
    _upsert_device(dev, ts)
    if info is not None:
        db.x("UPDATE devices SET probe_ts=?, probe_error=NULL, info=? WHERE id=?", (ts, json.dumps(info), dev["id"]))
    else:
        db.x("UPDATE devices SET probe_error=? WHERE id=?", (error, dev["id"]))
    return apply(dev, results, ts)


async def cycle():
    ts = db.now()
    peers = await tailscale_peers()
    sem = asyncio.Semaphore(config.CONCURRENCY)
    probed = await asyncio.gather(*(probe_one(p, sem) for p in peers))
    downs, ups = [], []
    for dev, outcome in zip(peers, probed):
        if outcome is None:
            _upsert_device(dev, ts)
            continue
        d, u = _store(dev, *outcome, ts)
        downs += d
        ups += u
    ids = [p["id"] for p in peers]
    if ids:
        db.x(f"UPDATE devices SET present=0 WHERE id NOT IN ({','.join('?' * len(ids))})", ids)
    alerts.queue(downs, ups)
    db.x("DELETE FROM samples WHERE ts < ?", (ts - config.RETENTION_DAYS * 86400,))
    db.x("DELETE FROM events WHERE ts < ?", (ts - 180 * 86400,))
    state["history"] = overview_history()
    state["last_cycle"] = db.now()
    log.info("cycle: %d routers, %d down, %d up, %ds", len(peers), len(downs), len(ups), db.now() - ts)


async def probe_now(device_id):
    """Manual re-check of one router from the dashboard."""
    peers = [p for p in await tailscale_peers() if p["id"] == device_id]
    if not peers:
        return False
    if agent_fresh(device_id):
        # the unreachable-over-SSH case is exactly why the agent exists: keep its data
        _upsert_device(peers[0], db.now())
        return True
    results, info, error = await probe_one(peers[0], asyncio.Semaphore(1), force=True)
    downs, ups = _store(peers[0], results, info, error, db.now())
    alerts.queue(downs, ups)
    return True


async def loop():
    while True:
        state["running"] = True
        try:
            await cycle()
            state["error"] = None
        except Exception as e:
            log.exception("cycle failed")
            state["error"] = str(e)[:200]
        state["running"] = False
        wake.clear()
        try:
            await asyncio.wait_for(wake.wait(), config.POLL_INTERVAL)
        except (asyncio.TimeoutError, TimeoutError):
            pass


# --- router agent ------------------------------------------------------------------

def agent_token(device_id: str) -> str:
    return hmac.new(config.AGENT_SECRET.encode(), device_id.encode(), hashlib.sha256).hexdigest()[:40]


def agent_auth(device_id, token) -> bool:
    if not (config.AGENT_SECRET and device_id and token):
        return False
    return hmac.compare_digest(agent_token(device_id), token)


def agent_fresh(device_id) -> bool:
    row = db.one("SELECT push_ts FROM devices WHERE id = ?", (device_id,))
    return bool(row and row["push_ts"] and db.now() - row["push_ts"] < config.PUSH_FRESH_SECONDS)


def ingest(device_id, text) -> bool:
    """A report pushed by the agent on the router; same probe output as over SSH."""
    row = db.one("SELECT * FROM devices WHERE id = ?", (device_id,))
    kv = _parse_kv(text)
    if not row or not kv:
        return False
    results, info = evaluate(kv)
    results["reach"] = ("ok", "на связи, данные присылает агент")
    info["source"] = "agent"
    if not info.get("proxies"):
        # the agent skips the bulky proxy list; keep what the last SSH poll saw
        info["proxies"] = db.loads(row["info"]).get("proxies", [])
    dev = {"id": row["id"], "name": row["name"], "hostname": row["hostname"], "ip": row["ip"],
           "online": bool(row["online"]), "last_seen": row["last_seen"]}
    ts = db.now()
    downs, ups = _store(dev, results, info, None, ts)
    db.x("UPDATE devices SET push_ts = ? WHERE id = ?", (ts, device_id))
    _ssh_fail_since.pop(device_id, None)
    alerts.queue(downs, ups)
    return True


def install_script(device_id) -> str:
    conf = f"URL='{config.PUBLIC_URL}'\nDEVICE='{device_id}'\nTOKEN='{agent_token(device_id)}'\n"
    return f"""#!/bin/sh
# Installs the monitoring agent: two small scripts, a config file and a cron line.
main() {{
set -e
mkdir -p /usr/lib/rmon
cat >/etc/rmon-agent.conf <<'RMON_EOF'
{conf}RMON_EOF
chmod 600 /etc/rmon-agent.conf
cat >/usr/lib/rmon/probe.sh <<'RMON_EOF'
{PROBE}RMON_EOF
cat >/usr/bin/rmon-agent <<'RMON_EOF'
{AGENT}RMON_EOF
chmod 755 /usr/bin/rmon-agent
for f in /usr/bin/rmon-agent /usr/lib/rmon/ /etc/rmon-agent.conf; do
	grep -qxF "$f" /etc/sysupgrade.conf 2>/dev/null || echo "$f" >>/etc/sysupgrade.conf
done
(crontab -l 2>/dev/null | grep -v rmon-agent; echo '*/2 * * * * /usr/bin/rmon-agent >/dev/null 2>&1 # rmon-agent') | crontab -
/etc/init.d/cron enable >/dev/null 2>&1 || true
/etc/init.d/cron restart >/dev/null 2>&1 || true
if /usr/bin/rmon-agent now </dev/null; then
	echo "rmon-agent: installed, first report sent"
else
	echo "rmon-agent: installed, but the first report failed (will retry from cron)"
fi
}}
main
"""


def install_command(device_id) -> str:
    return f"curl -fsS {config.PUBLIC_URL}/api/agent/install/{device_id}/{agent_token(device_id)} | sh"


# --- history -------------------------------------------------------------------

def _worst(current, status):
    rank = {"": 0, "na": 0, "unknown": 1, "ok": 2, "fail": 3}
    return status if rank.get(status, 0) > rank.get(current, 0) else current


def overview_history(hours=24, buckets=48):
    """Per device: worst overall status in each time bucket, oldest first."""
    end = db.now()
    start = end - hours * 3600
    width = hours * 3600 / buckets
    out = {}
    for row in db.q("SELECT ts, device_id, data FROM samples WHERE ts >= ?", (start,)):
        strip = out.setdefault(row["device_id"], [""] * buckets)
        i = min(buckets - 1, int((row["ts"] - start) / width))
        for status in db.loads(row["data"]).values():
            strip[i] = _worst(strip[i], status)
    return out


def device_history(device_id, hours=24, buckets=72):
    end = db.now()
    start = end - hours * 3600
    width = hours * 3600 / buckets
    strips = {name: [""] * buckets for name in CHECKS}
    counts = {name: [0, 0] for name in CHECKS}
    for row in db.q("SELECT ts, data FROM samples WHERE device_id = ? AND ts >= ?", (device_id, start)):
        i = min(buckets - 1, int((row["ts"] - start) / width))
        for name, status in db.loads(row["data"]).items():
            if name not in strips:
                continue
            strips[name][i] = _worst(strips[name][i], status)
            if status == "ok":
                counts[name][0] += 1
            elif status == "fail":
                counts[name][1] += 1
    uptime = {name: (round(100 * ok / (ok + fail), 1) if ok + fail else None) for name, (ok, fail) in counts.items()}
    return {"start": start, "end": end, "bucket": width, "strips": strips, "uptime": uptime}
