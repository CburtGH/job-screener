"""SQLite storage. One file at data/jobscout.db; no ORM, just small helpers."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "jobscout.db"

# Re-entrant so a nested `with conn()` in the same thread can't deadlock the whole app (it once did).
# Still: don't open a connection while holding another; do lookups before taking the lock.
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    content     TEXT NOT NULL,          -- markdown
    is_master   INTEGER NOT NULL DEFAULT 0,
    source_file TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT,
    location      TEXT,
    is_remote     INTEGER NOT NULL DEFAULT 0,
    employment_type TEXT,
    salary_min    REAL,
    salary_max    REAL,
    salary_text   TEXT,
    url           TEXT,
    apply_url     TEXT,
    publisher     TEXT,                 -- e.g. "Indeed", "ZipRecruiter" (from aggregators)
    description   TEXT,
    posted_at     TEXT,
    found_at      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',   -- new|saved|applying|applied|interview|offer|rejected|dismissed
    keyword_score REAL,
    ai_score      REAL,
    ai_summary    TEXT,
    ai_strengths  TEXT,                 -- json list
    ai_gaps       TEXT,                 -- json list
    tailored_resume TEXT,
    resume_changes  TEXT,               -- json list
    cover_letter  TEXT,
    notes         TEXT,
    applied_at    TEXT,
    archived_at   TEXT,                 -- set by the 60-day rule (monitor.archive_stale); purged 5 days later
    scored_by     TEXT,                 -- who produced ai_score: "Built-in MAX", "Claude", "External LLM", "Claude Code", …
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS email_threads (
    thread_id     TEXT PRIMARY KEY,     -- Gmail thread id
    history_id    TEXT,
    subject       TEXT,
    counterpart   TEXT,                 -- the other side's From header (recruiter, ATS, ...)
    participants  TEXT,                 -- json list of addresses (excluding you)
    last_from_me  INTEGER NOT NULL DEFAULT 0,
    last_date     TEXT,
    snippet       TEXT,
    message_count INTEGER,
    unread        INTEGER NOT NULL DEFAULT 0,
    in_inbox      INTEGER NOT NULL DEFAULT 1,
    labels        TEXT,
    synced_at     TEXT,
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    link_manual   INTEGER NOT NULL DEFAULT 0,
    category      TEXT,                 -- interview|rejection|application_received|recruiter_outreach|offer|assessment|follow_up|outreach|other
    ai_summary    TEXT,
    needs_reply   INTEGER NOT NULL DEFAULT 0,
    suggested_status TEXT,
    triaged_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_job ON email_threads(job_id);

-- Jobs the user removed. The pipeline never re-adds a posting that matches one of these (same source+id, or
-- same title+company from any source). The job row itself is deleted.
CREATE TABLE IF NOT EXISTS removed_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL,
    title       TEXT,
    company     TEXT,
    removed_at  TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_removed_dedupe ON removed_jobs(dedupe_key);

CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    level      TEXT NOT NULL,           -- info|warn|error
    message    TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def conn():
    with _lock:
        c = _connect()
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # Added columns (idempotent; existing databases have real data, so no rebuilds).
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        # hidden: 1 = user hid it; -1 = user unhid it (stays visible even if its score is low); 0 = default.
        # Dismissed jobs and low-scored new jobs are hidden too; see monitor.hidden_sql().
        if "hidden" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        # archived_at: soft delete by the 60-day rule (monitor.archive_stale); NULL = never archived.
        if "archived_at" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN archived_at TEXT")
        # scored_by: display label of the engine that produced the current ai_score.
        if "scored_by" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN scored_by TEXT")


def kv_get(key: str, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def log(message: str, level: str = "info") -> None:
    import logging
    logging.getLogger("jobscout.activity").log(
        {"warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO), message)
    with conn() as c:
        c.execute("INSERT INTO activity(at, level, message) VALUES(?,?,?)", (now_iso(), level, message))
        c.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT 500)")


JSON_LIST_FIELDS = ("ai_strengths", "ai_gaps", "resume_changes")


def row_to_job(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in JSON_LIST_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else []
    d["is_remote"] = bool(d["is_remote"])
    return d
