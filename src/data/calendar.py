"""Trading-calendar abstraction.

The scheduler asks a Calendar object whether now is a valid time to run a
cycle. It never branches on asset_class directly, so a second asset class
(e.g. stocks, with session hours) plugs in a new Calendar implementation
without touching the scheduler.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Calendar(Protocol):
    def is_open(self, at: datetime) -> bool: ...
    def next_bar_close(self, after: datetime, timeframe_minutes: int) -> datetime: ...


class NullCalendar:
    """Always open. This is crypto's calendar for the milestone 1 universe."""

    def is_open(self, at: datetime) -> bool:
        return True

    def next_bar_close(self, after: datetime, timeframe_minutes: int) -> datetime:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        elapsed_minutes = (after - epoch).total_seconds() / 60
        step_index = int(elapsed_minutes // timeframe_minutes) + 1
        return epoch + timedelta(minutes=step_index * timeframe_minutes)


def calendar_from_config(name: str | None) -> Calendar:
    if name is None:
        return NullCalendar()
    raise ValueError(
        f"unknown calendar {name!r}: only the null calendar (crypto) is implemented at milestone 1"
    )
