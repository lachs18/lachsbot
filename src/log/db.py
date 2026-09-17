"""SQLite schema for decisions / forecasts / outcomes.

This is the contract with the terminal - see SPEC.md 'Event schema'.
Changing a column name here means changing the terminal in the same commit.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "decisions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  id            TEXT PRIMARY KEY,
  bar_close     TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  strategy      TEXT NOT NULL,
  mode          TEXT NOT NULL,

  direction     TEXT,
  predicted_pct REAL,
  confidence    REAL,
  horizon_bars  INTEGER,
  context_bars  INTEGER,

  target_weight REAL,
  quantity      REAL,
  ref_price     REAL,

  action         TEXT,
  order_type     TEXT,
  limit_price    REAL,
  webhook_status INTEGER,
  webhook_body   TEXT,
  webhook_resp   TEXT,

  fill_qty      REAL,
  fill_price    REAL,
  fill_at       TEXT,
  venue         TEXT,

  stopped_at    INTEGER NOT NULL,
  outcome       TEXT NOT NULL,
  reason        TEXT,
  latency_ms    INTEGER NOT NULL,
  stage_ms      TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_bar ON decisions(bar_close DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol, bar_close DESC);

CREATE TABLE IF NOT EXISTS forecasts (
  decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
  context     TEXT NOT NULL,
  path        TEXT NOT NULL,
  band        TEXT NOT NULL,
  model       TEXT NOT NULL,
  model_sha   TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
  decision_id   TEXT PRIMARY KEY REFERENCES decisions(id),
  closed_at     TEXT NOT NULL,
  realized_pct  REAL NOT NULL,
  realized_path TEXT NOT NULL,
  exit_reason   TEXT NOT NULL
);
"""


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
