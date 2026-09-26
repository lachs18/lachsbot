"""Bar validation: no gaps, no stale timestamps, no zero-volume rows.

Per SPEC.md step 2: the data layer validates before anything downstream
sees the bars. A validation failure is raised, never silently patched.
"""
from __future__ import annotations

import pandas as pd


class BarValidationError(ValueError):
    pass


def validate_bars(
    bars: pd.DataFrame,
    timeframe_minutes: int,
    now: pd.Timestamp | None = None,
    stale_max_intervals: int = 2,
) -> None:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise BarValidationError(f"missing columns: {sorted(missing)}")

    if bars.empty:
        raise BarValidationError("no bars returned")

    if not bars.index.is_monotonic_increasing:
        raise BarValidationError("bars are not sorted by time")

    expected_step = pd.Timedelta(minutes=timeframe_minutes)
    gaps = bars.index.to_series().diff().dropna()
    bad_gaps = gaps[gaps != expected_step]
    if not bad_gaps.empty:
        raise BarValidationError(f"gap(s) in bars at {[str(t) for t in bad_gaps.index]}")

    zero_volume = bars.index[bars["volume"] <= 0]
    if len(zero_volume) > 0:
        raise BarValidationError(f"zero-volume bar(s) at {[str(t) for t in zero_volume]}")

    if now is not None:
        stale_after = bars.index[-1] + expected_step * stale_max_intervals
        if now > stale_after:
            raise BarValidationError(
                f"newest bar {bars.index[-1]} is stale relative to now={now} "
                f"(stale_data_max_intervals={stale_max_intervals} kill switch)"
            )
