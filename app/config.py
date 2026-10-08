import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "monitor.db"
try:
    DATA_DIR.chmod(0o700)  # the database, the routers' SSH key and run configs are for this user only
except OSError:
    pass

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://monit.rocky-rabbit.ru")

# Auth
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") == "1"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# SSH to routers: key first (if present), then each password in order.
SSH_USER = os.environ.get("FLEET_SSH_USER", "root")
SSH_PASSWORDS = os.environ.get("FLEET_SSH_PASSWORDS", "").split()
SSH_KEY = os.environ.get("FLEET_SSH_KEY", str(DATA_DIR / "id_ed25519"))

# Polling. COLLECTOR=0 serves the UI from an existing database without probing (local dev).
COLLECTOR_ENABLED = os.environ.get("COLLECTOR", "1") == "1"
# Also the period of the router agents' cron line, so it should be a whole number of minutes.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))
PROBE_TIMEOUT = int(os.environ.get("PROBE_TIMEOUT", "60"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "10"))
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "2"))
# Google, YouTube, ChatGPT, Discord: a failure is recorded once the service has been
# failing this long, and cleared once it has been answering again for this long.
SERVICE_FAIL_SECONDS = int(os.environ.get("SERVICE_FAIL_SECONDS", "900"))
SERVICE_RECOVER_SECONDS = int(os.environ.get("SERVICE_RECOVER_SECONDS", "300"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
AUTH_RETRY_SECONDS = 1800
SSH_GRACE_SECONDS = int(os.environ.get("SSH_GRACE_SECONDS", "900"))

# Router agents authenticate with HMAC(AGENT_SECRET, device id). A router whose
# agent reported within PUSH_FRESH_SECONDS is not polled over SSH.
AGENT_SECRET = os.environ.get("AGENT_SECRET", "")
# The default tolerates one missed report.
PUSH_FRESH_SECONDS = int(os.environ.get("PUSH_FRESH_SECONDS", str(POLL_INTERVAL * 2 + 90)))

# Tailnet device names (first DNS label) that are not monitored.
EXCLUDE = set(os.environ.get("EXCLUDE", "").split())

# Repairs. Claude Code runs on this server under a Claude subscription (login through
# the Telegram bot). Each stage has its own model: a cheap one reads the request, a
# strong one investigates, a cheap one carries out the approved plan.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
MODEL_ROUTE = os.environ.get("MODEL_ROUTE", "haiku")
MODEL_INVESTIGATE = os.environ.get("MODEL_INVESTIGATE", "opus")
MODEL_EXECUTE = os.environ.get("MODEL_EXECUTE", "haiku")
EFFORT_INVESTIGATE = os.environ.get("EFFORT_INVESTIGATE", "medium")
# 1 = carry out the plan without waiting for the confirmation button
FIX_AUTO_APPLY = os.environ.get("FIX_AUTO_APPLY", "0") == "1"
FIX_CONCURRENCY = int(os.environ.get("FIX_CONCURRENCY", "3"))
# A plan that fixed a symptom is reused for the same symptom without a new investigation.
PLAYBOOK_DAYS = int(os.environ.get("PLAYBOOK_DAYS", "30"))
