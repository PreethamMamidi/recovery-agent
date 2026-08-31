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
CREATE INDEX IF NOT EXISTS idx_decisions_payment ON decisions(payment_id);
"""


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def reset(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    # Drop the table instead of unlinking: Windows locks log.db if a
    # previous run_agent in this process has not fully released the file.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS decisions")
    conn.executescript(SCHEMA)
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


def count_close_reasons(conn: sqlite3.Connection) -> dict[str, int]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT json_extract(action_args, '$.reason') AS reason, COUNT(*) AS n
           FROM decisions
           WHERE executed = 1 AND chosen_action IN ('mark_uncollectible', 'escalate')
           GROUP BY 1"""
    )
    out: dict[str, int] = {}
    for row in cur:
        key = row["reason"] or "unspecified"
        out[key] = int(row["n"])
    return out


def count_gate_reasons(conn: sqlite3.Connection) -> dict[str, int]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT gate_reason, COUNT(*) AS n
           FROM decisions WHERE executed = 0
           GROUP BY gate_reason"""
    )
    return {str(row["gate_reason"]): int(row["n"]) for row in cur}


def print_trace(rows: list, payment_id: str) -> None:
    """Same columns as eval.run_agent's end-to-end trace."""
    print(f"\n  audit log  {payment_id}  ({len(rows)} decisions)")
    print(f"  {'at':<22}{'class':<22}{'action':<28}{'gate':<10}{'reason'}")
    print("  " + "-" * 90)
    for r in rows:
        print(f"  {r['timestamp']:<22}{r['failure_class']:<22}"
              f"{r['chosen_action']:<28}{r['gate_result']:<10}{r['gate_reason']}")
