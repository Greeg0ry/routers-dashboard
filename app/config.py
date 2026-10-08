import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "monitor.db"

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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
PROBE_TIMEOUT = int(os.environ.get("PROBE_TIMEOUT", "60"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "10"))
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "2"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
AUTH_RETRY_SECONDS = 1800
SSH_GRACE_SECONDS = int(os.environ.get("SSH_GRACE_SECONDS", "900"))

# Router agents authenticate with HMAC(AGENT_SECRET, device id). A router whose
# agent reported within PUSH_FRESH_SECONDS is not polled over SSH.
AGENT_SECRET = os.environ.get("AGENT_SECRET", "")
PUSH_FRESH_SECONDS = int(os.environ.get("PUSH_FRESH_SECONDS", "330"))

# Tailnet device names (first DNS label) that are not monitored.
EXCLUDE = set(os.environ.get("EXCLUDE", "").split())
