from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.log import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "decisions.db")
    yield connection
    connection.close()


def make_bars(n: int = 60, start_price: float = 100.0, step: float = 0.5, freq: str = "1h") -> pd.DataFrame:
    """Deterministic synthetic OHLCV bars for tests - no network involved."""
    idx = pd.date_range("2026-09-01", periods=n, freq=freq, tz="UTC")
    closes = [start_price + step * i for i in range(n)]
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
