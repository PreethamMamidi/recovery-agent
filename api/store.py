"""Webhook event store. Separate from results/audit.db so GET /payments
stays the dashboard's committed chain and a webhook replay cannot rewrite it."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "api" / "events.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unknown_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    reason TEXT,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    chosen_action TEXT NOT NULL,
    action_args TEXT NOT NULL,
    gate_result TEXT NOT NULL,
    gate_reason TEXT NOT NULL,
    executed INTEGER NOT NULL
);
"""


def events_path() -> Path:
    override = os.environ.get("API_EVENTS_DB")
    return Path(override) if override else DEFAULT_DB


def _connect() -> sqlite3.Connection:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event_seen(event_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_event(event_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO events (event_id, seen_at) VALUES (?, ?)",
            (event_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def log_unknown_reason(event_id: str, reason: str | None) -> None:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO unknown_reasons (event_id, reason, seen_at)
               VALUES (?, ?, ?)""",
            (event_id, reason, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def unknown_reasons(event_id: str | None = None) -> list[dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        if event_id is None:
            cur = conn.execute(
                "SELECT event_id, reason, seen_at FROM unknown_reasons ORDER BY id"
            )
        else:
            cur = conn.execute(
                """SELECT event_id, reason, seen_at FROM unknown_reasons
                   WHERE event_id = ? ORDER BY id""",
                (event_id,),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def log_decisions(event_id: str, payment_id: str, failure_class: str,
                  actions: list[dict]) -> None:
    conn = _connect()
    try:
        now = _now()
        for item in actions:
            conn.execute(
                """INSERT INTO decisions (
                    event_id, payment_id, timestamp, failure_class,
                    chosen_action, action_args, gate_result, gate_reason, executed
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    payment_id,
                    now,
                    failure_class,
                    item["action"],
                    item.get("args_json", "{}"),
                    item["gate"],
                    item["reason"],
                    int(item["gate"] == "allowed"),
                ),
            )
        conn.commit()
    finally:
        conn.close()
