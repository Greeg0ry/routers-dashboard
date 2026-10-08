"""Telegram side of the repairs: Claude login, requests in private messages, plan approval."""
import asyncio
import html
import logging
import re

import httpx

from . import alerts, claude, config, db, fixer

log = logging.getLogger("monitor.bot")

API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/"
RISK = {"low": "низкий", "medium": "средний", "high": "высокий"}
HELP = (
    "Я слежу за роутерами и чиню их с помощью Claude.\n\n"
    "Напишите, что исправить, обычным текстом — например: "
    "<i>исправь YouTube на всех роутерах, где он не открывается</i>. "
    "Я найду причину, покажу план и выполню его после вашего подтверждения.\n\n"
    "/login — войти в Claude по подписке\n"
    "/status — состояние Claude и роутеров\n"
    "Могу запоминать: <i>запомни, что zapret2 ставится из репозитория …</i> — заметки учитываются при поиске причин.\n\n"
    "/notes — мои заметки, /forget 3 — удалить заметку\n"
    "/cancel — отменить текущие задачи\n"
    "/logout — выйти из Claude"
)
_client: httpx.AsyncClient | None = None
_login: claude.Login | None = None
_login_timer: asyncio.Task | None = None


async def call(method, http_timeout=20, **payload):
    try:
        # Telegram rejects explicit nulls ("object expected as reply markup")
        body = {k: v for k, v in payload.items() if v is not None}
        r = await _client.post(API + method, json=body, timeout=http_timeout)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("telegram %s failed: %s", method, type(e).__name__)
        return None
    if not data.get("ok"):
        if "not modified" not in str(data.get("description")):
            log.warning("telegram %s: %s", method, data.get("description"))
        return None
    return data["result"]


async def say(text, keyboard=None):
    if len(text) > 4000:
        # every line closes its own tags, so a cut at a line break keeps the markup valid
        text = text[:4000].rsplit("\n", 1)[0] + "\n…"
    markup = {"inline_keyboard": keyboard} if keyboard else None
    sent = await call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=text, parse_mode="HTML",
                      disable_web_page_preview=True, reply_markup=markup)
    if sent is None:
        # better a plain message than a lost plan with its buttons
        plain = html.unescape(re.sub(r"</?(b|i|code|pre|a)\b[^>]*>", "", text))[:4000]
        sent = await call("sendMessage", chat_id=config.TELEGRAM_CHAT_ID, text=plain,
                          disable_web_page_preview=True, reply_markup=markup)
    return sent


e = html.escape


def _k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _usage(job):
    names = {"route": "разбор", "investigate": "исследование", "execute": "выполнение"}
    parts = []
    for stage, label in names.items():
        u = job["usage"].get(stage)
        if u:
            total = u["in"] + u["cached"]
            share = f", {round(100 * u['cached'] / total)}% из кэша" if total and u["cached"] else ""
            parts.append(f"{label} {u['model']}: {_k(total)} вх. / {_k(u['out'])} вых.{share}")
    return "\n\n<i>Токены — " + "; ".join(parts) + "</i>" if parts else ""


def _groups(job, steps=True):
    blocks = []
    for g in job["plan"]:
        title = fixer._labels(g["checks"]) or "Проблема"
        names = ", ".join(e(d["name"]) for d in g["devices"])
        lines = [f"<b>{title}</b> · {names}", e(g.get("diagnosis", ""))]
        if not g.get("fixable"):
            lines.append("⚠️ С роутера это не исправить.")
        elif steps:
            lines += [f"{i}. <code>{e(s['command'])}</code>\n    <i>{e(s['why'])}</i>" for i, s in enumerate(g["steps"], 1)]
            lines.append(f"Риск: {RISK.get(g.get('risk'), '?')}" + (" · план из сохранённых решений" if g.get("cached") else ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render(job):
    """(text, keyboard) for the job's current stage."""
    stage, jid = job["stage"], job["id"]
    count = len({t["id"] for t in job["targets"]})
    if stage == "routing":
        return "🔎 Разбираю запрос…", None
    if stage == "investigating":
        return f"🧠 Ищу причину ({config.MODEL_INVESTIGATE}) · роутеров: {count}…", None
    if stage == "awaiting":
        return ("🧩 <b>План исправления</b>\n\n" + _groups(job),
                [[{"text": "✅ Выполнить", "callback_data": f"j:{jid}:go"}, {"text": "✖️ Отмена", "callback_data": f"j:{jid}:no"}]])
    if stage == "executing":
        total = sum(len(g["devices"]) for g in job["plan"] if g.get("fixable"))
        return f"🛠 Выполняю план ({config.MODEL_EXECUTE}) · готово {len(job['results'])} из {total}…", None
    if stage == "cancelled":
        return "✖️ Задача отменена." + (f" {e(job['error'])}" if job["error"] else ""), None
    if stage == "failed":
        return f"❌ Не получилось: {e(job['error'] or 'неизвестная ошибка')}" + _usage(job), None
    if job["reply"]:
        return e(job["reply"]) + _usage(job), None
    if not job["results"]:
        return "🧩 <b>Диагноз</b>\n\n" + _groups(job, steps=False) + _usage(job), None
    lines = []
    for r in sorted(job["results"], key=lambda r: (r["ok"], r["name"])):
        label = fixer._labels(r["checks"])
        lines.append(f"{'✅' if r['ok'] else '❌'} <b>{e(r['name'])}</b>{' · ' + label if label else ''}\n"
                     f"{e(r.get('summary') or '')}" + (f"\nПроба: {e(r['verdict'])}" if r.get("verdict") else ""))
    failed = [r for r in job["results"] if not r["ok"]]
    head = "✅ <b>Исправлено</b>" if not failed else f"⚠️ <b>Исправлено {len(job['results']) - len(failed)} из {len(job['results'])}</b>"
    keyboard = [[{"text": "🧠 Разобраться глубже", "callback_data": f"j:{jid}:deep"}]] if failed else None
    return head + "\n\n" + "\n\n".join(lines) + _usage(job), keyboard


_push_locks: dict[int, asyncio.Lock] = {}


async def push(job_id):
    """Progress edits one message; a plan and a final result arrive as new messages so they notify."""
    # one at a time per job, each showing the state as it is now: updates cannot overtake each other
    async with _push_locks.setdefault(job_id, asyncio.Lock()):
        job = fixer.get(job_id)
        if not job:
            return
        text, keyboard = render(job)
        quiet = job["stage"] in fixer.RUNNING
        if job["msg_id"] and quiet:
            await call("editMessageText", chat_id=config.TELEGRAM_CHAT_ID, message_id=job["msg_id"], text=text,
                       parse_mode="HTML", disable_web_page_preview=True)
            return
        if job["msg_id"]:
            await call("deleteMessage", chat_id=config.TELEGRAM_CHAT_ID, message_id=job["msg_id"])
        sent = await say(text, keyboard)
        # a finished job keeps no message to edit
        db.x("UPDATE jobs SET msg_id = ? WHERE id = ?", (sent["message_id"] if sent and quiet else None, job_id))


# --- login -------------------------------------------------------------------------

def _drop_login():
    global _login, _login_timer
    if _login:
        _login.close()
    if _login_timer:
        _login_timer.cancel()
    _login = _login_timer = None


async def _login_timeout():
    await asyncio.sleep(600)
    _drop_login()
    await say("Вход в Claude не завершён за 10 минут, ссылка больше не действует. Отправьте /login заново.")


async def start_login():
    global _login, _login_timer
    _drop_login()
    login = claude.Login()
    try:
        url = await login.start()
    except (claude.ClaudeError, OSError) as err:
        await say(f"Не удалось начать вход: {e(str(err))}")
        return
    _login = login
    _login_timer = asyncio.get_running_loop().create_task(_login_timeout())
    await say("Откройте ссылку, войдите в аккаунт Claude с подпиской и разрешите доступ. "
              "На странице появится код — пришлите его сюда одним сообщением.",
              [[{"text": "Войти в Claude", "url": url}]])


async def finish_login(code, message_id):
    global _login
    login, _login = _login, None
    if _login_timer:
        _login_timer.cancel()
    # the code is single-use, but there is no reason to keep it in the chat
    await call("deleteMessage", chat_id=config.TELEGRAM_CHAT_ID, message_id=message_id)
    ok = await login.submit(code)
    if ok:
        status = await claude.auth_status()
        who = status.get("email") or status.get("subscriptionType") or ""
        await say(f"✅ Claude подключён{' · ' + e(str(who)) if who else ''}. Теперь можно писать, что исправить.")
    else:
        await say("Войти не получилось: " + e(login.output()[-200:] or "код не принят") + "\nОтправьте /login и попробуйте ещё раз.")


# --- updates -----------------------------------------------------------------------

async def status_text():
    auth = await claude.auth_status(fresh=True)
    who = auth.get("email") or auth.get("subscriptionType") or "подписка"
    lines = [f"Claude: {'подключён · ' + e(str(who)) if auth.get('loggedIn') else 'не авторизован, отправьте /login'}",
             f"Модели: разбор {config.MODEL_ROUTE}, исследование {config.MODEL_INVESTIGATE}, выполнение {config.MODEL_EXECUTE}"]
    devices = db.one("SELECT COUNT(*) AS n FROM devices WHERE present = 1")["n"]
    lines.append(f"Роутеров: {devices}")
    for row in db.q("SELECT c.name AS name, COUNT(*) AS n FROM checks c JOIN devices d ON d.id = c.device_id "
                    "WHERE c.down = 1 AND d.present = 1 GROUP BY c.name ORDER BY n DESC"):
        lines.append(f"🔴 {alerts.LABEL.get(row['name'], row['name'])}: {row['n']}")
    active = db.one("SELECT COUNT(*) AS n FROM jobs WHERE stage IN ('routing','investigating','awaiting','executing')")["n"]
    if active:
        lines.append(f"Задач в работе: {active}")
    return "\n".join(lines)


async def on_message(msg):
    text = (msg.get("text") or "").strip()
    if not text:
        return
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    if command in ("/start", "/help"):
        await say(HELP)
    elif command == "/login":
        await start_login()
    elif command == "/logout":
        await claude.logout()
        await say("Вышел из Claude.")
    elif command == "/notes":
        rows = fixer.notes()
        await say("\n\n".join(f"<b>#{r['id']}</b>{' · из исправления' if r['source'] == 'auto' else ''}\n{e(r['text'])}" for r in rows)
                  if rows else "Заметок пока нет. Напишите, что запомнить, обычным текстом.")
    elif command == "/forget":
        ids = [int(x.lstrip("#")) for x in text.split()[1:] if x.lstrip("#").isdigit()]
        await say(f"Удалено заметок: {fixer.forget(ids)}." if ids else "Укажите номер: /forget 3")
    elif command == "/status":
        await say(await status_text())
    elif command == "/cancel":
        _drop_login()
        rows = db.q("SELECT id FROM jobs WHERE stage IN ('routing','investigating','awaiting','executing')")
        for row in rows:
            fixer.cancel(row["id"])
        await say(f"Отменено задач: {len(rows)}." if rows else "Отменять нечего.")
    elif command:
        await say("Не знаю такой команды. /help")
    elif _login:
        await finish_login(text, msg["message_id"])
    elif not (await claude.auth_status()).get("loggedIn"):
        await say("Claude не авторизован. Отправьте /login — пришлю ссылку для входа.")
    else:
        fixer.submit(text)


async def on_callback(query):
    parts = (query.get("data") or "").split(":")
    note = None
    if len(parts) == 3 and parts[0] == "j" and parts[1].isdigit():
        job_id, action = int(parts[1]), parts[2]
        if action == "go":
            note = "Выполняю" if fixer.confirm(job_id) else "План уже неактуален"
        elif action == "no":
            note = "Отменено" if fixer.cancel(job_id) else "Уже неактуально"
        elif action == "deep":
            note = "Исследую заново" if fixer.deepen(job_id) else "Нечего исследовать или роутеры заняты"
        message = query.get("message") or {}
        if message.get("message_id"):
            await call("editMessageReplyMarkup", chat_id=config.TELEGRAM_CHAT_ID, message_id=message["message_id"],
                       reply_markup={"inline_keyboard": []})
    await call("answerCallbackQuery", callback_query_id=query["id"], text=note)


async def loop():
    global _client
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    _client = httpx.AsyncClient(timeout=20)
    fixer.on_update = push
    await call("setMyCommands", commands=[
        {"command": "status", "description": "Состояние Claude и роутеров"},
        {"command": "login", "description": "Войти в Claude по подписке"},
        {"command": "notes", "description": "Мои заметки"},
        {"command": "cancel", "description": "Отменить текущие задачи"},
        {"command": "help", "description": "Что я умею"},
    ])
    row = db.one("SELECT value FROM meta WHERE key = 'tg_offset'")
    offset = int(row["value"]) if row else 0
    while True:
        updates = await call("getUpdates", http_timeout=70, timeout=50, offset=offset,
                             allowed_updates=["message", "callback_query"])
        if updates is None:
            await asyncio.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            # stored before handling: a request must never run twice after a restart
            db.x("INSERT INTO meta (key, value) VALUES ('tg_offset', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (str(offset),))
            try:
                msg, query = update.get("message"), update.get("callback_query")
                # only the owner, and only in the private chat
                if msg and str(msg["chat"]["id"]) == str(config.TELEGRAM_CHAT_ID) and msg["chat"].get("type") == "private":
                    await on_message(msg)
                elif query and str(query["from"]["id"]) == str(config.TELEGRAM_CHAT_ID):
                    await on_callback(query)
            except Exception:
                log.exception("update handling failed")
