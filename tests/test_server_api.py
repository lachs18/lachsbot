from __future__ import annotations

from datetime import UTC, datetime

from src.log import writer
from src.server import api
from src.signal.sma import forecast
from src.types import Allocation, Refusal, WebhookResult
from tests.conftest import make_bars


def _sig(symbol, step=1.0):
    return forecast(make_bars(n=40, step=step), symbol)


def test_funnel_counts_match_direct_sql_count(conn):
    """Acceptance criterion 5's invariant at the API layer: the funnel is a
    read of the same rows any direct SQL count would see, not a separate
    running total that can drift from the database."""
    today = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    low_conf_sig = _sig("QQQ/USD")
    low_conf_sig.bar_close = today
    low_conf_sig.confidence = 0.3
    d1 = writer.create_decision(conn, low_conf_sig, "kronos-1h", "paper")
    writer.record_refusal(conn, d1, Refusal("QQQ/USD", low_conf_sig, "confidence too low", stage=1), {"data": 0, "inference": 0, "risk": 0, "network": 0}, 10)

    filled_sig = _sig("BTC/USD")
    filled_sig.bar_close = today
    d2 = writer.create_decision(conn, filled_sig, "kronos-1h", "paper")
    writer.record_execution(
        conn, d2, 61200.0,
        WebhookResult(ok=True, status=200, body_sent="{}", response_text="ok", latency_ms=5), {"data": 0, "inference": 0, "risk": 0, "network": 5}, 20,
    )
    writer.record_fill(conn, d2, 0.01, 61200.0, today, "coinbase paper")

    funnel = api.get_funnel(conn, date="today")
    direct_count = conn.execute("SELECT COUNT(*) as n FROM decisions").fetchone()["n"]
    assert funnel["stages"][0]["n"] == direct_count
    assert funnel["stages"][-1]["n"] == conn.execute("SELECT COUNT(*) as n FROM decisions WHERE outcome='filled'").fetchone()["n"]
    assert funnel["drops"][0]["count"] == 1  # the confidence-floor halt


def test_positions_tracks_buy_then_exit(conn):
    buy_sig = _sig("ETH/USD")
    d1 = writer.create_decision(conn, buy_sig, "kronos-1h", "paper")
    buy_alloc = Allocation(symbol="ETH/USD", signal=buy_sig, target_weight=0.05, quantity=1.0, ref_price=2500.0, action="buy", order_type="limit")
    writer.record_allocation(conn, d1, buy_alloc)
    writer.record_execution(conn, d1, 2500.0, WebhookResult(ok=True, status=200, body_sent="{}", response_text="ok", latency_ms=5), {"data": 0, "inference": 0, "risk": 0, "network": 5}, 20)
    writer.record_fill(conn, d1, 1.0, 2500.0, "2026-09-17T00:00:00+00:00", "coinbase paper")

    positions = api.get_positions(conn)["positions"]
    assert len(positions) == 1
    assert positions[0]["symbol"] == "ETH/USD"

    exit_sig = _sig("ETH/USD", step=-1.0)
    d2 = writer.create_decision(conn, exit_sig, "kronos-1h", "paper")
    exit_alloc = Allocation(symbol="ETH/USD", signal=exit_sig, target_weight=0.0, quantity=1.0, ref_price=2600.0, action="exit", order_type="market")
    writer.record_allocation(conn, d2, exit_alloc)
    writer.record_execution(conn, d2, None, WebhookResult(ok=True, status=200, body_sent="{}", response_text="ok", latency_ms=5), {"data": 0, "inference": 0, "risk": 0, "network": 5}, 20)
    writer.record_fill(conn, d2, 1.0, 2600.0, "2026-09-17T01:00:00+00:00", "coinbase paper")

    positions_after = api.get_positions(conn)["positions"]
    assert positions_after == []


def test_get_decision_includes_forecast(conn):
    sig = _sig("SOL/USD")
    decision_id = writer.create_decision(conn, sig, "kronos-1h", "paper")
    result = api.get_decision(conn, decision_id)
    assert result["symbol"] == "SOL/USD"
    assert "forecast" in result
    assert result["forecast"]["model"] == "sma-10-30"


def test_get_decision_missing_returns_none(conn):
    assert api.get_decision(conn, "does-not-exist") is None


def test_positions_response_includes_exposure_metadata(conn):
    result = api.get_positions(conn)
    assert set(result) == {"positions", "equity_usd", "gross_exposure", "max_gross_exposure"}
    assert result["gross_exposure"] == 0.0  # no open positions yet
