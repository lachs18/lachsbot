"""The only module in the repo allowed to write to decisions.db.

Two rules from SPEC.md drive every function here:
- the log is written before the action, not after - each stage commits as
  it completes, so a crash mid-run leaves a truncated, honest chain
- rows are append-and-update only, nothing is ever deleted

Any row left with outcome in ('halted', 'failed') and an empty reason is a
bug in the caller, not something this module will paper over.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from src.types import Allocation, Refusal, Signal, WebhookResult


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_decision(conn: sqlite3.Connection, signal: Signal, strategy: str, mode: str) -> str:
    """Stage 1. Called for every symbol in the universe, including ones that
    will not trade - a skipped symbol is an event, not an absence."""
    decision_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO decisions
           (id, bar_close, created_at, symbol, strategy, mode,
            direction, predicted_pct, confidence, horizon_bars, context_bars,
            stopped_at, outcome, reason, latency_ms, stage_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            decision_id, signal.bar_close, _now(), signal.symbol, strategy, mode,
            signal.direction, signal.predicted_pct, signal.confidence,
            signal.horizon_bars, signal.context_bars,
            1, "pending", None, 0,
            json.dumps({"data": 0, "inference": 0, "risk": 0, "network": 0}),
        ),
    )
    conn.execute(
        """INSERT INTO forecasts (decision_id, context, path, band, model, model_sha)
           VALUES (?,?,?,?,?,?)""",
        (decision_id, json.dumps(signal.context), json.dumps(signal.path), json.dumps(signal.band),
         signal.model, signal.model_sha),
    )
    conn.commit()
    return decision_id


def create_data_halt(
    conn: sqlite3.Connection,
    symbol: str,
    bar_close: str,
    strategy: str,
    mode: str,
    reason: str,
    stage_ms: dict,
    latency_ms: int,
) -> str:
    """A symbol whose bars failed fetch or validation before any signal could
    run. Still gets a row - a skipped symbol is an event, not an absence.
    No forecasts row: no model ran, so there is no forecast to store."""
    decision_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO decisions
           (id, bar_close, created_at, symbol, strategy, mode, stopped_at, outcome, reason, latency_ms, stage_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, bar_close, _now(), symbol, strategy, mode, 1, "halted", reason, latency_ms, json.dumps(stage_ms)),
    )
    conn.commit()
    return decision_id


def record_refusal(conn: sqlite3.Connection, decision_id: str, refusal: Refusal, stage_ms: dict, latency_ms: int) -> None:
    """Stage 1 or 2 halt. reason is not optional - see SPEC.md."""
    if not refusal.reason:
        raise ValueError("a refusal without a stated reason is the thing this system exists to prevent")
    conn.execute(
        """UPDATE decisions SET stopped_at=?, outcome='halted', reason=?, latency_ms=?, stage_ms=? WHERE id=?""",
        (refusal.stage, refusal.reason, latency_ms, json.dumps(stage_ms), decision_id),
    )
    conn.commit()


def record_allocation(conn: sqlite3.Connection, decision_id: str, alloc: Allocation) -> None:
    """Stage 2 pass-through. Execution stage decides the final outcome."""
    conn.execute(
        """UPDATE decisions
           SET target_weight=?, quantity=?, ref_price=?, action=?, order_type=?, stopped_at=2
           WHERE id=?""",
        (alloc.target_weight, alloc.quantity, alloc.ref_price, alloc.action, alloc.order_type, decision_id),
    )
    conn.commit()


def record_execution(
    conn: sqlite3.Connection,
    decision_id: str,
    limit_price: float | None,
    result: WebhookResult,
    stage_ms: dict,
    latency_ms: int,
) -> None:
    """Stage 3. webhook_body/webhook_resp are stored verbatim - never re-serialised."""
    conn.execute(
        """UPDATE decisions
           SET limit_price=?, webhook_status=?, webhook_body=?, webhook_resp=?, stage_ms=?, latency_ms=?, stopped_at=3
           WHERE id=?""",
        (limit_price, result.status, result.body_sent, result.response_text,
         json.dumps(stage_ms), latency_ms, decision_id),
    )
    if result.ok:
        conn.execute("UPDATE decisions SET outcome='pending' WHERE id=?", (decision_id,))
    else:
        reason = result.error or f"webhook rejected: HTTP {result.status}: {result.response_text}"
        conn.execute("UPDATE decisions SET outcome='failed', reason=? WHERE id=?", (reason, decision_id))
    conn.commit()


def record_fill(conn: sqlite3.Connection, decision_id: str, fill_qty: float, fill_price: float, fill_at: str, venue: str) -> None:
    """Stage 4. TradersPost has no live order/fill API today (see cli.py
    reconcile), so this is backfilled from an order-history export rather
    than read back synchronously inside run-once."""
    conn.execute(
        """UPDATE decisions SET fill_qty=?, fill_price=?, fill_at=?, venue=?, stopped_at=4, outcome='filled'
           WHERE id=?""",
        (fill_qty, fill_price, fill_at, venue, decision_id),
    )
    conn.commit()


def record_outcome(
    conn: sqlite3.Connection,
    decision_id: str,
    closed_at: str,
    realized_pct: float,
    realized_path: list[float],
    exit_reason: str,
) -> None:
    """Written when a position closes, joined by the terminal's scorecard panels."""
    conn.execute(
        """INSERT OR REPLACE INTO outcomes (decision_id, closed_at, realized_pct, realized_path, exit_reason)
           VALUES (?,?,?,?,?)""",
        (decision_id, closed_at, realized_pct, json.dumps(realized_path), exit_reason),
    )
    conn.commit()


def mark_reconciliation_failure(conn: sqlite3.Connection, decision_id: str, reason: str) -> None:
    """Never overwrites a row already confirmed filled."""
    conn.execute(
        "UPDATE decisions SET outcome='failed', reason=? WHERE id=? AND outcome!='filled'",
        (reason, decision_id),
    )
    conn.commit()
