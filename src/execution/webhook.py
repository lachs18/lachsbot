"""Builds and posts the TradersPost webhook. Never decides whether to trade.

Payload fields are grounded in TradersPost's published webhook reference
(action, orderType, quantity, limitPrice, rejectAfter, cancelAfter, time,
metadata) - see https://docs.traderspost.io/docs/developer-resources/webhook-reference
Two things worth knowing before this goes live:

- rejectAfter only takes effect if "Allow signal overrides" and the matching
  "reject entry/exit if signal is older than" toggle are enabled on the
  TradersPost strategy itself. Setting it here is necessary but not
  sufficient - flip that switch in the TradersPost UI too.
- Per SPEC.md: rejectAfter is a crash detector, not a price guard. Price is
  protected by limitPrice; rejectAfter is set to the maximum (30) and a
  rejection is treated as an alert, never a routine miss.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

from src.types import Allocation, WebhookResult


def compute_limit_price(alloc: Allocation, tolerance_bps: float) -> float | None:
    """Entries are limit orders at ref_price plus a tolerance band. Exits are
    market orders - the cost of not exiting outweighs a slightly worse fill."""
    if alloc.action != "buy" or alloc.order_type != "limit":
        return None
    return round(alloc.ref_price * (1 + tolerance_bps / 10_000), 8)


def build_payload(
    alloc: Allocation,
    decision_id: str,
    limit_price: float | None,
    now: datetime,
    traderspost_ticker: str,
    reject_after: int = 30,
    cancel_after: int | None = 1800,
) -> dict:
    """`traderspost_ticker` must be the already-translated ticker from
    config/universe.yaml (e.g. BTC-USD), never `alloc.symbol` (the internal
    ccxt notation, e.g. BTC/USD) - see that file for why the mapping lives
    there and not as a substitution here. Checked, not assumed: a slash
    reaching this far means the caller skipped the config lookup."""
    if not 1 <= reject_after <= 30:
        raise ValueError("rejectAfter must be 1..30 per TradersPost's own limits")
    if "/" in traderspost_ticker:
        raise ValueError(
            f"ticker {traderspost_ticker!r} looks like internal ccxt notation, not a TradersPost "
            "ticker - use the traderspost_ticker mapping in config/universe.yaml, not alloc.symbol"
        )

    payload: dict = {
        "ticker": traderspost_ticker,
        "action": alloc.action,
        "orderType": alloc.order_type,
        "time": now.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "rejectAfter": reject_after,
        "metadata": {"decision_id": decision_id, "confidence": alloc.signal.confidence},
    }
    if alloc.action != "exit":
        payload["quantity"] = alloc.quantity
    if limit_price is not None:
        payload["limitPrice"] = limit_price
        if cancel_after is not None:
            payload["cancelAfter"] = cancel_after
    return payload


def _scrub(text: str, url: str) -> str:
    """Nothing is logged that contains the webhook URL, including on error (SPEC.md)."""
    return text.replace(url, "<webhook-url>") if url and text else text


def submit(
    alloc: Allocation,
    decision_id: str,
    webhook_url: str,
    tolerance_bps: float,
    traderspost_ticker: str,
    reject_after: int = 30,
    cancel_after: int | None = 1800,
    timeout_s: float = 10.0,
    now: datetime | None = None,
) -> WebhookResult:
    now = now or datetime.now(UTC)
    limit_price = compute_limit_price(alloc, tolerance_bps)
    payload = build_payload(alloc, decision_id, limit_price, now, traderspost_ticker, reject_after, cancel_after)
    body = json.dumps(payload)

    if not webhook_url:
        return WebhookResult(
            ok=False, status=None, body_sent=body, response_text="", latency_ms=0,
            error="no TradersPost webhook URL configured for this universe (see .env.example)",
        )

    req = urllib.request.Request(
        webhook_url, data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            response_text = resp.read().decode("utf-8", errors="replace")
            latency_ms = int((time.perf_counter() - start) * 1000)
            return WebhookResult(
                ok=200 <= resp.status < 300, status=resp.status,
                body_sent=body, response_text=response_text, latency_ms=latency_ms,
            )
    except urllib.error.HTTPError as exc:
        response_text = _scrub(exc.read().decode("utf-8", errors="replace"), webhook_url)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return WebhookResult(
            ok=False, status=exc.code, body_sent=body, response_text=response_text, latency_ms=latency_ms,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure becomes a failed WebhookResult, never a crash
        latency_ms = int((time.perf_counter() - start) * 1000)
        return WebhookResult(
            ok=False, status=None, body_sent=body, response_text="", latency_ms=latency_ms,
            error=_scrub(str(exc), webhook_url),
        )
