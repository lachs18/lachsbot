"""Integration tests against the milestone 1 acceptance criteria in SPEC.md.

Network is never involved: ccxt_provider.fetch_ohlcv is monkeypatched to
return deterministic synthetic bars, and the decisions.db path is
redirected to a temp file. This tests the plumbing SPEC.md asks for; it
does not and cannot prove the CCXT/TradersPost integration itself works
against live services from this sandbox (outbound network to arbitrary
hosts is blocked here - see the final summary).
"""
from __future__ import annotations

import json
import logging

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


def test_webhook_rejection_logs_at_error_level(_synthetic_bars, _isolated_db, monkeypatch, caplog):
    """Acceptance criterion 8's error-level log requirement. The code path
    exists at src/cli.py's webhook-rejection branch (logger.error(...));
    this closes the previously-flagged gap where nothing asserted it
    actually fires. webhook.submit is monkeypatched to a fixed rejection
    rather than relying on a real 60s delay, since the point here is the
    logging behavior, not re-proving the reject-after timing (see
    tests/test_webhook.py::test_forced_60_second_delay_is_rejected for that)."""
    from src.types import WebhookResult

    monkeypatch.setattr(
        cli.webhook, "submit",
        lambda *a, **kw: WebhookResult(
            ok=False, status=400, body_sent="{}",
            response_text='{"message":"Rejected: signal older than rejectAfter"}',
            latency_ms=61200,
        ),
    )

    with caplog.at_level(logging.ERROR, logger="kronos1h"):
        cli.run_once()

    error_messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("webhook rejected" in m for m in error_messages)


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


def _sent_decision(conn, symbol="BTC/USD", action="buy", quantity=0.01, ref_price=139.0):
    """Writes one decision all the way through a successful webhook send -
    the state `reconcile` looks for. Used to test reconcile in isolation
    from run_once, since reconcile only cares about already-sent rows."""
    from src.log import writer as log_writer
    from src.signal.sma import forecast
    from src.types import Allocation, WebhookResult

    sig = forecast(make_bars(n=40, step=1.0), symbol)
    decision_id = log_writer.create_decision(conn, sig, "kronos-1h", "paper")
    alloc = Allocation(
        symbol=symbol, signal=sig, target_weight=0.05, quantity=quantity,
        ref_price=ref_price, action=action, order_type="limit",
    )
    log_writer.record_allocation(conn, decision_id, alloc)
    log_writer.record_execution(
        conn, decision_id, ref_price,
        WebhookResult(ok=True, status=200, body_sent="{}", response_text="ok", latency_ms=10),
        {"data": 0, "inference": 0, "risk": 0, "network": 10}, 10,
    )
    return decision_id


def test_reconcile_exits_nonzero_on_unmatched_order(_isolated_db, monkeypatch):
    """Acceptance criterion 7, against the broker-API design (see the
    docstring on src.cli.reconcile for why this targets the broker
    instead of TradersPost, which has no order-history API)."""
    conn = db_module.connect(_isolated_db)
    decision_id = _sent_decision(conn)
    conn.close()

    monkeypatch.setattr(cli.broker_client, "load_client_from_env", lambda *a, **kw: object())
    monkeypatch.setattr(cli.broker_client, "fetch_closed_orders", lambda client, symbol, since_ms=None: [])

    assert cli.reconcile() == 1

    conn = db_module.connect(_isolated_db)
    row = conn.execute("SELECT outcome FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["outcome"] == "failed"  # mark_reconciliation_failure flags the unmatched order
    conn.close()


def test_reconcile_clean_when_every_sent_order_is_matched(_isolated_db, monkeypatch):
    conn = db_module.connect(_isolated_db)
    _sent_decision(conn, quantity=0.01)
    conn.close()

    monkeypatch.setattr(cli.broker_client, "load_client_from_env", lambda *a, **kw: object())
    monkeypatch.setattr(
        cli.broker_client, "fetch_closed_orders",
        lambda client, symbol, since_ms=None: [{"side": "buy", "filled": 0.01}],
    )

    assert cli.reconcile() == 0


def test_reconcile_refuses_a_write_scoped_key(_isolated_db, monkeypatch):
    """check_read_only() must run before any order-history call, so a
    trade-capable key never gets used even accidentally - and reconcile
    fails cleanly (logged, non-zero exit) rather than crashing."""
    from src.broker.client import BrokerKeyNotReadOnly

    def fake_load(*a, **kw):
        raise BrokerKeyNotReadOnly("the configured broker API key has trade or transfer permission")

    monkeypatch.setattr(cli.broker_client, "load_client_from_env", fake_load)
    assert cli.reconcile() == 1
