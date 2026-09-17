"""Milestone 1 signal: a 10/30 SMA crossover.

Deliberately stupid, per SPEC.md: "When something breaks you know it is
the plumbing." Returns the same Signal type Kronos will return at
milestone 2 - forecast() is the only function that changes when it does.

def forecast(bars: pd.DataFrame, symbol: str) -> Signal
"""
from __future__ import annotations

import pandas as pd

from src.types import Signal

FAST = 10
SLOW = 30
CONFIDENCE = 0.6  # hardcoded per SPEC.md milestone 1


def forecast(bars: pd.DataFrame, symbol: str, horizon_bars: int = 4) -> Signal:
    if len(bars) < SLOW + 1:
        raise ValueError(f"need at least {SLOW + 1} bars for a 10/30 crossover, got {len(bars)}")

    closes = bars["close"]
    sma_fast = closes.rolling(FAST).mean()
    sma_slow = closes.rolling(SLOW).mean()

    last_fast, last_slow = sma_fast.iloc[-1], sma_slow.iloc[-1]
    ref_price = float(closes.iloc[-1])
    bar_close = bars.index[-1].isoformat()

    if last_fast > last_slow:
        direction = "long"
    elif last_fast < last_slow:
        direction = "short"
    else:
        direction = "flat"

    predicted_pct = float((last_fast - last_slow) / last_slow * 100)

    step = (ref_price * predicted_pct / 100) / horizon_bars
    path = [round(ref_price + step * (i + 1), 8) for i in range(horizon_bars)]
    spread_frac = (1 - CONFIDENCE) * 0.03
    band = [
        (round(p * (1 - spread_frac * (i + 1) ** 0.5), 8), round(p * (1 + spread_frac * (i + 1) ** 0.5), 8))
        for i, p in enumerate(path)
    ]

    context_window = closes.tail(bars.shape[0])
    return Signal(
        symbol=symbol,
        direction=direction,
        predicted_pct=predicted_pct,
        confidence=CONFIDENCE,
        horizon_bars=horizon_bars,
        context_bars=len(context_window),
        ref_price=ref_price,
        bar_close=bar_close,
        context=[round(float(c), 8) for c in context_window],
        path=path,
        band=band,
        model="sma-10-30",
        model_sha=None,
    )
