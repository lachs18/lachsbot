"""CCXT-backed OHLCV provider with a local SQLite cache.

Build decision: CCXT hourly OHLCV, same source for backtest and live,
cached locally so replay is deterministic. This module is the only place
in the repo allowed to import ccxt - swapping the data provider later
means touching this file alone.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "bars_cache.db"

TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def _cache_conn() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bars (
          exchange  TEXT NOT NULL,
          symbol    TEXT NOT NULL,
          timeframe TEXT NOT NULL,
          ts        TEXT NOT NULL,
          open      REAL NOT NULL,
          high      REAL NOT NULL,
          low       REAL NOT NULL,
          close     REAL NOT NULL,
          volume    REAL NOT NULL,
          PRIMARY KEY (exchange, symbol, timeframe, ts)
        )
        """
    )
    conn.commit()
    return conn


def _rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")


def _read_cache(conn: sqlite3.Connection, exchange: str, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM bars
           WHERE exchange=? AND symbol=? AND timeframe=?
           ORDER BY ts DESC LIMIT ?""",
        (exchange, symbol, timeframe, limit),
    ).fetchall()
    rows.reverse()
    return _rows_to_df(rows)


def _write_cache(conn: sqlite3.Connection, exchange: str, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    rows = [
        (exchange, symbol, timeframe, ts.isoformat(), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
        for ts, r in df.iterrows()
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO bars
           (exchange, symbol, timeframe, ts, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def fetch_ohlcv(symbol: str, timeframe: str, exchange: str, limit: int = 512) -> pd.DataFrame:
    """Return the last `limit` closed bars for symbol, indexed by UTC ts, oldest first.

    Tries the live exchange first, then falls back to the local cache so a
    network outage degrades to stale-but-known data rather than a crash -
    the staleness itself is caught by validate_bars, not hidden here. A
    cache miss with no network is a hard error.
    """
    import ccxt  # lazy import: keeps ccxt confined to this one module

    conn = _cache_conn()
    try:
        client = getattr(ccxt, exchange)()
        raw = client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts")
        _write_cache(conn, exchange, symbol, timeframe, df)
        return df
    except Exception as exc:
        cached = _read_cache(conn, exchange, symbol, timeframe, limit)
        if cached.empty:
            raise RuntimeError(
                f"fetch_ohlcv failed for {symbol} on {exchange} and no cached bars exist: {exc}"
            ) from exc
        return cached
    finally:
        conn.close()


def replay_ohlcv(symbol: str, timeframe: str, exchange: str, before: str, limit: int = 512) -> pd.DataFrame:
    """Read exactly what was cached at or before `before`, for deterministic replay (milestone 3)."""
    conn = _cache_conn()
    try:
        rows = conn.execute(
            """SELECT ts, open, high, low, close, volume FROM bars
               WHERE exchange=? AND symbol=? AND timeframe=? AND ts<=?
               ORDER BY ts DESC LIMIT ?""",
            (exchange, symbol, timeframe, before, limit),
        ).fetchall()
        rows.reverse()
        if not rows:
            raise RuntimeError(f"no cached bars for {symbol} at or before {before}")
        return _rows_to_df(rows)
    finally:
        conn.close()
