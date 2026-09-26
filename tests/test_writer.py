from __future__ import annotations

import json

import pytest

from src.log import writer
from src.signal.sma import forecast
from src.types import Refusal, WebhookResult
from tests.conftest import make_bars


def _sig(symbol="BTC/USD"):
    return forecast(make_bars(n=40, step=1.0), symbol)


def test_create_decision_writes_decision_and_forecast_row(conn):
    sig = _sig()
    decision_id = writer.create_decision(conn, sig, "kronos-1h", "paper")

    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row is not None
    assert row["symbol"] == "BTC/USD"
    assert row["outcome"] == "pending"
    assert row["stopped_at"] == 1
    assert row["latency_ms"] == 0
    assert json.loads(row["stage_ms"]) == {"data": 0, "inference": 0, "risk": 0, "network": 0}

    forecast_row = conn.execute("SELECT * FROM forecasts WHERE decision_id=?", (decision_id,)).fetchone()
    assert forecast_row is not None
    assert json.loads(forecast_row["path"]) == sig.path


def test_refusal_without_reason_raises(conn):
    sig = _sig()
    decision_id = writer.create_decision(conn, sig, "kronos-1h", "paper")
    bad_refusal = Refusal(sig.symbol, sig, "", stage=1)
    with pytest.raises(ValueError):
        writer.record_refusal(conn, decision_id, bad_refusal, {"data": 0, "inference": 0, "risk": 0, "network": 0}, 10)


def test_halted_row_always_has_reason(conn):
    sig = _sig()
    decision_id = writer.create_decision(conn, sig, "kronos-1h", "paper")
    refusal = Refusal(sig.symbol, sig, "Forecast confidence 0.30 is below the 0.55 floor. No size requested.", stage=1)
    writer.record_refusal(conn, decision_id, refusal, {"data": 1, "inference": 2, "risk": 0, "network": 0}, 15)

    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["outcome"] == "halted"
    assert row["reason"]
    assert row["stopped_at"] == 1
    assert json.loads(row["stage_ms"]) == {"data": 1, "inference": 2, "risk": 0, "network": 0}


def test_no_halted_or_failed_row_has_empty_reason(conn):
    """Acceptance criterion 2, at the writer level: this is the invariant
    the whole system exists to enforce."""
    sig = _sig()
    decision_id = writer.create_decision(conn, sig, "kronos-1h", "paper")
    writer.record_execution(
        conn, decision_id, None,
        WebhookResult(ok=False, status=400, body_sent="{}", response_text="rejected", latency_ms=5, error=None),
        {"data": 1, "inference": 1, "risk": 1, "network": 5}, 20,
    )
    bad_rows = conn.execute(
        "SELECT * FROM decisions WHERE outcome IN ('halted','failed') AND (reason IS NULL OR reason = '')"
    ).fetchall()
    assert bad_rows == []


def test_data_halt_creates_row_without_forecast(conn):
    decision_id = writer.create_data_halt(
        conn, "SOL/USD", "2026-09-17T00:00:00+00:00", "kronos-1h", "paper",
        "data layer: gap(s) in bars", {"data": 3, "inference": 0, "risk": 0, "network": 0}, 12,
    )
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["outcome"] == "halted"
    assert row["reason"]
    assert row["direction"] is None
    forecast_row = conn.execute("SELECT * FROM forecasts WHERE decision_id=?", (decision_id,)).fetchone()
    assert forecast_row is None
