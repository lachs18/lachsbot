"""run-once, run-scheduled, replay, reconcile.

This is the only module that wires the five layers together end to end -
each layer module above it stays ignorant of its neighbours (SPEC.md
"Interfaces between layers").
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

from src.data import ccxt_provider
from src.data.calendar import calendar_from_config
from src.data.validate import BarValidationError, validate_bars
from src.execution import webhook
from src.log import db, writer
from src.risk import fixed_fractional
from src.server import api
from src.signal import sma
from src.types import PortfolioState, Refusal

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "kronos-1h"
MODE = "paper"

logger = logging.getLogger("kronos1h")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def load_config() -> tuple[dict, dict]:
    universe = yaml.safe_load((ROOT / "config" / "universe.yaml").read_text())
    risk = yaml.safe_load((ROOT / "config" / "risk.yaml").read_text())
    return universe, risk


def halt_file_present() -> bool:
    return (ROOT / "HALT").exists()


def _load_portfolio_state(conn, equity_usd: float, timeframe_minutes: int, current_bar_close: str) -> PortfolioState:
    positions = api.get_positions(conn)["positions"]
    pos_qty = {p["symbol"]: p["qty"] for p in positions}
    bars_held: dict[str, int] = {}
    now = datetime.fromisoformat(current_bar_close)
    for p in positions:
        entered = datetime.fromisoformat(p["entered_at"])
        bars_held[p["symbol"]] = int((now - entered).total_seconds() // (timeframe_minutes * 60))
    gross_notional = sum(p["qty"] * p["entry_price"] for p in positions)
    gross_exposure = gross_notional / equity_usd if equity_usd else 0.0
    return PortfolioState(equity_usd=equity_usd, positions=pos_qty, bars_held=bars_held, gross_exposure=gross_exposure)


def run_once(bar_close: str | None = None) -> int:
    """One full cycle: fetch, validate, forecast, size, execute, log.
    Returns 0 on success, 1 if the HALT file blocked the run entirely."""
    if halt_file_present():
        logger.error("HALT file present at repo root - refusing to run. Remove it to resume.")
        return 1

    universe_cfg, risk_cfg = load_config()
    exchange = universe_cfg["data_provider"]["exchange"]
    timeframe = universe_cfg["timeframe"]
    timeframe_minutes = ccxt_provider.TIMEFRAME_MINUTES[timeframe]
    context_bars = universe_cfg["context_bars"]
    horizon_bars = risk_cfg["horizon_bars"]
    webhook_url = os.environ.get(universe_cfg["webhook_url_env"], "")

    conn = db.connect()
    entries: dict[str, dict] = {}
    signals = []

    for sym_cfg in universe_cfg["symbols"]:
        symbol = sym_cfg["symbol"]
        t0 = time.perf_counter()
        t_data = 0.0
        try:
            bars = ccxt_provider.fetch_ohlcv(symbol, timeframe, exchange, limit=context_bars)
            t_data = time.perf_counter() - t0
            now_ts = pd.Timestamp(bar_close, tz="UTC") if bar_close else pd.Timestamp.now(tz="UTC")
            validate_bars(bars, timeframe_minutes, now=now_ts, stale_max_intervals=risk_cfg["stale_data_max_intervals"])
        except (BarValidationError, RuntimeError) as exc:
            bc = bar_close or datetime.now(UTC).isoformat()
            stage_ms = {"data": int(t_data * 1000), "inference": 0, "risk": 0, "network": 0}
            writer.create_data_halt(
                conn, symbol, bc, STRATEGY, MODE, f"data layer: {exc}", stage_ms,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            logger.warning("data halt for %s: %s", symbol, exc)
            continue

        t1 = time.perf_counter()
        sig = sma.forecast(bars, symbol, horizon_bars=horizon_bars)
        t_inference = time.perf_counter() - t1

        decision_id = writer.create_decision(conn, sig, STRATEGY, MODE)
        entries[symbol] = {"decision_id": decision_id, "t_data": t_data, "t_inference": t_inference, "t0": t0}
        signals.append(sig)

    if not signals:
        conn.close()
        return 0

    t_risk0 = time.perf_counter()
    portfolio = _load_portfolio_state(conn, risk_cfg["starting_equity_usd"], timeframe_minutes, signals[0].bar_close)
    results = fixed_fractional.allocate(
        signals,
        portfolio,
        position_fraction=risk_cfg["position_fraction"],
        min_ticket_usd=risk_cfg["min_ticket_usd"],
        max_gross_exposure=risk_cfg["max_gross_exposure"],
        confidence_floor=risk_cfg["confidence_floor"],
        exit_after_bars=risk_cfg["exit_after_bars"],
    )
    t_risk = time.perf_counter() - t_risk0

    tol_by_symbol = {s["symbol"]: s["tolerance_bps"] for s in universe_cfg["symbols"]}
    consecutive_failures = 0
    failure_limit = risk_cfg["consecutive_webhook_failures_halt"]

    for result in results:
        e = entries[result.symbol]
        decision_id = e["decision_id"]
        stage_ms = {"data": int(e["t_data"] * 1000), "inference": int(e["t_inference"] * 1000), "risk": int(t_risk * 1000), "network": 0}
        latency_ms = int((time.perf_counter() - e["t0"]) * 1000)

        if isinstance(result, Refusal):
            writer.record_refusal(conn, decision_id, result, stage_ms, latency_ms)
            continue

        writer.record_allocation(conn, decision_id, result)

        if halt_file_present():
            writer.record_refusal(
                conn, decision_id,
                Refusal(result.symbol, result.signal, "HALT file present; new entries blocked mid-cycle.", stage=2),
                stage_ms, latency_ms,
            )
            continue
        if consecutive_failures >= failure_limit:
            writer.record_refusal(
                conn, decision_id,
                Refusal(result.symbol, result.signal,
                        f"{consecutive_failures} consecutive webhook failures; halted per kill switch.", stage=2),
                stage_ms, latency_ms,
            )
            continue

        tolerance_bps = tol_by_symbol.get(result.symbol, 0.0)
        t_net0 = time.perf_counter()
        wh = webhook.submit(result, decision_id, webhook_url, tolerance_bps, reject_after=30, cancel_after=1800)
        stage_ms["network"] = int((time.perf_counter() - t_net0) * 1000)
        latency_ms = int((time.perf_counter() - e["t0"]) * 1000)

        limit_price = webhook.compute_limit_price(result, tolerance_bps)
        writer.record_execution(conn, decision_id, limit_price, wh, stage_ms, latency_ms)

        if wh.ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            # Per SPEC.md: every rejection is an alert, not a routine miss.
            logger.error("webhook rejected for %s: status=%s response=%s", result.symbol, wh.status, wh.response_text)

    conn.close()
    return 0


def run_scheduled(poll_interval_s: int = 5) -> None:
    universe_cfg, _ = load_config()
    cal = calendar_from_config(universe_cfg["calendar"])
    timeframe_minutes = ccxt_provider.TIMEFRAME_MINUTES[universe_cfg["timeframe"]]
    logger.info("run-scheduled: waiting for the next bar close (calendar=%s)", universe_cfg["calendar"])
    while True:
        now = datetime.now(UTC)
        if not cal.is_open(now):
            time.sleep(poll_interval_s)
            continue
        next_close = cal.next_bar_close(now, timeframe_minutes)
        wait_s = max((next_close - now).total_seconds() + 3, poll_interval_s)
        time.sleep(wait_s)
        bar_close_iso = next_close.astimezone(UTC).isoformat()
        logger.info("running cycle for bar_close=%s", bar_close_iso)
        try:
            run_once(bar_close=bar_close_iso)
        except Exception:
            logger.exception("run_once crashed for bar_close=%s", bar_close_iso)


def replay(bar_close: str) -> int:
    """Recompute signal for a past bar from the local cache (deterministic)
    and diff against what is already logged. Milestone 1 ships the signal
    half of this check; milestone 3 extends it through execution once a
    backtester exists to compare against."""
    universe_cfg, risk_cfg = load_config()
    exchange = universe_cfg["data_provider"]["exchange"]
    timeframe = universe_cfg["timeframe"]
    context_bars = universe_cfg["context_bars"]
    horizon_bars = risk_cfg["horizon_bars"]

    conn = db.connect()
    mismatches = []
    checked = 0
    for sym_cfg in universe_cfg["symbols"]:
        symbol = sym_cfg["symbol"]
        logged = conn.execute(
            "SELECT * FROM decisions WHERE symbol=? AND bar_close=? ORDER BY created_at DESC LIMIT 1",
            (symbol, bar_close),
        ).fetchone()
        if logged is None or logged["direction"] is None:
            continue
        checked += 1
        try:
            bars = ccxt_provider.replay_ohlcv(symbol, timeframe, exchange, before=bar_close, limit=context_bars)
            sig = sma.forecast(bars, symbol, horizon_bars=horizon_bars)
        except Exception as exc:  # noqa: BLE001 - any replay failure is itself a reportable mismatch
            mismatches.append(f"{symbol}: replay failed: {exc}")
            continue
        if logged["direction"] != sig.direction or abs((logged["predicted_pct"] or 0.0) - sig.predicted_pct) > 1e-6:
            mismatches.append(
                f"{symbol}: logged direction={logged['direction']} predicted_pct={logged['predicted_pct']} "
                f"but replay gives direction={sig.direction} predicted_pct={sig.predicted_pct}"
            )
    conn.close()

    if mismatches:
        logger.error("replay mismatch for bar_close=%s:", bar_close)
        for m in mismatches:
            logger.error("  - %s", m)
        return 1
    logger.info("replay of %s: %d logged decision(s) reproduced exactly.", bar_close, checked)
    return 0


def reconcile(export_path: str) -> int:
    """Compare a TradersPost order-history export against the decisions table.

    DEVIATION FROM SPEC.md, flagged rather than improvised silently:
    TradersPost does not currently expose a public API for order/account
    history - as of writing it is still roadmap/waitlist only. The spec's
    reconcile command assumed a live pull; that cannot be built as written.
    This is the practical substitute: point it at a CSV export from the
    TradersPost dashboard's order history. The exact export column names,
    and whether metadata.decision_id round-trips into it at all, are
    UNVERIFIED without a real TradersPost account - confirm against a real
    export before trusting this in place of the live API, and revisit once
    TradersPost ships the waitlisted API.

    Expected columns: an "id" or "decision_id" column, or a "metadata"
    column containing the JSON blob we sent (which may itself have a
    "decision_id" key) - configurable below.
    """
    conn = db.connect()
    sent = conn.execute(
        "SELECT id, symbol FROM decisions WHERE stopped_at>=3 AND webhook_status BETWEEN 200 AND 299"
    ).fetchall()
    sent_ids = {r["id"] for r in sent}

    matched_ids: set[str] = set()
    unmatched_export_rows: list[dict] = []

    with open(export_path, newline="") as f:
        for row in csv.DictReader(f):
            decision_id = row.get("decision_id") or row.get("id")
            if not decision_id and row.get("metadata"):
                try:
                    decision_id = json.loads(row["metadata"]).get("decision_id")
                except (json.JSONDecodeError, AttributeError):
                    decision_id = None

            if decision_id and decision_id in sent_ids:
                matched_ids.add(decision_id)
            else:
                unmatched_export_rows.append(row)

    unmatched_decisions = sent_ids - matched_ids

    if unmatched_export_rows or unmatched_decisions:
        logger.error(
            "reconcile FAILED: %d export row(s) with no matching decision_id, "
            "%d sent decision(s) with no matching export row",
            len(unmatched_export_rows), len(unmatched_decisions),
        )
        for row in unmatched_export_rows:
            logger.error("  unmatched order in export: %s", row)
        for decision_id in unmatched_decisions:
            writer.mark_reconciliation_failure(conn, decision_id, "no matching order found in TradersPost export")
            logger.error("  unmatched decision, no order found in export: %s", decision_id)
        conn.close()
        return 1

    logger.info("reconcile OK: %d order(s) matched against %d sent decision(s)", len(matched_ids), len(sent_ids))
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_once = sub.add_parser("run-once")
    p_once.add_argument("--bar-close", default=None)

    p_sched = sub.add_parser("run-scheduled")
    p_sched.add_argument("--poll-interval", type=int, default=5)

    p_replay = sub.add_parser("replay")
    p_replay.add_argument("bar_close")

    p_recon = sub.add_parser("reconcile")
    p_recon.add_argument("export_path")

    args = parser.parse_args(argv)

    if args.command == "run-once":
        return run_once(bar_close=args.bar_close)
    if args.command == "run-scheduled":
        run_scheduled(poll_interval_s=args.poll_interval)
        return 0
    if args.command == "replay":
        return replay(args.bar_close)
    if args.command == "reconcile":
        return reconcile(args.export_path)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
