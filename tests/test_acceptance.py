"""Integration tests against the milestone 1 acceptance criteria in SPEC.md.

Network is never involved: ccxt_provider.fetch_ohlcv is monkeypatched to
return deterministic synthetic bars, and the decisions.db path is
redirected to a temp file. This tests the plumbing SPEC.md asks for; it
does not and cannot prove the CCXT/TradersPost integration itself works
against live services from this sandbox (outbound network to arbitrary
hosts is blocked here - see the final summary).
"""
from __future__ import annotations

import csv
import json

import pandas as pd
import pytest

import src.log.db as db_module
from src import cli
from src.data import ccxt_provider
from tests.conftest import make_bars


def _fresh_bars(n: int = 40, step: float = 1.0) -> pd.DataFrame:
    """Bars anchored to real 'now' so the staleness kill switch does not
    fire in these tests - make_bars()'s fixed historical date is meant for
    tests that never go through validate_bars() (e.g. test_signal.py)."""
    now = pd.Timestamp.now(tz="UTC").floor("h")
    idx = pd.date_range(end=now, periods=n, freq="1h")
    closes = [100.0 + step * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [10.0] * n,
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    real_connect = db_module.connect
    db_path = tmp_path / "decisions.db"
    monkeypatch.setattr(db_module, "connect", lambda *a, **kw: real_connect(db_path))
    monkeypatch.delenv("TRADERSPOST_WEBHOOK_URL_CRYPTO", raising=False)
    return db_path


@pytest.fixture()
def _synthetic_bars(monkeypatch):
    def fake_fetch(symbol, timeframe, exchange, limit=512):
        return _fresh_bars(n=40, step=1.0)

    monkeypatch.setattr(ccxt_provider, "fetch_ohlcv", fake_fetch)


def test_run_once_writes_one_row_per_symbol_including_non_traders(_synthetic_bars, _isolated_db):
    """Acceptance criterion 1."""
    cli.run_once()
    conn = db_module.connect(_isolated_db)
    universe, _ = cli.load_config()
    symbols = [s["symbol"] for s in universe["symbols"]]
    rows = conn.execute("SELECT symbol FROM decisions").fetchall()
    assert sorted(r["symbol"] for r in rows) == sorted(symbols)
    conn.close()


def test_run_once_every_non_filled_row_has_a_reason(_synthetic_bars, _isolated_db):
    """Acceptance criterion 2. With no webhook URL configured, every row
    should end up 'failed' with a reason - none should be silently blank."""
    cli.run_once()
    conn = db_module.connect(_isolated_db)
    bad = conn.execute(
        "SELECT * FROM decisions WHERE outcome != 'filled' AND (reason IS NULL OR reason = '')"
    ).fetchall()
    assert bad == []
    outcomes = {r["outcome"] for r in conn.execute("SELECT outcome FROM decisions").fetchall()}
    assert outcomes == {"failed"}  # confidence 0.6 clears the floor, sizing succeeds, only the webhook has nowhere to go
    conn.close()


def test_run_once_populates_latency_and_stage_ms_on_every_row(_synthetic_bars, _isolated_db):
    """Acceptance criterion 6."""
    cli.run_once()
    conn = db_module.connect(_isolated_db)
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    assert len(rows) > 0
    for row in rows:
        assert row["latency_ms"] is not None
        stage_ms = json.loads(row["stage_ms"])
        assert set(stage_ms) == {"data", "inference", "risk", "network"}
    conn.close()


def test_crash_mid_run_leaves_stopped_at_matching_last_completed_stage(monkeypatch, _synthetic_bars, _isolated_db):
    """Acceptance criterion 4: a crash leaves a truncated, honest chain -
    no row claims a stage it never reached."""
    call_count = {"n": 0}
    original_forecast = cli.sma.forecast

    def flaky_forecast(bars, symbol, horizon_bars=4):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash mid-cycle")
        return original_forecast(bars, symbol, horizon_bars=horizon_bars)

    monkeypatch.setattr(cli.sma, "forecast", flaky_forecast)

    with pytest.raises(RuntimeError):
        cli.run_once()

    conn = db_module.connect(_isolated_db)
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    assert len(rows) == 1  # only the symbol processed before the crash got a row
    assert rows[0]["stopped_at"] == 1
    assert rows[0]["outcome"] == "pending"  # truncated, not silently completed - that truncation is the diagnostic
    conn.close()


def test_reconcile_exits_nonzero_on_unmatched_order(_synthetic_bars, _isolated_db, tmp_path, monkeypatch):
    """Acceptance criterion 7, against the export-file substitute for
    TradersPost's (currently nonexistent) order-history API - see the
    docstring on src.cli.reconcile for why this deviates from SPEC.md."""
    monkeypatch.setenv("TRADERSPOST_WEBHOOK_URL_CRYPTO", "http://127.0.0.1:1/unused")
    # webhook.submit will fail (nothing listening on port 1) - still exercises the row shape
    cli.run_once()

    conn = db_module.connect(_isolated_db)
    sent_ids = [r["id"] for r in conn.execute("SELECT id FROM decisions").fetchall()]
    conn.close()

    # Force one row into a "sent" state as if the webhook had succeeded,
    # since our fake URL above cannot actually accept a connection.
    conn = db_module.connect(_isolated_db)
    conn.execute("UPDATE decisions SET webhook_status=200 WHERE id=?", (sent_ids[0],))
    conn.commit()
    conn.close()

    export_path = tmp_path / "traderspost_orders.csv"
    with open(export_path, "w", newline="") as f:
        writer_csv = csv.DictWriter(f, fieldnames=["decision_id", "ticker", "status"])
        writer_csv.writeheader()
        # the export is empty of any row for our one "sent" decision - it must be reported unmatched

    exit_code = cli.reconcile(str(export_path))
    assert exit_code == 1

    conn = db_module.connect(_isolated_db)
    still_sent = conn.execute("SELECT id, outcome FROM decisions WHERE id=?", (sent_ids[0],)).fetchone()
    assert still_sent["outcome"] == "failed"  # mark_reconciliation_failure flags the unmatched order
    conn.close()


def test_reconcile_clean_when_every_sent_order_is_matched(_synthetic_bars, _isolated_db, tmp_path):
    conn = db_module.connect(_isolated_db)
    from src.log import writer as log_writer
    from src.signal.sma import forecast
    from src.types import WebhookResult

    sig = forecast(make_bars(n=40, step=1.0), "BTC/USD")
    decision_id = log_writer.create_decision(conn, sig, "kronos-1h", "paper")
    log_writer.record_execution(
        conn, decision_id, sig.ref_price,
        WebhookResult(ok=True, status=200, body_sent="{}", response_text="ok", latency_ms=10),
        {"data": 0, "inference": 0, "risk": 0, "network": 10}, 10,
    )
    conn.close()

    export_path = tmp_path / "orders.csv"
    with open(export_path, "w", newline="") as f:
        writer_csv = csv.DictWriter(f, fieldnames=["decision_id", "ticker", "status"])
        writer_csv.writeheader()
        writer_csv.writerow({"decision_id": decision_id, "ticker": "BTC/USD", "status": "filled"})

    assert cli.reconcile(str(export_path)) == 0
