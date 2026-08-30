"""SQLite audit log. One row per proposed decision, including rejections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "audit" / "log.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    chosen_action TEXT NOT NULL,
    action_args TEXT NOT NULL,
    gate_result TEXT NOT NULL,
    gate_reason TEXT NOT NULL,
    executed INTEGER NOT NULL,
    flagged_for_review INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    cost REAL
);
"""


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    return conn


def reset(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    # Drop the table instead of unlinking: Windows locks log.db if a
    # previous run_agent in this process has not fully released the file.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS decisions")
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def log_decision(conn: sqlite3.Connection, *,
                 payment_id: str,
                 attempt_number: int,
                 timestamp: str,
                 failure_class: str,
                 chosen_action: str,
                 action_args: dict,
                 gate_result: str,
                 gate_reason: str,
                 executed: bool,
                 flagged_for_review: bool = False,
                 outcome: str | None = None,
                 cost: float | None = None) -> None:
    conn.execute(
        """INSERT INTO decisions (
            payment_id, attempt_number, timestamp, failure_class,
            chosen_action, action_args, gate_result, gate_reason,
            executed, flagged_for_review, outcome, cost
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            payment_id, attempt_number, timestamp, failure_class,
            chosen_action, json.dumps(action_args), gate_result, gate_reason,
            int(executed), int(flagged_for_review), outcome, cost,
        ),
    )


def fetch_payment(conn: sqlite3.Connection, payment_id: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM decisions WHERE payment_id = ? ORDER BY id",
        (payment_id,),
    )
    return list(cur.fetchall())


def count_flagged(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT payment_id) FROM decisions WHERE flagged_for_review = 1"
    ).fetchone()
    return int(row[0])


def count_rejections(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE executed = 0"
    ).fetchone()
    return int(row[0])
