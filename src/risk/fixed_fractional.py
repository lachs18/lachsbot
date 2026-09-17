"""Milestone 1 sizing: fixed fractional. skfolio replaces this file's role
at milestone 4 without changing allocate()'s signature.

def allocate(signals: list[Signal], state: PortfolioState) -> list[Allocation | Refusal]

Receives the whole candidate set in one call - per SPEC.md, "sizing five
candidates independently and summing them is how exposure caps get blown."
Exposure is accumulated across this one call, not across separate calls.
"""
from __future__ import annotations

from src.types import Allocation, PortfolioState, Refusal, Signal


def allocate(
    signals: list[Signal],
    state: PortfolioState,
    *,
    position_fraction: float,
    min_ticket_usd: float,
    max_gross_exposure: float,
    confidence_floor: float,
    exit_after_bars: int,
) -> list[Allocation | Refusal]:
    results: list[Allocation | Refusal] = []
    cumulative_exposure = state.gross_exposure

    for sig in signals:
        held_qty = state.positions.get(sig.symbol, 0.0)
        bars_held = state.bars_held.get(sig.symbol, 0)

        if sig.confidence < confidence_floor:
            results.append(
                Refusal(
                    sig.symbol,
                    sig,
                    f"Forecast confidence {sig.confidence:.2f} is below the "
                    f"{confidence_floor:.2f} floor. No size requested.",
                    stage=1,
                )
            )
            continue

        should_exit = held_qty > 0 and (sig.direction in ("short", "flat") or bars_held >= exit_after_bars)
        if should_exit:
            bits = []
            if sig.direction in ("short", "flat"):
                bits.append(f"signal reversed to {sig.direction}")
            if bars_held >= exit_after_bars:
                bits.append(f"held {bars_held} bars >= exit_after_bars={exit_after_bars}")
            results.append(
                Allocation(
                    symbol=sig.symbol,
                    signal=sig,
                    target_weight=0.0,
                    quantity=held_qty,
                    ref_price=sig.ref_price,
                    action="exit",
                    order_type="market",
                )
            )
            continue

        if held_qty > 0:
            results.append(
                Refusal(sig.symbol, sig, f"Already holding {held_qty} {sig.symbol}; no scaling in at milestone 1.")
            )
            continue

        if sig.direction == "flat":
            results.append(Refusal(sig.symbol, sig, "Direction is flat; no position warranted."))
            continue

        if sig.direction == "short":
            results.append(
                Refusal(
                    sig.symbol,
                    sig,
                    "Short entries are not supported on this spot crypto universe; "
                    "no existing long position to exit.",
                )
            )
            continue

        notional = state.equity_usd * position_fraction
        if notional < min_ticket_usd:
            results.append(
                Refusal(
                    sig.symbol,
                    sig,
                    f"Fixed-fractional weight {position_fraction:.1%} (${notional:.2f}) is under "
                    f"the ${min_ticket_usd:.2f} minimum ticket.",
                )
            )
            continue

        if cumulative_exposure + position_fraction > max_gross_exposure + 1e-9:  # float-safe cap comparison
            results.append(
                Refusal(
                    sig.symbol,
                    sig,
                    f"Gross exposure would exceed the {max_gross_exposure:.0%} cap "
                    f"({cumulative_exposure:.1%} committed already this cycle). No weight assigned.",
                )
            )
            continue

        cumulative_exposure += position_fraction
        quantity = round(notional / sig.ref_price, 8)
        results.append(
            Allocation(
                symbol=sig.symbol,
                signal=sig,
                target_weight=position_fraction,
                quantity=quantity,
                ref_price=sig.ref_price,
                action="buy",
                order_type="limit",
            )
        )

    return results
