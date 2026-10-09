import json
import os
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA synchronous=NORMAL")
for _suffix in ("", "-wal", "-shm"):
    try:
        os.chmod(f"{DB_PATH}{_suffix}", 0o600)
    except OSError:
        pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hostname TEXT,
    ip TEXT,
    online INTEGER DEFAULT 0,
    last_seen INTEGER,
    present INTEGER DEFAULT 1,
    muted INTEGER DEFAULT 0,
    first_seen INTEGER,
    probe_ts INTEGER,
    probe_error TEXT,
    info TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS checks (
    device_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    since INTEGER,
    streak INTEGER DEFAULT 0,
    down INTEGER DEFAULT 0,
    down_since INTEGER,
    alerted INTEGER DEFAULT 0,
    muted INTEGER DEFAULT 0,
    updated INTEGER,
    PRIMARY KEY (device_id, name)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    device_name TEXT,
    check_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    duration INTEGER
);
CREATE INDEX IF NOT EXISTS events_device ON events (device_id, ts);
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_device ON samples (device_id, ts);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    updated INTEGER,
    source TEXT,
    request TEXT,
    stage TEXT NOT NULL,
    error TEXT,
    reply TEXT,
    targets TEXT DEFAULT '[]',
    plan TEXT DEFAULT '[]',
    results TEXT DEFAULT '[]',
    usage TEXT DEFAULT '{}',
    tried TEXT,
    msg_id INTEGER
);
CREATE TABLE IF NOT EXISTS job_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    device TEXT,
    mode TEXT,
    command TEXT,
    exit_code INTEGER,
    output TEXT
);
CREATE INDEX IF NOT EXISTS job_log_job ON job_log (job_id);
CREATE TABLE IF NOT EXISTS playbooks (
    signature TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    ok INTEGER DEFAULT 0,
    fail INTEGER DEFAULT 0,
    updated INTEGER
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    source TEXT,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    updated INTEGER,
    batch INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    name TEXT,
    from_version TEXT,
    to_version TEXT,
    channel TEXT,
    args TEXT,
    stage TEXT NOT NULL,
    log TEXT
);
CREATE INDEX IF NOT EXISTS updates_device ON updates (device_id, id);
CREATE TABLE IF NOT EXISTS singbox_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    updated INTEGER,
    device_id TEXT NOT NULL,
    name TEXT,
    action TEXT NOT NULL,
    raw TEXT,
    from_version TEXT,
    to_version TEXT,
    status TEXT,
    stage TEXT NOT NULL,
    log TEXT
);
CREATE INDEX IF NOT EXISTS singbox_actions_device ON singbox_actions (device_id, id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""
with _lock:
    _conn.executescript(SCHEMA)
    if "push_ts" not in [r["name"] for r in _conn.execute("PRAGMA table_info(devices)")]:
        _conn.execute("ALTER TABLE devices ADD COLUMN push_ts INTEGER")


def q(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchall()


def one(sql, args=()):
    with _lock:
        return _conn.execute(sql, args).fetchone()


def x(sql, args=()):
    with _lock:
        return _conn.execute(sql, args)


def now() -> int:
    return int(time.time())


def loads(s):
    try:
        return json.loads(s) if s else {}
    except ValueError:
        return {}
