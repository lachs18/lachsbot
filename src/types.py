"""Shared data contracts between layers. No layer's decision logic lives here.

Per SPEC.md "Interfaces between layers": each layer exposes one typed
function and knows nothing about its neighbours' internals. These
dataclasses are the only thing that crosses a layer boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    symbol: str
    direction: str            # 'long' | 'short' | 'flat'
    predicted_pct: float      # forecast move over the horizon, percent
    confidence: float         # 0..1
    horizon_bars: int
    context_bars: int
    ref_price: float
    bar_close: str            # ISO 8601 UTC, the bar this signal is about
    context: list[float]      # closes fed to the model
    path: list[float]         # predicted closes, one per horizon bar
    band: list[tuple[float, float]]  # [lo, hi] per predicted step
    model: str = "sma-10-30"
    model_sha: str | None = None


@dataclass
class Refusal:
    symbol: str
    signal: Signal
    reason: str                # mandatory: see SPEC.md "rules the writer must enforce"
    stage: int = 2              # 1 = stopped at signal/confidence, 2 = stopped at risk sizing


@dataclass
class Allocation:
    symbol: str
    signal: Signal
    target_weight: float
    quantity: float
    ref_price: float
    action: str                 # 'buy' | 'sell' | 'exit'
    order_type: str             # 'market' | 'limit'
    limit_price: float | None = None
    tolerance_bps: float = 0.0


@dataclass
class PortfolioState:
    equity_usd: float
    positions: dict[str, float] = field(default_factory=dict)   # symbol -> quantity held
    bars_held: dict[str, int] = field(default_factory=dict)     # symbol -> bars since entry, for the time-based exit rule
    gross_exposure: float = 0.0                                   # fraction of equity, before this cycle's orders


@dataclass
class WebhookResult:
    ok: bool
    status: int | None
    body_sent: str            # exact JSON string sent, verbatim - never re-serialised on read
    response_text: str        # exact response text received, verbatim
    latency_ms: int
    error: str | None = None
