import asyncio
import html
import logging

import httpx

from . import config, db

log = logging.getLogger("monitor.alerts")

LABEL = {
    "reach": "Роутер",
    "internet": "Интернет",
    "zapret": "zapret",
    "forkop": "forkop",
    "google": "Google",
    "youtube": "YouTube",
    "chatgpt": "ChatGPT",
    "discord": "Discord",
}
DOWN_TEXT = {
    "reach": "Роутер недоступен",
    "internet": "Нет интернета",
    "zapret": "zapret не работает",
    "forkop": "forkop не работает",
    "google": "Google не открывается",
    "youtube": "YouTube не открывается",
    "chatgpt": "ChatGPT не открывается",
    "discord": "Discord не открывается",
}
UP_TEXT = {
    "reach": "Роутер снова в сети",
    "internet": "Интернет восстановлен",
    "zapret": "zapret снова работает",
    "forkop": "forkop снова работает",
    "google": "Google снова открывается",
    "youtube": "YouTube снова открывается",
    "chatgpt": "ChatGPT снова открывается",
    "discord": "Discord снова открывается",
}
ORDER = list(LABEL)


def human(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 90:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{hours // 24} дн"


def queue(downs, ups):
    """downs: [(device, check, detail)], ups: [(device, check, duration)].

    One message per poll cycle, grouped by check, so a resource failing on many
    routers at once arrives as a single notification.
    """
    if not downs and not ups:
        return
    blocks = []
    for check in ORDER:
        items = [d for d in downs if d[1] == check]
        if items:
            head = f"🔴 <b>{DOWN_TEXT[check]}</b>"
            if len(items) > 1:
                head += f" · {len(items)}"
            lines = [f"• {html.escape(n)}" + (f" — {html.escape(d)}" if d else "") for n, _, d in items]
            blocks.append("\n".join([head] + lines))
    for check in ORDER:
        items = [u for u in ups if u[1] == check]
        if items:
            head = f"🟢 <b>{UP_TEXT[check]}</b>"
            lines = [f"• {html.escape(n)} — не работало {human(d)}" for n, _, d in items]
            blocks.append("\n".join([head] + lines))
    text = "\n\n".join(blocks)
    if len(text) > 3800:
        text = text[:3800] + "\n…"
    text += f'\n\n<a href="{config.PUBLIC_URL}">Открыть дашборд</a>'
    db.x("INSERT INTO outbox (ts, text) VALUES (?, ?)", (db.now(), text))


async def _send(client: httpx.AsyncClient, text: str) -> bool:
    """True when the message is done with (delivered or undeliverable)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        log.warning("telegram send failed: %s", type(e).__name__)
        return False
    if r.status_code == 200:
        return True
    if r.status_code == 400:
        log.error("telegram rejected message: %s", r.text[:200])
        return True
    log.warning("telegram HTTP %s", r.status_code)
    return False


async def sender_loop():
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        log.warning("telegram is not configured, alerts stay in the outbox")
        return
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                db.x("DELETE FROM outbox WHERE ts < ?", (db.now() - 86400,))
                for row in db.q("SELECT id, text FROM outbox ORDER BY id LIMIT 10"):
                    if not await _send(client, row["text"]):
                        break
                    db.x("DELETE FROM outbox WHERE id = ?", (row["id"],))
            except Exception:
                log.exception("sender loop error")
            await asyncio.sleep(10)
