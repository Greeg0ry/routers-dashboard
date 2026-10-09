"""sing-box on one router: update check, update, switching the build (X, Extended, compressed).

forkop does the work itself, the same way its LuCI page does: `forkop component_action_async
sing_box <action>` starts a background job on the router and `component_action_status` reports
it. That job checks free flash first, keeps the previous package for rollback and restarts
forkop afterwards, so the dashboard only starts it, waits and shows the answer.

Updating the whole fleet, or moving it to one build, is a staged rollout like the forkop
one: the first router alone, then two at a time, and it stops at the first failure so a
bad build cannot spread.
"""
import asyncio
import html
import json
import logging
import re
import time

from . import collector, config, db, fixer

log = logging.getLogger("monitor.singbox")

# what the dashboard offers -> forkop's component action
ACTIONS = {"check": "check_update", "update": "install", "x": "install_x",
           "extended": "install_extended", "compressed": "install_extended_compressed",
           # not a forkop action: singbox_x.sh, for routers where forkop refuses X for lack of flash
           "x_clean": "install_x_clean"}
FLEET = ("update", "x", "extended", "compressed")
CLEAN_SCRIPT = (config.BASE_DIR / "singbox_x.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
CLEAN_REMOTE = "/tmp/rmon-sbx-run"  # .sh, .log and .rc live next to each other on the router
CLEAN_RESULT = re.compile(r"^RESULT=(\w+)$", re.M)
TITLE = {"install": "обновление sing-box", "install_x": "установка Sing-Box X",
         "install_extended": "установка sing-box Extended",
         "install_extended_compressed": "установка sing-box Extended compressed",
         "install_x_clean": "установка Sing-Box X с удалением прежней сборки"}
CHECK_TIMEOUT = 3 * 60
INSTALL_TIMEOUT = 20 * 60
_KIND = {action: kind for kind, action in ACTIONS.items()}
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]+$")

_tasks = set()
_rollout: asyncio.Task | None = None


def busy(device_id):
    return bool(db.one("SELECT 1 FROM singbox_actions WHERE device_id = ? AND stage IN ('queued','running')", (device_id,)))


def recover():
    db.x("UPDATE singbox_actions SET stage = 'skipped', log = 'сервис был перезапущен', updated = ? WHERE stage = 'queued'",
         (db.now(),))
    db.x("UPDATE singbox_actions SET stage = 'failed', log = 'сервис был перезапущен; действие на роутере "
         "могло завершиться само — проверьте версию', updated = ? WHERE stage = 'running'", (db.now(),))


def state(dev):
    """For the dashboard: installed build, the newest known update check and the last action. None when unknown."""
    sb = db.loads(dev["info"]).get("singbox")
    if not sb:
        return None
    last = db.one("SELECT * FROM singbox_actions WHERE device_id = ? ORDER BY id DESC LIMIT 1", (dev["id"],))
    # a check asked from here beats the router's cached one while the same build is still installed
    check = db.one("SELECT * FROM singbox_actions WHERE device_id = ? AND action = 'check_update' AND stage = 'done' "
                   "ORDER BY id DESC LIMIT 1", (dev["id"],))
    if check and check["raw"] == sb["raw"] and check["updated"] >= (sb.get("checked") or 0):
        sb = {**sb, "latest": check["to_version"], "status": check["status"], "checked": check["updated"]}
    return {**sb, "outdated": sb.get("status") == "outdated", "action": dict(last) if last else None}


def _blocked(device_id, fixes=True):
    """Why this router cannot get a new sing-box right now, or None. A check does not mind a repair (fixes=False)."""
    reach = db.one("SELECT status FROM checks WHERE device_id = ? AND name = 'reach'", (device_id,))
    if not reach or reach["status"] != "ok":
        return "роутер недоступен"
    if db.one("SELECT 1 FROM updates WHERE device_id = ? AND stage IN ('queued','running')", (device_id,)):
        return "на этом роутере обновляется forkop"
    if fixes and fixer._jobs_busy({device_id}):
        return "роутер занят исправлением"
    return None


def overview():
    """For the dashboard: which routers have a newer sing-box waiting and the state of the current rollout."""
    devices = [dev["id"] for dev in db.q("SELECT * FROM devices WHERE present = 1") if (state(dev) or {}).get("outdated")]
    batch = db.one("SELECT MAX(batch) AS b FROM singbox_actions")["b"]
    rows = db.q("SELECT stage, COUNT(*) AS n FROM singbox_actions WHERE batch = ? GROUP BY stage", (batch,)) if batch else []
    counts = {r["stage"]: r["n"] for r in rows}
    last = db.one("SELECT action FROM singbox_actions WHERE batch = ? LIMIT 1", (batch,)) if batch else None
    return {"outdated": devices,
            "rollout": {"batch": batch, "action": last["action"] if last else None, "counts": counts, "active": bool(counts.get("queued") or counts.get("running"))}}


def _wanted(sb, action):
    """Whether this router still needs the fleet action: it may have been changed since it was queued."""
    if not sb:
        return False
    return sb["outdated"] if action == "install" else sb["variant"] != _KIND[action].removesuffix("_clean")


def start_all(kind="update"):
    """Queues the fleet: "update" for every router with a newer version waiting, or a build name
    ("x", "extended", "compressed") for every router that has another build. Returns (count, error)."""
    global _rollout
    action = ACTIONS.get(kind)
    if kind not in FLEET:
        return 0, "неизвестное действие"
    if _rollout and not _rollout.done():
        return 0, "массовое действие с sing-box уже идёт"
    chosen = []
    for dev in db.q("SELECT * FROM devices WHERE present = 1 ORDER BY name"):
        sb = state(dev)
        if _wanted(sb, action) and not busy(dev["id"]) and not _blocked(dev["id"]):
            chosen.append((dev, sb))
    if not chosen:
        return 0, ("нет роутеров, которые можно обновить: новой версии нет, роутер недоступен или занят" if kind == "update"
                   else "нет роутеров для установки: сборка уже стоит, роутер недоступен или занят")
    # A router that already refused this (usually not enough flash) goes last, so that a
    # repeated run reaches the others before it stops on the same router again.
    chosen.sort(key=lambda c: bool(c[1]["action"] and c[1]["action"]["action"] == action and c[1]["action"]["stage"] == "failed"))
    batch = (db.one("SELECT MAX(batch) AS b FROM singbox_actions")["b"] or 0) + 1
    for dev, sb in chosen:
        db.x("INSERT INTO singbox_actions (ts, updated, batch, device_id, name, action, raw, from_version, to_version, stage) "
             "VALUES (?,?,?,?,?,?,?,?,?, 'queued')",
             (db.now(), db.now(), batch, dev["id"], dev["name"], action, sb["raw"], sb["version"],
              sb.get("latest") if kind == "update" else None))
    _rollout = asyncio.get_running_loop().create_task(_run_all(batch))
    return len(chosen), None


def cancel():
    """Stops the rollout before the routers still waiting; the ones being updated finish."""
    return db.x("UPDATE singbox_actions SET stage = 'skipped', log = 'отменено', updated = ? WHERE stage = 'queued'",
                (db.now(),)).rowcount


async def _run_all(batch):
    queue = [dict(r) for r in db.q("SELECT * FROM singbox_actions WHERE batch = ? ORDER BY id", (batch,))]
    try:
        # the first router goes alone: if the build is broken, only it finds out
        waves = [queue[:1]] + [queue[i:i + 2] for i in range(1, len(queue), 2)]
        for wave in waves:
            wave = [a for a in wave if db.one("SELECT stage FROM singbox_actions WHERE id = ?", (a["id"],))["stage"] == "queued"]
            results = await asyncio.gather(*(_one(a) for a in wave))
            if not all(results):
                db.x("UPDATE singbox_actions SET stage = 'skipped', log = 'остановлено: на другом роутере не удалось', "
                     "updated = ? WHERE batch = ? AND stage = 'queued'", (db.now(), batch))
                break
    except Exception:
        log.exception("sing-box rollout %s failed", batch)
        db.x("UPDATE singbox_actions SET stage = 'failed', log = 'внутренняя ошибка', updated = ? "
             "WHERE batch = ? AND stage IN ('queued','running')", (db.now(), batch))
    rows = db.q("SELECT * FROM singbox_actions WHERE batch = ? ORDER BY name", (batch,))
    icon = {"done": "✅", "failed": "❌", "skipped": "⏭"}
    lines = [f"{icon.get(r['stage'], '•')} {r['name']}: {r['from_version'] or '?'} → {r['to_version'] or '?'}"
             + (f" — {((r['log'] or '').strip().splitlines() or [''])[0][:120]}" if r["stage"] != "done" else "")
             for r in rows]
    done = sum(1 for r in rows if r["stage"] == "done")
    title = TITLE[rows[0]["action"]] if rows else ""
    text = (f"📦 <b>{title[:1].upper() + title[1:]}: {done} из {len(rows)}</b>\n" + "\n".join(html.escape(line) for line in lines)
            + f'\n\n<a href="{config.PUBLIC_URL}">Открыть дашборд</a>')
    db.x("INSERT INTO outbox (ts, text) VALUES (?, ?)", (db.now(), text[:3900]))


async def _one(action) -> bool:
    """One router of the rollout. True when it may go on: updated, or left untouched because it was not available."""
    dev = db.one("SELECT * FROM devices WHERE id = ? AND present = 1", (action["device_id"],))
    reason = "роутер пропал из списка" if not dev else _blocked(dev["id"])
    if not reason and not _wanted(state(dev), action["action"]):
        reason = "уже не требуется, sing-box на роутере изменился"
    if reason:
        # nothing was tried on it, so it says nothing about the build
        _set(action["id"], stage="skipped", log=f"не обновлялся: {reason}")
        return True
    _set(action["id"], stage="running", log="подключаюсь к роутеру")
    try:
        ok = await _do(action["id"], dict(dev), action["action"], report=False)
        if ok is None:
            # the router did not answer at all, so the build was never tried on it
            _set(action["id"], stage="skipped")
        return ok is not False
    except Exception:
        log.exception("sing-box action %s failed", action["id"])
        _set(action["id"], stage="failed", log="внутренняя ошибка")
        return False


def start(device_id, kind):
    """Starts a check or an install on one router. Returns an error text, or None when started."""
    action = ACTIONS.get(kind)
    dev = db.one("SELECT * FROM devices WHERE id = ? AND present = 1", (device_id,))
    if not action or not dev:
        return "неизвестное действие или роутер"
    info = db.loads(dev["info"])
    if not info.get("forkop_version"):
        return "на роутере нет forkop — sing-box ставится через него"
    if busy(device_id):
        return "на этом роутере уже идёт действие с sing-box"
    reason = _blocked(device_id, fixes=action != "check_update")
    if reason:
        return reason
    sb = info.get("singbox") or {}
    cur = db.x("INSERT INTO singbox_actions (ts, updated, device_id, name, action, raw, from_version, stage, log) "
               "VALUES (?,?,?,?,?,?,?, 'running', 'подключаюсь к роутеру')",
               (db.now(), db.now(), device_id, dev["name"], action, sb.get("raw"), sb.get("version")))
    task = asyncio.get_running_loop().create_task(_run(cur.lastrowid, dict(dev), action))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return None


def _set(action_id, **fields):
    fields["updated"] = db.now()
    db.x(f"UPDATE singbox_actions SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*fields.values(), action_id))


async def _forkop(dev, command, timeout):
    """Runs a forkop command that answers in JSON. Returns (object, error)."""
    result, error = await collector.ssh_run(dev, command, timeout)
    if error:
        return None, error
    data = db.loads(str(result.stdout or "").strip())
    return (data if isinstance(data, dict) else {}), None


async def _run(action_id, dev, action):
    try:
        await _do(action_id, dev, action)
    except Exception:
        log.exception("sing-box action %s failed", action_id)
        _set(action_id, stage="failed", log="внутренняя ошибка")


async def _do(action_id, dev, action, report=True):
    """Runs the action on the router and records the outcome. True when forkop reported success,
    None when nothing was started on it: the router could not be reached or is short of memory."""
    check = action == "check_update"
    reason = None if check else await collector.low_memory(dev)
    if reason:
        _set(action_id, stage="failed", log=reason)
        return None
    if action == "install_x_clean":
        return await _clean(action_id, dev, report)
    # HUP is ignored so that a dropped SSH session cannot take the router's background job with it
    job, error = await _forkop(dev, f"(trap '' HUP; exec forkop component_action_async sing_box {action} '')", 60)
    if error:
        _set(action_id, stage="failed", log=f"роутер не отвечает по SSH: {error}")
        return None
    job_id = str(job.get("job_id") or "")
    if not job.get("success") or not _JOB_ID.match(job_id):
        _set(action_id, stage="failed", log=str(job.get("message") or "")[:300] or
             "forkop не принял команду: в этой версии нет управления sing-box, сначала обновите forkop")
        return False
    _set(action_id, log="forkop проверяет версии" if check else
         "forkop скачивает пакет, проверяет место и меняет sing-box; интернет за роутером на это время пропадает")

    deadline, final, misses = time.time() + (CHECK_TIMEOUT if check else INSTALL_TIMEOUT), None, 0
    while time.time() < deadline and final is None:
        await asyncio.sleep(3 if check else 10)
        status, error = await _forkop(dev, f"forkop component_action_status {job_id}", 40)
        # action "status" is forkop saying it cannot read the job right now, not the job's result
        if error or not status or status.get("action") == "status":
            misses += 1  # the tunnel may blink while forkop is stopped for the swap
            if misses > 20:
                break
        elif not status.get("running"):
            final = status
    if final is None:
        _set(action_id, stage="failed", log="не дождался ответа роутера; действие могло завершиться само — "
                                              "нажмите «Проверить сейчас» и посмотрите версию")
        return False

    ok, message = bool(final.get("success")), str(final.get("message") or "")[:1000]
    if check:
        _set(action_id, stage="done" if ok else "failed", log=message, status=str(final.get("status") or ""),
             from_version=str(final.get("current_version") or ""), to_version=str(final.get("latest_version") or ""))
        return ok
    if not ok:
        message += "\n\nесли sing-box уже был затронут, forkop сам возвращает прежнюю сборку и запускается заново"
    return await _finish(action_id, dev, action, ok, message, report)


async def _finish(action_id, dev, action, ok, message, report, why=None):
    """Records how an install ended: the build the router really has now, the log, the Telegram line."""
    now = collector.singbox_info((await _forkop(dev, "forkop get_system_info", 90))[0])
    if now:
        row = db.one("SELECT info FROM devices WHERE id = ?", (dev["id"],))
        db.x("UPDATE devices SET info = ? WHERE id = ?", (json.dumps({**db.loads(row["info"]), "singbox": now}), dev["id"]))
    _set(action_id, stage="done" if ok else "failed", log=message, to_version=(now or {}).get("version"))
    collector.wake.set()  # the checks should show what the swap did to the services
    if not report:
        return ok
    row = db.one("SELECT * FROM singbox_actions WHERE id = ?", (action_id,))
    why = why if why is not None else (message.splitlines() or [""])[0]
    line = (f"{'✅' if ok else '❌'} {row['name']}: {TITLE[action]}, {row['from_version'] or '?'} → {row['to_version'] or '?'}"
            + ("" if ok else f" — {why[:200]}"))
    db.x("INSERT INTO outbox (ts, text) VALUES (?, ?)",
         (db.now(), f'📦 {html.escape(line)}\n\n<a href="{config.PUBLIC_URL}">Открыть дашборд</a>'))
    return ok


CLEAN_VERDICT = {
    "untouched": "на роутере ничего не изменено",
    "rolled_back": "не получилось, прежний sing-box возвращён и forkop запущен",
    "broken": "НЕ ПОЛУЧИЛОСЬ И ОТКАТ НЕ УДАЛСЯ: на роутере нет рабочего sing-box, forkop остановлен. Нужно чинить вручную",
}


async def _clean(action_id, dev, report):
    """Sing-Box X where forkop refuses it for lack of flash: singbox_x.sh removes the old package first."""
    # Detached from the SSH session: forkop is stopped half-way, and a dropped connection
    # must not leave the router without any sing-box.
    command = f"sh {CLEAN_REMOTE}.sh >{CLEAN_REMOTE}.log 2>&1; echo $? >{CLEAN_REMOTE}.rc"
    result, error = await collector.ssh_run(
        dev, f"rm -f {CLEAN_REMOTE}.rc {CLEAN_REMOTE}.log; cat >{CLEAN_REMOTE}.sh && "
             f"{{ (trap '' HUP; exec sh -c '{command}') </dev/null >/dev/null 2>&1 & }}",
        60, input=CLEAN_SCRIPT)
    if error or result.exit_status != 0:
        _set(action_id, stage="failed", log=f"не удалось запустить: {error or str(result.stderr)[:300]}")
        return None
    deadline, rc, tail, misses = time.time() + INSTALL_TIMEOUT, None, "", 0
    while time.time() < deadline and rc is None:
        await asyncio.sleep(10)
        result, error = await collector.ssh_run(dev, f"cat {CLEAN_REMOTE}.rc 2>/dev/null; echo ---; cat {CLEAN_REMOTE}.log 2>/dev/null", 30)
        if error:
            misses += 1  # the tunnel may blink while forkop is stopped
            if misses > 30:
                break
            continue
        head, _, tail = str(result.stdout).partition("---\n")
        tail = tail.strip()[-3000:]
        _set(action_id, log=tail or "скрипт работает")
        rc = int(head.strip()) if head.strip().lstrip("-").isdigit() else None
    if rc is None:
        _set(action_id, stage="failed", log=(tail + "\n\nне дождался завершения; скрипт на роутере доделывает сам — "
                                              "нажмите «Проверить сейчас» и посмотрите сборку").strip())
        return False
    await collector.ssh_run(dev, f"rm -f {CLEAN_REMOTE}.sh {CLEAN_REMOTE}.rc {CLEAN_REMOTE}.log", 30)
    found = CLEAN_RESULT.findall(tail)
    verdict = found[-1] if found else "broken"
    ok = verdict in ("ok", "already")
    why = "" if ok else CLEAN_VERDICT.get(verdict, CLEAN_VERDICT["broken"])
    return await _finish(action_id, dev, "install_x_clean", ok, tail if ok else f"{tail}\n\n{why}", report, why)
