from __future__ import annotations

import pytest

from src.signal.sma import forecast
from tests.conftest import make_bars


def test_forecast_direction_long_on_uptrend():
    bars = make_bars(n=40, start_price=100.0, step=1.0)
    sig = forecast(bars, "BTC/USD", horizon_bars=4)
    assert sig.direction == "long"
    assert sig.confidence == 0.6
    assert sig.horizon_bars == 4
    assert len(sig.path) == 4
    assert len(sig.band) == 4
    assert sig.symbol == "BTC/USD"


def test_forecast_direction_short_on_downtrend():
    bars = make_bars(n=40, start_price=200.0, step=-1.0)
    sig = forecast(bars, "ETH/USD")
    assert sig.direction == "short"
    assert sig.predicted_pct < 0


def test_forecast_requires_enough_bars():
    bars = make_bars(n=10)
    with pytest.raises(ValueError):
        forecast(bars, "SOL/USD")


def test_forecast_bar_close_matches_last_index():
    bars = make_bars(n=40)
    sig = forecast(bars, "BTC/USD")
    assert sig.bar_close == bars.index[-1].isoformat()
