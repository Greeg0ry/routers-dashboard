"""sing-box on one router: update check, update, switching the build (X, Extended, compressed).

forkop does the work itself, the same way its LuCI page does: `forkop component_action_async
sing_box <action>` starts a background job on the router and `component_action_status` reports
it. That job checks free flash first, keeps the previous package for rollback and restarts
forkop afterwards, so the dashboard only starts it, waits and shows the answer.
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
           "extended": "install_extended", "compressed": "install_extended_compressed"}
TITLE = {"install": "обновление sing-box", "install_x": "установка Sing-Box X",
         "install_extended": "установка sing-box Extended",
         "install_extended_compressed": "установка sing-box Extended compressed"}
CHECK_TIMEOUT = 3 * 60
INSTALL_TIMEOUT = 20 * 60
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]+$")

_tasks = set()


def busy(device_id):
    return bool(db.one("SELECT 1 FROM singbox_actions WHERE device_id = ? AND stage = 'running'", (device_id,)))


def recover():
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


def start(device_id, kind):
    """Starts a check or an install on one router. Returns an error text, or None when started."""
    action = ACTIONS.get(kind)
    dev = db.one("SELECT * FROM devices WHERE id = ? AND present = 1", (device_id,))
    if not action or not dev:
        return "неизвестное действие или роутер"
    info = db.loads(dev["info"])
    if not info.get("forkop_version"):
        return "на роутере нет forkop — sing-box ставится через него"
    reach = db.one("SELECT status FROM checks WHERE device_id = ? AND name = 'reach'", (device_id,))
    if not reach or reach["status"] != "ok":
        return "роутер недоступен"
    if busy(device_id):
        return "на этом роутере уже идёт действие с sing-box"
    if db.one("SELECT 1 FROM updates WHERE device_id = ? AND stage IN ('queued','running')", (device_id,)):
        return "на этом роутере обновляется forkop"
    if action != "check_update" and fixer._busy({device_id}):  # busy() above already ruled out sing-box itself
        return "роутер занят исправлением"
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


async def _do(action_id, dev, action):
    check = action == "check_update"
    # HUP is ignored so that a dropped SSH session cannot take the router's background job with it
    job, error = await _forkop(dev, f"(trap '' HUP; exec forkop component_action_async sing_box {action} '')", 60)
    if error:
        _set(action_id, stage="failed", log=f"роутер не отвечает по SSH: {error}")
        return
    job_id = str(job.get("job_id") or "")
    if not job.get("success") or not _JOB_ID.match(job_id):
        _set(action_id, stage="failed", log=str(job.get("message") or "")[:300] or
             "forkop не принял команду: в этой версии нет управления sing-box, сначала обновите forkop")
        return
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
        return

    ok, message = bool(final.get("success")), str(final.get("message") or "")[:1000]
    if check:
        _set(action_id, stage="done" if ok else "failed", log=message, status=str(final.get("status") or ""),
             from_version=str(final.get("current_version") or ""), to_version=str(final.get("latest_version") or ""))
        return
    # the build the router really has now, whatever the job said
    now = collector.singbox_info((await _forkop(dev, "forkop get_system_info", 90))[0])
    if now:
        row = db.one("SELECT info FROM devices WHERE id = ?", (dev["id"],))
        db.x("UPDATE devices SET info = ? WHERE id = ?", (json.dumps({**db.loads(row["info"]), "singbox": now}), dev["id"]))
    if not ok:
        message += "\n\nесли sing-box уже был затронут, forkop сам возвращает прежнюю сборку и запускается заново"
    _set(action_id, stage="done" if ok else "failed", log=message, to_version=(now or {}).get("version"))
    collector.wake.set()  # the checks should show what the swap did to the services
    row = db.one("SELECT * FROM singbox_actions WHERE id = ?", (action_id,))
    line = (f"{'✅' if ok else '❌'} {row['name']}: {TITLE[action]}, {row['from_version'] or '?'} → {row['to_version'] or '?'}"
            + ("" if ok else f" — {(message.splitlines() or [''])[0][:200]}"))
    db.x("INSERT INTO outbox (ts, text) VALUES (?, ?)",
         (db.now(), f'📦 {html.escape(line)}\n\n<a href="{config.PUBLIC_URL}">Открыть дашборд</a>'))
