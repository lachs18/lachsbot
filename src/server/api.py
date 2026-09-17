"""Pure query functions backing the read-only JSON API.

No POST routes exist anywhere in this package, by construction - the
terminal cannot cause anything to happen (SPEC.md).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_risk_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())


def get_decisions(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM decisions ORDER BY bar_close DESC, created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_decision(conn: sqlite3.Connection, decision_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    forecast = conn.execute("SELECT * FROM forecasts WHERE decision_id=?", (decision_id,)).fetchone()
    if forecast:
        result["forecast"] = dict(forecast)
    outcome = conn.execute("SELECT * FROM outcomes WHERE decision_id=?", (decision_id,)).fetchone()
    if outcome:
        result["outcome_detail"] = dict(outcome)
    return result


def get_funnel(conn: sqlite3.Connection, date: str = "today") -> dict:
    day = datetime.now(UTC).strftime("%Y-%m-%d") if date == "today" else date
    rows = conn.execute(
        "SELECT * FROM decisions WHERE substr(bar_close, 1, 10) = ?", (day,)
    ).fetchall()

    n = len(rows)
    stage1_halt = sum(1 for r in rows if r["outcome"] == "halted" and r["stopped_at"] == 1)
    stage2_halt = sum(1 for r in rows if r["outcome"] == "halted" and r["stopped_at"] == 2)
    sent_to_broker = sum(1 for r in rows if r["stopped_at"] >= 3)
    webhook_failed = sum(1 for r in rows if r["outcome"] == "failed")
    filled = sum(1 for r in rows if r["outcome"] == "filled")
    cleared_confidence = n - stage1_halt
    received_size = cleared_confidence - stage2_halt

    return {
        "date": day,
        "stages": [
            {"n": n, "label": "signals evaluated"},
            {"n": cleared_confidence, "label": "cleared confidence"},
            {"n": received_size, "label": "received a size"},
            {"n": sent_to_broker, "label": "sent to broker"},
            {"n": filled, "label": "filled"},
        ],
        "drops": [
            {"count": stage1_halt, "label": "forecast below confidence floor", "mild": True},
            {"count": stage2_halt, "label": "weight under minimum ticket, exposure cap, or unsupported direction", "mild": True},
            {"count": webhook_failed, "label": "rejected after the webhook was sent", "mild": False},
        ],
    }


def get_positions(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT symbol, action, fill_qty, fill_price, bar_close, venue
           FROM decisions WHERE outcome='filled' ORDER BY bar_close ASC, created_at ASC"""
    ).fetchall()
    open_positions: dict[str, dict] = {}
    for r in rows:
        if r["action"] == "buy":
            open_positions[r["symbol"]] = {
                "symbol": r["symbol"],
                "qty": r["fill_qty"],
                "entry_price": r["fill_price"],
                "entered_at": r["bar_close"],
                "venue": r["venue"],
            }
        elif r["action"] == "exit":
            open_positions.pop(r["symbol"], None)

    positions = list(open_positions.values())
    risk_cfg = _load_risk_config()
    equity_usd = risk_cfg["starting_equity_usd"]
    gross_notional = sum(p["qty"] * p["entry_price"] for p in positions)
    return {
        "positions": positions,
        "equity_usd": equity_usd,
        "gross_exposure": (gross_notional / equity_usd) if equity_usd else 0.0,
        "max_gross_exposure": risk_cfg["max_gross_exposure"],
    }


def get_scorecard(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT d.symbol, d.bar_close, d.confidence, d.predicted_pct, d.ref_price,
                  f.path AS forecast_path, f.band AS forecast_band,
                  o.realized_pct, o.realized_path, o.exit_reason, o.closed_at
           FROM outcomes o
           JOIN decisions d ON d.id = o.decision_id
           JOIN forecasts f ON f.decision_id = d.id
           ORDER BY o.closed_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]
