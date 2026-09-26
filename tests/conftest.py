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


def _fresh_bars(n: int = 40, step: float = 1.0) -> pd.DataFrame:
    """Bars anchored to real 'now' so the staleness kill switch does not
    fire in tests that go through validate_bars() (e.g. cli.run_once) -
    make_bars()'s fixed historical date is for tests that never validate
    staleness (e.g. test_signal.py)."""
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


@pytest.fixture()
def _isolated_db(monkeypatch, tmp_path):
    """Redirects db.connect() to a temp file and clears the webhook URL, so
    cli.run_once tests never touch the real decisions.db or a real webhook."""
    from src.log import db as db_module

    real_connect = db_module.connect
    db_path = tmp_path / "decisions.db"
    monkeypatch.setattr(db_module, "connect", lambda *a, **kw: real_connect(db_path))
    monkeypatch.delenv("TRADERSPOST_WEBHOOK_URL_CRYPTO", raising=False)
    return db_path


@pytest.fixture()
def _synthetic_bars(monkeypatch):
    """Monkeypatches ccxt_provider.fetch_ohlcv to return a deterministic
    uptrend - real enough to clear the SMA crossover's confidence floor and
    reach risk sizing, without any network call."""
    from src.data import ccxt_provider

    def fake_fetch(symbol, timeframe, exchange, limit=512):
        return _fresh_bars(n=40, step=1.0)

    monkeypatch.setattr(ccxt_provider, "fetch_ohlcv", fake_fetch)
