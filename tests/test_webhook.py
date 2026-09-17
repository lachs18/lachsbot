"""Execution-layer tests.

The fixture server below models TradersPost's *documented* webhook
behaviour (field names from docs.traderspost.io/docs/developer-resources/
webhook-reference, and rejectAfter's documented age-check against the
`time` field) - it is not literal recorded bytes from a real TradersPost
account, because no account exists yet (open decision #1, broker TBD).
Byte-identity and reject-after behaviour are marked PENDING REAL
VERIFICATION and must be re-checked against an actual TradersPost paper
strategy before this is trusted for anything beyond plumbing.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from src.execution.webhook import build_payload, compute_limit_price, submit
from src.signal.sma import forecast
from src.types import Allocation
from tests.conftest import make_bars


def _alloc(action="buy", order_type="limit"):
    sig = forecast(make_bars(n=40, step=1.0), "BTC/USD")
    return Allocation(
        symbol="BTC/USD", signal=sig, target_weight=0.05, quantity=0.01,
        ref_price=sig.ref_price, action=action, order_type=order_type,
    )


def test_compute_limit_price_only_for_limit_buys():
    buy = _alloc(action="buy", order_type="limit")
    assert compute_limit_price(buy, tolerance_bps=15) == pytest.approx(buy.ref_price * 1.0015)

    exit_alloc = _alloc(action="exit", order_type="market")
    assert compute_limit_price(exit_alloc, tolerance_bps=15) is None


def test_build_payload_matches_traderspost_documented_fields():
    alloc = _alloc()
    now = datetime(2026, 9, 17, 13, 0, 0, tzinfo=UTC)
    payload = build_payload(
        alloc, "decision-123", limit_price=61200.5, now=now, traderspost_ticker="BTC-USD",
        reject_after=30, cancel_after=1800,
    )

    assert payload["ticker"] == "BTC-USD"
    assert payload["action"] == "buy"
    assert payload["orderType"] == "limit"
    assert payload["quantity"] == alloc.quantity
    assert payload["limitPrice"] == 61200.5
    assert payload["rejectAfter"] == 30
    assert payload["cancelAfter"] == 1800
    assert payload["time"] == "2026-09-17 13:00:00"
    assert payload["metadata"]["decision_id"] == "decision-123"
    assert payload["metadata"]["confidence"] == alloc.signal.confidence


def test_build_payload_exit_omits_quantity():
    alloc = _alloc(action="exit", order_type="market")
    payload = build_payload(alloc, "d1", None, datetime.now(UTC), traderspost_ticker="BTC-USD")
    assert "quantity" not in payload
    assert "limitPrice" not in payload


def test_reject_after_out_of_range_raises():
    alloc = _alloc()
    with pytest.raises(ValueError):
        build_payload(alloc, "d1", None, datetime.now(UTC), traderspost_ticker="BTC-USD", reject_after=45)


def test_build_payload_rejects_ccxt_notation_ticker():
    """The one guard this module owns: config/universe.yaml does the actual
    translation, but build_payload refuses to send a slash-notation ticker
    regardless, in case a caller ever passes alloc.symbol through by mistake."""
    alloc = _alloc()
    with pytest.raises(ValueError, match="ccxt notation"):
        build_payload(alloc, "d1", None, datetime.now(UTC), traderspost_ticker=alloc.symbol)


def test_build_payload_ticker_never_contains_a_slash():
    """Acceptance test for the TradersPost ticker-notation fix: whatever
    reaches the payload must be TradersPost's hyphen notation, never ccxt's
    slash notation - this must fail if that regresses."""
    alloc = _alloc()
    payload = build_payload(alloc, "d1", None, datetime.now(UTC), traderspost_ticker="BTC-USD")
    assert "/" not in payload["ticker"]
    assert payload["ticker"] == "BTC-USD"


def test_submit_with_no_webhook_url_configured_fails_cleanly():
    alloc = _alloc()
    result = submit(alloc, "d1", webhook_url="", tolerance_bps=15, traderspost_ticker="BTC-USD")
    assert result.ok is False
    assert result.status is None
    assert "webhook url" in result.error.lower()
    assert result.body_sent  # payload is still built and stored, even though nothing was sent


class _FixtureHandler(BaseHTTPRequestHandler):
    received: ClassVar[list[bytes]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).received.append(raw)
        payload = json.loads(raw)

        stale = False
        reject_after = payload.get("rejectAfter")
        time_field = payload.get("time")
        if reject_after and time_field:
            sig_time = datetime.strptime(time_field, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            age = (datetime.now(UTC) - sig_time).total_seconds()
            stale = age > reject_after

        if stale:
            body = json.dumps({"message": "Rejected: signal older than rejectAfter", "code": "SIGNAL_TOO_OLD"}).encode()
            self.send_response(400)
        else:
            body = json.dumps({"status": "accepted"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture()
def fixture_server():
    _FixtureHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join()


def test_webhook_body_is_byte_identical_to_what_the_server_received(fixture_server):
    alloc = _alloc()
    result = submit(alloc, "decision-abc", fixture_server, tolerance_bps=15, traderspost_ticker="BTC-USD", now=datetime.now(UTC))
    assert result.ok is True
    assert result.status == 200
    assert len(_FixtureHandler.received) == 1
    assert _FixtureHandler.received[0] == result.body_sent.encode("utf-8")
    assert json.loads(_FixtureHandler.received[0])["ticker"] == "BTC-USD"


def test_forced_60_second_delay_is_rejected(fixture_server):
    """Acceptance criterion 8: a forced delay produces a rejected webhook."""
    alloc = _alloc()
    stale_now = datetime.now(UTC) - timedelta(seconds=61)
    result = submit(
        alloc, "decision-stale", fixture_server, tolerance_bps=15, traderspost_ticker="BTC-USD",
        reject_after=30, now=stale_now,
    )
    assert result.ok is False
    assert result.status == 400
    assert "older" in result.response_text.lower() or "reject" in result.response_text.lower()
    # the exact bytes sent are still recorded, byte-identical, even on a rejection
    assert _FixtureHandler.received[-1] == result.body_sent.encode("utf-8")
