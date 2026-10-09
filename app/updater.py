"""Forkop updates from github.com/Screamshow/forkop, one router or the whole fleet.

The project's own install.sh does the work: unlike the update built into each
installed version, it is the same code for every router, backs up the settings
and rolls back by itself when something fails. The server downloads it once and
hands the same copy to every router, so nothing is piped from the network into
a shell there. No model is involved.

A fleet update is a staged rollout: the first router alone, then two at a time,
and the rollout stops at the first failure so a bad release cannot spread.
"""
import asyncio
import html
import logging
import re
import time

import httpx

from . import alerts, collector, config, db, fixer, singbox

log = logging.getLogger("monitor.updater")

REPO = "Screamshow/forkop"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=30"
INSTALLER_URL = f"https://raw.githubusercontent.com/{REPO}/main/install.sh"
REMOTE = "/tmp/rmon-forkop"  # .sh, .log and .rc live next to each other on the router
UPDATE_TIMEOUT = 25 * 60
ACTIVE = ("queued", "running")
# what the installer refuses to decide without a person; each is offered as an explicit retry
FLAGS = {"allow_tiny": "--allow-low-space-tiny", "confirm_legacy": "--confirm-legacy-migration"}
# A config file left behind by the long-removed legacy package makes the installer choose
# "legacy migration", which replaces the current forkop settings with that old file. When
# forkop is installed and the legacy package is not, the leftover is moved out of the way
# (kept in /root) so the installer does a normal update.
STALE_LEGACY = (
    "if { opkg list-installed 2>/dev/null || apk list -I 2>/dev/null; } | grep -qE '^forkop[ -]' && "
    "! { opkg list-installed 2>/dev/null || apk list -I 2>/dev/null; } | grep -qE '^podkop[-_]plus'; then "
    "for f in /etc/config/podkop-plus /etc/config/podkop_plus; do "
    "[ -f $f ] && mv $f /root/$(basename $f).stale; done; fi;"
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_latest = {"ts": 0, "stable": None, "canary": None, "error": None}
_installer = {"ts": 0, "text": None}
_rollout: asyncio.Task | None = None
_refresh: asyncio.Task | None = None


def _key(version):
    """Sortable version: 2.0.0 is newer than 2.0.0-canary.2, which is newer than 1.16.4."""
    numbers = [int(n) for n in re.findall(r"\d+", version or "")]
    core, pre = (numbers + [0, 0, 0])[:3], numbers[3:4]
    return (*core, 0 if "canary" in (version or "") else 1, *(pre or [0]))


async def _fetch_latest():
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
            r.raise_for_status()
            tags = [x["tag_name"] for x in r.json() if not x.get("draft")]
        stable = [t for t in tags if "canary" not in t]
        _latest.update(ts=time.time(), error=None,
                       stable=max(stable, key=_key) if stable else None,
                       canary=max(tags, key=_key) if tags else None)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        # keep the previous answer; try again in a few minutes rather than on every request
        _latest.update(ts=time.time() - 1500, error=type(e).__name__)
        log.warning("forkop releases: %s", type(e).__name__)


def latest():
    """Cached latest versions; a stale cache is refreshed in the background."""
    global _refresh
    if time.time() - _latest["ts"] > 1800 and (_refresh is None or _refresh.done()):
        _refresh = asyncio.get_running_loop().create_task(_fetch_latest())
    return {"stable": _latest["stable"], "canary": _latest["canary"], "error": _latest["error"]}


async def _get_installer():
    if _installer["text"] and time.time() - _installer["ts"] < 600:
        return _installer["text"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(INSTALLER_URL)
        r.raise_for_status()
    text = r.text.replace("\r\n", "\n")
    # a truncated or replaced download must never reach a router
    if not text.startswith("#!/bin/sh") or 'REPO_NAME="forkop"' not in text or not text.rstrip().endswith('main "$@"'):
        raise ValueError("install.sh из репозитория не похож на установщик forkop")
    _installer.update(ts=time.time(), text=text)
    return text


def plan(dev):
    """What an update would do for this router, from the last probe: None when forkop is not there."""
    current = (db.loads(dev["info"]).get("forkop_version") or "").strip()
    if not current or not re.match(r"\d+\.\d+", current):
        return None
    channel = "canary" if "canary" in current else "stable"
    target = latest()[channel]
    return {"current": current, "channel": channel, "target": target,
            "outdated": bool(target) and _key(current) < _key(target)}


def last_for(device_id):
    row = db.one("SELECT * FROM updates WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,))
    return dict(row) if row else None


def overview():
    """For the dashboard: versions, per-router plans and the state of the current rollout."""
    devices = {}
    for dev in db.q("SELECT * FROM devices WHERE present = 1"):
        p = plan(dev)
        if p:
            devices[dev["id"]] = p
    batch = db.one("SELECT MAX(batch) AS b FROM updates")["b"]
    rows = db.q("SELECT stage, COUNT(*) AS n FROM updates WHERE batch = ? GROUP BY stage", (batch,)) if batch else []
    counts = {r["stage"]: r["n"] for r in rows}
    return {"latest": latest(), "devices": devices,
            "rollout": {"batch": batch, "counts": counts, "active": bool(counts.get("queued") or counts.get("running"))}}


def recover():
    db.x("UPDATE updates SET stage = 'failed', log = 'сервис был перезапущен во время обновления; "
         "установщик на роутере мог завершиться сам — проверьте версию' WHERE stage = 'running'")
    db.x("UPDATE updates SET stage = 'skipped', log = 'сервис был перезапущен' WHERE stage = 'queued'")


def start(device_ids=None, flags=()):
    """Queues updates for the given routers, or for every outdated reachable one. Returns (count, error)."""
    global _rollout
    if _rollout and not _rollout.done():
        return 0, "обновление уже идёт"
    chosen = []
    for dev in db.q("SELECT * FROM devices WHERE present = 1 ORDER BY name"):
        p = plan(dev)
        if not p or not p["target"] or (device_ids is not None and dev["id"] not in device_ids):
            continue
        reach = db.one("SELECT status FROM checks WHERE device_id = ? AND name = 'reach'", (dev["id"],))
        if not p["outdated"] or not reach or reach["status"] != "ok" or fixer._busy({dev["id"]}) or singbox.busy(dev["id"]):
            continue
        chosen.append((dict(dev), p))
    if not chosen:
        return 0, "нет роутеров, которые можно обновить: версия уже последняя, роутер недоступен или занят исправлением"
    batch = (db.one("SELECT MAX(batch) AS b FROM updates")["b"] or 0) + 1
    args = " ".join(FLAGS[f] for f in flags if f in FLAGS)
    for dev, p in chosen:
        db.x("INSERT INTO updates (ts, updated, batch, device_id, name, from_version, to_version, channel, args, stage) "
             "VALUES (?,?,?,?,?,?,?,?,?, 'queued')",
             (db.now(), db.now(), batch, dev["id"], dev["name"], p["current"], p["target"], p["channel"], args))
    _rollout = asyncio.get_running_loop().create_task(_run(batch))
    return len(chosen), None


def cancel():
    """Stops the rollout before the routers still waiting; the one being updated finishes."""
    return db.x("UPDATE updates SET stage = 'skipped', log = 'отменено', updated = ? WHERE stage = 'queued'",
                (db.now(),)).rowcount


def _set(update_id, **fields):
    fields["updated"] = db.now()
    db.x(f"UPDATE updates SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*fields.values(), update_id))


async def _run(batch):
    try:
        script = await _get_installer()
    except (httpx.HTTPError, ValueError) as e:
        db.x("UPDATE updates SET stage = 'failed', log = ? WHERE batch = ?", (f"не удалось скачать install.sh: {e}", batch))
        return
    queue = [dict(r) for r in db.q("SELECT * FROM updates WHERE batch = ? ORDER BY id", (batch,))]
    try:
        # the first router goes alone: if the release is broken, only it finds out
        waves = [queue[:1]] + [queue[i:i + 2] for i in range(1, len(queue), 2)]
        for wave in waves:
            wave = [u for u in wave if db.one("SELECT stage FROM updates WHERE id = ?", (u["id"],))["stage"] == "queued"]
            results = await asyncio.gather(*(_update(u, script) for u in wave))
            if not all(results):
                db.x("UPDATE updates SET stage = 'skipped', log = 'остановлено: обновление на другом роутере не удалось', "
                     "updated = ? WHERE batch = ? AND stage = 'queued'", (db.now(), batch))
                break
    except Exception:
        log.exception("forkop rollout %s failed", batch)
        db.x("UPDATE updates SET stage = 'failed', log = 'внутренняя ошибка' WHERE batch = ? AND stage IN ('queued','running')", (batch,))
    _report(batch)


def _report(batch):
    rows = db.q("SELECT * FROM updates WHERE batch = ? ORDER BY name", (batch,))
    icon = {"done": "✅", "failed": "❌", "skipped": "⏭"}
    lines = [f"{icon.get(r['stage'], '•')} {r['name']}: {r['from_version']} → {r['to_version']}"
             + (f" — {(r['log'] or '').strip().splitlines()[-1][:120]}" if r["stage"] != "done" and r["log"] else "")
             for r in rows]
    done = sum(1 for r in rows if r["stage"] == "done")
    text = (f"🔄 <b>Обновление forkop: {done} из {len(rows)}</b>\n" + "\n".join(html.escape(line) for line in lines)
            + f'\n\n<a href="{config.PUBLIC_URL}">Открыть дашборд</a>')
    db.x("INSERT INTO outbox (ts, text) VALUES (?, ?)", (db.now(), text[:3900]))


# install.sh calls "apk add --force-reinstall" for both the install and its own rollback.
# apk-tools 3.0.2 (OpenWrt 25.12.0) does not know the option: the install fails, the
# rollback fails the same way and forkop is left stopped. Such a router is not touched.
APK_CHECK = ("command -v apk >/dev/null && apk add --force-reinstall --simulate 2>&1 | grep -q 'unrecognized option' "
             "&& echo APK_TOO_OLD $(apk --version | cut -d, -f1); true")
# What a failed installer can leave behind when its rollback did not finish.
REVIVE = ("forkop get_status 2>/dev/null | grep -q '\"running\": 1' || "
          "{ /etc/init.d/forkop enable; /etc/init.d/forkop start >/dev/null 2>&1; echo REVIVED; }")


async def _revive(dev, was_running):
    """After a failed update: start forkop again if it worked before and is stopped now."""
    if not was_running:
        return ""
    result, error = await collector.ssh_run(dev, REVIVE, 120)
    if error:
        return f"\n\nне удалось проверить, работает ли forkop после сбоя: {error}"
    if "REVIVED" in str(result.stdout):
        return "\n\nпосле сбоя forkop был остановлен — запущен заново на прежней версии"
    return ""


async def _update(update, script) -> bool:
    """One router. True when the new version is installed and forkop works."""
    dev = db.one("SELECT * FROM devices WHERE id = ?", (update["device_id"],))
    if not dev:
        _set(update["id"], stage="failed", log="роутер пропал из списка")
        return False
    dev = dict(dev)
    if singbox.busy(dev["id"]):
        _set(update["id"], stage="failed", log="Обновление не запускалось: на роутере сейчас меняется sing-box. Повторите позже.")
        return False
    _set(update["id"], stage="running", log="проверяю роутер")
    before = db.one("SELECT status FROM checks WHERE device_id = ? AND name = 'forkop'", (dev["id"],))
    was_running = bool(before) and before["status"] == "ok"
    result, error = await collector.ssh_run(dev, APK_CHECK, 40)
    if error:
        _set(update["id"], stage="failed", log=f"роутер не отвечает по SSH: {error}")
        return False
    if "APK_TOO_OLD" in str(result.stdout):
        version = str(result.stdout).replace("APK_TOO_OLD", "").strip()
        _set(update["id"], stage="failed", log=(
            f"Обновление не запускалось, на роутере ничего не изменено. Установщику forkop нужна опция apk "
            f"--force-reinstall, а в {version or 'apk этой прошивки'} её нет: установка упала бы, откат тоже, "
            "и forkop остался бы остановлен. Сначала нужно обновить прошивку роутера (или пакет apk)."))
        return False
    _set(update["id"], log="передаю установщик")
    # Detached from the SSH session: the update restarts networking pieces, and a
    # dropped connection must not kill the installer half-way.
    command = (f"sh {REMOTE}.sh --channel {update['channel']} {update['args'] or ''} >{REMOTE}.log 2>&1; "
               f"echo $? >{REMOTE}.rc")
    result, error = await collector.ssh_run(
        dev, f"rm -f {REMOTE}.rc {REMOTE}.log; {STALE_LEGACY} cat >{REMOTE}.sh && "
             f"{{ (trap '' HUP; exec sh -c '{command}') </dev/null >/dev/null 2>&1 & }}",
        60, input=script)
    if error or result.exit_status != 0:
        _set(update["id"], stage="failed", log=f"не удалось запустить: {error or str(result.stderr)[:300]}")
        return False

    deadline, rc, tail, misses = time.time() + UPDATE_TIMEOUT, None, "", 0
    while time.time() < deadline and rc is None:
        await asyncio.sleep(15)
        result, error = await collector.ssh_run(dev, f"cat {REMOTE}.rc 2>/dev/null; echo ---; tail -n 25 {REMOTE}.log 2>/dev/null", 30)
        if error:
            misses += 1  # the tunnel may blink while forkop restarts
            if misses > 20:
                break
            continue
        head, _, tail = str(result.stdout).partition("---\n")
        tail = _ANSI.sub("", tail).strip()[-3000:]
        _set(update["id"], log=tail or "установщик работает")
        rc = int(head.strip()) if head.strip().lstrip("-").isdigit() else None
    await collector.ssh_run(dev, f"rm -f {REMOTE}.sh {REMOTE}.rc", 30)
    if rc is None:
        _set(update["id"], stage="failed", log=(tail + "\n\nне дождался завершения установщика").strip())
        return False
    if rc != 0:
        note = await _revive(dev, was_running)
        _set(update["id"], stage="failed", log=tail + f"\n\nустановщик завершился с кодом {rc}; прежнюю версию и настройки он возвращает сам" + note)
        return False

    # The verdict is the probe's: the version changed and forkop works. After an update
    # forkop needs a minute or two to rebuild its lists and start sing-box, so a stopped
    # service is only a failure if it is still stopped after several probes.
    error = status = detail = None
    for attempt in range(8):
        await asyncio.sleep(30)
        _set(update["id"], log=tail + f"\n\nустановщик завершился, жду запуска forkop (проверка {attempt + 1} из 8)")
        kv, error = await collector.ssh_probe(dev)
        if error:
            continue
        checked, info = collector.evaluate(kv)
        status, detail = checked["forkop"]
        if status != "fail":
            break
    if error:
        _set(update["id"], stage="failed", log=tail + f"\n\nустановщик завершился успешно, но проверить роутер не удалось: {error}")
        return False
    downs, ups = collector._store(dev, checked, info, None, db.now())
    alerts.queue(downs, ups)
    version = info.get("forkop_version") or "?"
    if status == "fail":
        _set(update["id"], stage="failed", to_version=version,
             log=tail + f"\n\nчерез 4 минуты после обновления forkop не работает: {detail}")
        return False
    _set(update["id"], stage="done", to_version=version, log=tail)
    return True
