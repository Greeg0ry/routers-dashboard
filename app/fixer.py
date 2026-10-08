"""Repairs on request: route the request, investigate, get approval, execute, verify.

Token economy, in order of effect:
  * the request is read by the cheap model, and only it sees the whole fleet table;
  * routers with the same symptom form one group, and the strong model investigates
    a couple of representatives instead of every router;
  * a plan that worked is stored as a playbook and reused for the same symptom
    without any investigation;
  * the executor gets one router and the ready plan, so its context stays small;
  * system prompts are constant text, so the API prompt cache serves them;
  * the result is verified by the dashboard's own probe, not by another model run.
"""
import asyncio
import json
import logging
import os
import re
import sys

from . import alerts, claude, collector, config, db

log = logging.getLogger("monitor.fixer")

FIXABLE = [c for c in collector.CHECKS if c != "reach"]
RUNNING = ("routing", "investigating", "executing")
TOOLS = ("mcp__rmon__router_run", "mcp__rmon__router_check")
REPRESENTATIVES = 2

on_update = None  # set by the bot: called with a job id after every change
_tasks: dict[int, asyncio.Task] = {}
_notices: set[asyncio.Task] = set()

KNOWLEDGE = """\
Ты обслуживаешь парк домашних роутеров OpenWrt (aarch64, busybox ash, пакеты через apk, на старых — opkg). \
На роутерах стоит обход блокировок:
- zapret или zapret2 (процессы nfqws / nfqws2, nft-таблица, служба /etc/init.d/zapret или zapret2, \
настройки в /opt/zapret*/config и uci) — обход DPI на уровне пакетов;
- forkop — форк podkop, управляет sing-box: FakeIP DNS (198.18.x), списки доменов, прокси-серверы. \
Служба /etc/init.d/forkop, настройки `uci show forkop`, диагностика `forkop get_status`, \
`forkop check_nft_rules`, `forkop check_fakeip`, `forkop show_version`, журнал `logread -e forkop -e sing-box`.
Трафик самого роутера идёт через оба механизма, поэтому curl с роутера показывает то же, что видят устройства за ним.

Дашборд проверяет сервисы так: curl с роутера к набору адресов сервиса, любой HTTP-ответ — адрес доступен, \
код 000 — соединения нет. Сервис считается упавшим, если не отвечает хотя бы один адрес из набора.

Правила:
- Роутеры разные (версии, конфиги) — проверяй, а не предполагай. Путей и команд не выдумывай: сначала убедись, что они есть.
- Не печатай содержимое /etc/sing-box/config.json и секреты прокси целиком — выбирай нужные поля через grep/jsonfilter.
- Экономь вызовы: объединяй команды через ';' в один вызов, фильтруй вывод на роутере (grep, tail -n 30). \
`logread -f` и другие бесконечные команды запрещены.
- Нельзя: перезагрузка, прошивка, сброс, смена паролей, настройки сети/Wi-Fi/firewall/SSH/tailscale, удаление пакетов, \
отключение автозапуска zapret и forkop.
- Пиши по-русски, коротко, без Markdown.
"""

ROUTE_SYSTEM = """\
Ты диспетчер бота мониторинга роутеров. Тебе дают таблицу роутеров с текущими сбоями и сообщение владельца. \
Определи, что он хочет, и верни JSON.
- intent "fix": владелец просит что-то исправить или починить. В checks перечисли проверки, которые нужно чинить \
(из internet, zapret, forkop, google, youtube, chatgpt, discord); пустой список — все текущие сбои названных роутеров. \
В devices — имена роутеров точно как в таблице; пустой список — все роутеры, где есть такой сбой. \
Если проблема не относится ни к одной проверке (другой сайт, скорость, Wi-Fi), оставь checks пустым и коротко \
опиши проблему в problem.
- intent "answer": вопрос или любое другое сообщение. Ответь сам в reply по данным таблицы: коротко, по-русски, \
обычным текстом без разметки. Ничего не выдумывай: если данных в таблице нет, так и скажи.
"""
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["fix", "answer"]},
        "checks": {"type": "array", "items": {"type": "string", "enum": FIXABLE}},
        "devices": {"type": "array", "items": {"type": "string"}},
        "problem": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["intent"],
}

INVESTIGATE_SYSTEM = KNOWLEDGE + """
Сейчас этап диагностики. Тебе дают проблему и группу роутеров с одинаковым симптомом. Инструменты работают \
только на роутерах-представителях и только на чтение: разрешены команды вроде cat, grep, awk, uci show, logread, nft list, ip, curl, forkop get_status, соединённые через ; | && — без циклов, $(…) и записи в файлы.
Найди причину и составь план исправления, который другой, более простой исполнитель выполнит на КАЖДОМ роутере \
группы. Поэтому:
- команды плана должны быть готовыми к запуску и не зависеть от конкретного роутера (без IP, имён интерфейсов и \
значений, которые ты видел только на одном из них; если значение нужно — вычисляй его в самой команде);
- начинай с наименее рискованного (перезапуск службы, обновление списков), меняй конфигурацию только если \
причина точно в ней, и сохраняй копию файла в /tmp перед правкой;
- после перезапуска forkop или zapret добавь `sleep 20`, службе нужно время;
- 1–5 шагов. Проверку результата в план не включай — её сделает система.
Если с роутера это не исправить (лежит прокси-сервер, проблема у провайдера, нужен человек) — поставь \
fixable=false, оставь steps пустым и объясни в diagnosis, что нужно сделать владельцу.
diagnosis — 1–3 предложения для владельца: что сломано и почему. risk — насколько план может ухудшить \
работу интернета у людей за роутером.
"""
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "fixable": {"type": "boolean"},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {"command": {"type": "string"}, "why": {"type": "string"}},
            "required": ["command", "why"],
        }},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["diagnosis", "fixable", "steps", "risk"],
}

EXECUTE_SYSTEM = KNOWLEDGE + """
Сейчас этап исполнения. Тебе дают один роутер и утверждённый владельцем план. Выполни шаги по порядку через \
router_run: один шаг — один вызов, команду шага передавай дословно, иначе она будет отклонена. Кроме шагов плана разрешены только команды чтения. Если шаг завершился ошибкой — один раз \
разберись (посмотри вывод, проверь путь или имя службы) и, если причина очевидна и исправление остаётся в \
рамках плана, повтори; иначе остановись. В конце вызови router_check и верни JSON: fixed — прошли ли нужные \
проверки, summary — одно-два предложения о том, что сделано и чем закончилось.
"""
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"fixed": {"type": "boolean"}, "summary": {"type": "string"}},
    "required": ["fixed", "summary"],
}


# --- jobs --------------------------------------------------------------------------

def get(job_id):
    row = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        return None
    job = dict(row)
    for key in ("targets", "plan", "results"):
        job[key] = json.loads(job[key] or "[]")
    job["usage"] = db.loads(job["usage"])
    job["tried"] = json.loads(job["tried"]) if job["tried"] else None
    return job


def _save(job_id, **fields):
    for key, value in fields.items():
        if isinstance(value, (list, dict)):
            fields[key] = json.dumps(value, ensure_ascii=False)
    fields["updated"] = db.now()
    db.x(f"UPDATE jobs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*fields.values(), job_id))
    if on_update:
        task = asyncio.get_running_loop().create_task(on_update(job_id))
        _notices.add(task)
        task.add_done_callback(_notices.discard)


def _add_usage(job_id, stage, model, usage):
    total = get(job_id)["usage"]
    entry = total.setdefault(stage, {"model": model, "in": 0, "cached": 0, "out": 0, "turns": 0, "runs": 0})
    for key in ("in", "cached", "out", "turns", "runs"):
        entry[key] += usage.get(key, 0)
    db.x("UPDATE jobs SET usage = ? WHERE id = ?", (json.dumps(total), job_id))


def for_device(device_id):
    """The latest job of the last day that involves this router."""
    for row in db.q("SELECT id, targets FROM jobs WHERE ts > ? ORDER BY id DESC LIMIT 30", (db.now() - 86400,)):
        if any(t["id"] == device_id for t in json.loads(row["targets"] or "[]")):
            return get(row["id"])
    return None


def _busy(device_ids):
    db.x("UPDATE jobs SET stage = 'cancelled', error = 'план не подтверждён за час' WHERE stage = 'awaiting' AND updated < ?",
         (db.now() - 3600,))
    for row in db.q("SELECT targets FROM jobs WHERE stage IN ('routing','investigating','awaiting','executing')"):
        if any(t["id"] in device_ids for t in json.loads(row["targets"] or "[]")):
            return True
    return False


def recover():
    """After a restart nothing is running any more; plans waiting for approval stay valid."""
    db.x("UPDATE jobs SET stage = 'failed', error = 'сервис был перезапущен во время работы' "
         "WHERE stage IN ('routing','investigating','executing')")
    db.x("DELETE FROM job_log WHERE ts < ?", (db.now() - 90 * 86400,))


async def _together(coros):
    """gather, but one failure stops the others instead of leaving them running on routers."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _spawn(job_id, coro):
    async def guarded():
        try:
            await coro
        except asyncio.CancelledError:
            _save(job_id, stage="cancelled")
        except claude.ClaudeError as e:
            _save(job_id, stage="failed", error=str(e))
        except Exception as e:
            log.exception("job %s failed", job_id)
            _save(job_id, stage="failed", error=f"внутренняя ошибка: {type(e).__name__}")
        finally:
            _tasks.pop(job_id, None)
    _tasks[job_id] = asyncio.get_running_loop().create_task(guarded())


# --- fleet -------------------------------------------------------------------------

def _fleet():
    checks = {}
    for c in db.q("SELECT * FROM checks"):
        checks.setdefault(c["device_id"], {})[c["name"]] = c
    return [(dict(r), checks.get(r["id"], {})) for r in db.q("SELECT * FROM devices WHERE present = 1 ORDER BY name")]


def _describe(dev, checks):
    info = db.loads(dev["info"])
    reach = checks.get("reach")
    if not reach or reach["status"] != "ok":
        state = "недоступен: " + (reach["detail"] if reach else "нет данных")
    else:
        bad = [f"{name}: {c['detail']}" for name, c in checks.items() if c["status"] == "fail"]
        state = "сбои — " + "; ".join(bad) if bad else "сбоев нет"
    return (f"{dev['name']} | {info.get('model') or '?'} | OpenWrt {info.get('release') or '?'} | "
            f"zapret: {', '.join(info.get('zapret') or []) or 'нет'} | forkop: {info.get('forkop_version') or 'нет'} | {state}")


def _signature(names, dev, checks):
    """Routers with the same signature get one investigation and one plan."""
    info = db.loads(dev["info"])
    status = lambda name: checks[name]["status"] if name in checks else "-"
    failing = [n + ":" + re.sub(r"\d+ мс|\(\d+ из \d+\)", "", checks[n]["detail"] or "").strip() for n in sorted(names)]
    forkop = ".".join((re.findall(r"\d+", info.get("forkop_version") or "") + ["", ""])[:2])
    return f"{' & '.join(failing)}|{'+'.join(info.get('zapret') or [])}:{status('zapret')}|forkop {forkop}:{status('forkop')}"


def _labels(names):
    return ", ".join(alerts.LABEL[n] for n in names)


def _mcp_config(job_id, mode, device_ids, max_calls, plan=()):
    path = config.DATA_DIR / f"mcp-{job_id}-{os.urandom(4).hex()}.json"
    env = {
        "PYTHONPATH": str(config.BASE_DIR.parent), "DATA_DIR": str(config.DATA_DIR),
        "FLEET_SSH_USER": config.SSH_USER, "FLEET_SSH_PASSWORDS": " ".join(config.SSH_PASSWORDS),
        "FLEET_SSH_KEY": config.SSH_KEY, "COLLECTOR": "0", "RMON_PLAN": json.dumps(list(plan)),
        "RMON_JOB": str(job_id), "RMON_MODE": mode, "RMON_DEVICES": ",".join(device_ids), "RMON_MAX_CALLS": str(max_calls),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"mcpServers": {"rmon": {"command": sys.executable, "args": ["-m", "app.fix_mcp"], "env": env}}}, f)
    return path


# --- stages ------------------------------------------------------------------------
# A target is one router with every check that has to pass on it. All of a router's
# failures go into one investigation and one executor: they usually share a cause,
# and two executors must never work on the same router at once.

async def _ready():
    if not (await claude.auth_status()).get("loggedIn"):
        raise claude.ClaudeError("Claude не авторизован. Отправьте боту /login")


def _create(source, request, stage, targets=(), tried=None):
    return db.x("INSERT INTO jobs (ts, updated, source, request, stage, targets, tried) VALUES (?,?,?,?,?,?,?)",
                (db.now(), db.now(), source, request[:2000], stage, json.dumps(list(targets)),
                 json.dumps(tried, ensure_ascii=False) if tried else None)).lastrowid


def submit(text, source="telegram"):
    """A free-text request. Returns the job id."""
    job_id = _create(source, text, "routing")
    _spawn(job_id, _route(job_id, text))
    return job_id


def submit_device(device_id):
    """The "fix" button on a router: no routing model is needed. Returns (job id, error)."""
    fleet = {dev["id"]: (dev, checks) for dev, checks in _fleet()}
    if device_id not in fleet:
        return None, "роутер не найден"
    dev, checks = fleet[device_id]
    names = [n for n in FIXABLE if n in checks and checks[n]["status"] == "fail"]
    if not names:
        return None, "на роутере сейчас нет сбоев"
    if _busy({device_id}):
        return None, "по этому роутеру уже есть незавершённая задача"
    targets = [{"id": device_id, "name": dev["name"], "checks": names}]
    job_id = _create("dashboard", f"Исправить на {dev['name']}: {_labels(names)}", "investigating", targets)
    _spawn(job_id, _plan(job_id, targets))
    return job_id, None


def deepen(job_id):
    """A failed fix goes back to the strong model together with what was already tried."""
    job = get(job_id)
    failed = {r["id"] for r in job["results"] if not r["ok"]}
    targets = [t for t in job["targets"] if t["id"] in failed]
    if job["stage"] != "done" or not targets or _busy(failed):
        return None
    tried = {"plan": [g for g in job["plan"] if failed & {d["id"] for d in g["devices"]}],
             "results": [r for r in job["results"] if not r["ok"]]}
    new_id = _create(job["source"], job["request"], "investigating", targets, tried)
    _spawn(new_id, _plan(new_id, targets, tried=tried))
    return new_id


async def _route(job_id, text):
    await _ready()
    fleet = _fleet()
    table = "\n".join(_describe(dev, checks) for dev, checks in fleet)
    answer, usage = await claude.run(
        f"Роутеры:\n{table}\n\nСообщение владельца:\n{text}", model=config.MODEL_ROUTE,
        system=ROUTE_SYSTEM, schema=ROUTE_SCHEMA, timeout=180)
    _add_usage(job_id, "route", config.MODEL_ROUTE, usage)
    if answer.get("intent") != "fix":
        _save(job_id, stage="done", reply=answer.get("reply") or "Не понял запрос.")
        return
    wanted = {str(n).lower() for n in answer.get("devices") or []}
    asked = [c for c in answer.get("checks") or [] if c in FIXABLE]
    problem = (answer.get("problem") or "").strip()
    targets, skipped = [], []
    for dev, checks in fleet:
        if wanted and dev["name"].lower() not in wanted:
            continue
        failing = [n for n in (asked or FIXABLE) if n in checks and checks[n]["status"] == "fail"]
        reachable = "reach" in checks and checks["reach"]["status"] == "ok"
        if not reachable and (wanted or failing):
            skipped.append(dev["name"])
        elif failing or (wanted and problem and not asked):
            targets.append({"id": dev["id"], "name": dev["name"], "checks": failing})
    if _busy({t["id"] for t in targets}):
        _save(job_id, stage="done", reply="По этим роутерам уже есть незавершённая задача — дождитесь её или отмените.")
        return
    if not targets:
        reply = "Роутеров с такой проблемой сейчас нет."
        if skipped:
            reply += " Недоступны и пропущены: " + ", ".join(skipped) + "."
        _save(job_id, stage="done", reply=reply)
        return
    _save(job_id, stage="investigating", targets=targets)
    await _plan(job_id, targets, problem=problem)


async def _plan(job_id, targets, problem=None, tried=None):
    await _ready()
    fleet = {dev["id"]: (dev, checks) for dev, checks in _fleet()}
    groups = {}
    for t in targets:
        dev, checks = fleet[t["id"]]
        key = _signature(t["checks"], dev, checks) if t["checks"] else f"custom|{t['id']}"
        groups.setdefault(key, {"signature": key if t["checks"] else None, "checks": t["checks"], "devices": []})
        groups[key]["devices"].append({"id": t["id"], "name": t["name"]})
    plan = list(groups.values())
    request = get(job_id)["request"]
    sem = asyncio.Semaphore(2)

    async def investigate(group):
        cached = None if tried or not group["signature"] else db.one(
            "SELECT * FROM playbooks WHERE signature = ? AND ok > fail AND updated > ?",
            (group["signature"], db.now() - config.PLAYBOOK_DAYS * 86400))
        if cached:
            group.update(json.loads(cached["plan"]), cached=True)
            return
        reps = group["devices"][:REPRESENTATIVES]
        lines = [_describe(*fleet[d["id"]]) for d in group["devices"]]
        prompt = (f"Запрос владельца: {request}\n"
                  + (f"Проблема: {problem}\n" if problem else "")
                  + (f"Не проходят проверки: {', '.join(group['checks'])}\n" if group["checks"] else "")
                  + f"Роутеры группы ({len(lines)}):\n" + "\n".join(lines)
                  + f"\n\nИнструменты доступны на: {', '.join(d['name'] for d in reps)}.")
        if tried:
            prompt += ("\n\nЭто уже пробовали, не помогло — найди другую причину:\n"
                       + json.dumps(tried, ensure_ascii=False)[:3000])
        path = _mcp_config(job_id, "investigate", [d["id"] for d in reps], 20)
        try:
            async with sem:
                answer, usage = await claude.run(
                    prompt, model=config.MODEL_INVESTIGATE, system=INVESTIGATE_SYSTEM, schema=PLAN_SCHEMA,
                    mcp=path, tools=TOOLS, effort=config.EFFORT_INVESTIGATE or None, timeout=900)
        finally:
            path.unlink(missing_ok=True)
        _add_usage(job_id, "investigate", config.MODEL_INVESTIGATE, usage)
        group.update(diagnosis=str(answer["diagnosis"]), risk=answer.get("risk", "medium"), cached=False,
                     steps=[{"command": str(s["command"]), "why": str(s.get("why", ""))} for s in answer["steps"]][:8])
        group["fixable"] = bool(answer["fixable"]) and bool(group["steps"])

    await _together(investigate(g) for g in plan)
    if not any(g["fixable"] for g in plan):
        _save(job_id, stage="done", plan=plan)
    elif config.FIX_AUTO_APPLY:
        _save(job_id, stage="executing", plan=plan)
        await _execute(job_id)
    else:
        _save(job_id, stage="awaiting", plan=plan)


def confirm(job_id):
    job = get(job_id)
    if not job or job["stage"] != "awaiting":
        return False
    _save(job_id, stage="executing")
    _spawn(job_id, _execute(job_id))
    return True


def cancel(job_id):
    job = get(job_id)
    if not job or job["stage"] not in ("awaiting", *RUNNING):
        return False
    task = _tasks.get(job_id)
    if task:
        task.cancel()
    else:
        _save(job_id, stage="cancelled")
    return True


async def _execute(job_id):
    await _ready()
    job = get(job_id)
    results = []
    sem = asyncio.Semaphore(config.FIX_CONCURRENCY)

    async def one(group, device):
        outcome = {"id": device["id"], "name": device["name"], "checks": group["checks"], "ok": False}
        commands = [s["command"] for s in group["steps"]]
        path = _mcp_config(job_id, "execute", [device["id"]], len(commands) * 2 + 4, plan=commands)
        try:
            dev = dict(db.one("SELECT * FROM devices WHERE id = ?", (device["id"],)))
            steps = "\n".join(f"{i}. {s['command']}\n   зачем: {s['why']}" for i, s in enumerate(group["steps"], 1))
            prompt = (f"Роутер: {dev['name']}\n"
                      f"Проблема: {group['diagnosis']}\n"
                      + (f"Нужные проверки: {', '.join(group['checks'])}\n" if group["checks"] else "")
                      + f"План:\n{steps}")
            async with sem:
                answer, usage = await claude.run(
                    prompt, model=config.MODEL_EXECUTE, system=EXECUTE_SYSTEM, schema=RESULT_SCHEMA,
                    mcp=path, tools=TOOLS, timeout=600)
                _add_usage(job_id, "execute", config.MODEL_EXECUTE, usage)
                outcome.update(ok=bool(answer["fixed"]), summary=str(answer["summary"])[:500])
                # the verdict is the probe's, not the model's
                kv, error = await collector.ssh_probe(dev)
                if error:
                    outcome.update(ok=False, unverified=True, verdict=f"проверить не удалось: {error}")
                else:
                    checked, info = collector.evaluate(kv)
                    downs, ups = collector._store(dev, checked, info, None, db.now())
                    alerts.queue(downs, ups)
                    if group["checks"]:
                        bad = [f"{alerts.LABEL[n]}: {checked[n][1]}" for n in group["checks"] if checked[n][0] != "ok"]
                        outcome.update(ok=not bad, verdict="; ".join(bad) or "все проверки проходят")
        except claude.ClaudeError as e:
            outcome.update(unverified=True, summary=str(e))
        except Exception as e:
            # one router's trouble must not abandon the others half-way
            log.exception("job %s: %s failed", job_id, device["name"])
            outcome.update(unverified=True, summary=f"внутренняя ошибка: {type(e).__name__}")
        finally:
            path.unlink(missing_ok=True)
        results.append(outcome)
        _save(job_id, results=results)

    await _together(one(g, d) for g in job["plan"] if g.get("fixable") for d in g["devices"])

    for group in job["plan"]:
        if not group.get("fixable") or not group["signature"]:
            continue
        # only outcomes the probe actually judged say anything about the plan
        judged = [r for r in results if r["id"] in {d["id"] for d in group["devices"]} and not r.get("unverified")]
        ok = sum(1 for r in judged if r["ok"])
        fail = len(judged) - ok
        if not judged or (not group.get("cached") and not ok):
            continue  # an unproven plan never replaces a stored one
        stored = {k: group[k] for k in ("diagnosis", "steps", "risk", "fixable")}
        score = "ok + excluded.ok, fail = fail + excluded.fail" if group.get("cached") else "excluded.ok, fail = excluded.fail"
        db.x("INSERT INTO playbooks (signature, plan, ok, fail, updated) VALUES (?,?,?,?,?) "
             f"ON CONFLICT (signature) DO UPDATE SET plan = excluded.plan, ok = {score}, updated = excluded.updated",
             (group["signature"], json.dumps(stored, ensure_ascii=False), ok, fail, db.now()))
    _save(job_id, stage="done", results=results)
