from __future__ import annotations

from src.risk.fixed_fractional import allocate
from src.signal.sma import forecast
from src.types import Allocation, PortfolioState, Refusal
from tests.conftest import make_bars

RISK_KW = {
    "position_fraction": 0.05,
    "min_ticket_usd": 50.0,
    "max_gross_exposure": 0.60,
    "confidence_floor": 0.55,
    "exit_after_bars": 4,
}


def _sig(symbol="BTC/USD", n=40, start=100.0, step=1.0, confidence=None):
    bars = make_bars(n=n, start_price=start, step=step)
    sig = forecast(bars, symbol)
    if confidence is not None:
        sig.confidence = confidence
    return sig


def test_confidence_floor_refusal_is_stage_1():
    sig = _sig(confidence=0.3)
    state = PortfolioState(equity_usd=10_000)
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Refusal)
    assert result.stage == 1
    assert "confidence" in result.reason.lower()
    assert result.reason  # non-empty, always


def test_long_signal_no_position_gets_sized():
    sig = _sig(step=1.0)
    state = PortfolioState(equity_usd=10_000)
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Allocation)
    assert result.action == "buy"
    assert result.order_type == "limit"
    assert result.quantity > 0
    assert result.target_weight == 0.05


def test_min_ticket_refusal():
    sig = _sig(step=1.0)
    state = PortfolioState(equity_usd=100)  # 5% of 100 = $5, well under $50 min ticket
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Refusal)
    assert result.stage == 2
    assert "minimum ticket" in result.reason


def test_exposure_cap_refuses_later_candidates_in_same_cycle():
    signals = [_sig(symbol=f"SYM{i}/USD", step=1.0) for i in range(20)]
    state = PortfolioState(equity_usd=10_000)  # 20 * 5% = 100% > 60% cap
    results = allocate(signals, state, **RISK_KW)
    allocations = [r for r in results if isinstance(r, Allocation)]
    refusals = [r for r in results if isinstance(r, Refusal)]
    assert len(allocations) == 12  # 12 * 5% = 60%, exactly the cap
    assert len(refusals) == 8
    assert all("exposure" in r.reason.lower() for r in refusals)
    assert all(r.reason for r in refusals)


def test_already_holding_refuses_scale_in():
    sig = _sig(step=1.0)
    state = PortfolioState(equity_usd=10_000, positions={sig.symbol: 1.0}, bars_held={sig.symbol: 1})
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Refusal)
    assert "scaling in" in result.reason


def test_signal_reversal_triggers_exit():
    sig = _sig(step=-1.0)  # direction becomes 'short'
    state = PortfolioState(equity_usd=10_000, positions={sig.symbol: 2.0}, bars_held={sig.symbol: 1})
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Allocation)
    assert result.action == "exit"
    assert result.quantity == 2.0


def test_time_based_exit_after_n_bars():
    sig = _sig(step=1.0)  # still long, but held long enough to force an exit
    state = PortfolioState(equity_usd=10_000, positions={sig.symbol: 2.0}, bars_held={sig.symbol: 4})
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Allocation)
    assert result.action == "exit"


def test_short_direction_with_no_position_is_refused_not_shorted():
    sig = _sig(step=-1.0)
    state = PortfolioState(equity_usd=10_000)
    [result] = allocate([sig], state, **RISK_KW)
    assert isinstance(result, Refusal)
    assert "short" in result.reason.lower()
