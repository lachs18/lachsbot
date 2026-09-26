"""Milestone 2 phase 1: log-only mode (SPEC.md 'Log-only bring-up').

The one guarantee this whole mode exists for: nothing reaches TradersPost.
webhook.submit is the only function in the codebase that ever makes a
network call to TradersPost, so proving it is never invoked in mode='log'
is the acceptance test for the entire phase - everything else (data,
signal, risk, log) is unchanged milestone 1 plumbing.
"""
from __future__ import annotations

import pytest

from src import cli
from src.log import db as db_module


def test_default_mode_is_log(_synthetic_bars, _isolated_db):
    """cli.run_once() with no --mode must never touch the webhook, even if
    a webhook URL happens to be configured - 'paper' is opt-in, not the
    fallback for a forgotten flag."""
    cli.run_once()
    conn = db_module.connect(_isolated_db)
    outcomes = {r["outcome"] for r in conn.execute("SELECT outcome FROM decisions").fetchall()}
    conn.close()
    assert outcomes == {"logged"}  # confidence 0.6 clears the floor, sizing succeeds, mode='log' stops it there


def test_log_mode_never_calls_webhook_submit(_synthetic_bars, _isolated_db, monkeypatch):
    """The core proof: even with a tradeable signal that would reach
    execution in mode='paper', webhook.submit is never called in mode='log'."""

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("webhook.submit was called in mode='log' - this must never happen")

    monkeypatch.setattr(cli.webhook, "submit", _fail_if_called)

    result = cli.run_once(mode="log")

    assert result == 0
    conn = db_module.connect(_isolated_db)
    rows = conn.execute("SELECT outcome, webhook_status, webhook_body FROM decisions").fetchall()
    conn.close()
    assert len(rows) > 0
    assert any(r["outcome"] == "logged" for r in rows)
    for row in rows:
        assert row["webhook_status"] is None
        assert row["webhook_body"] is None


def test_log_mode_records_limit_price_without_sending_it(_synthetic_bars, _isolated_db):
    """Log-only rows still carry the computed limit_price (useful for later
    review/replay) - it is only the network call that is skipped."""
    cli.run_once(mode="log")
    conn = db_module.connect(_isolated_db)
    logged = conn.execute("SELECT * FROM decisions WHERE outcome='logged'").fetchall()
    conn.close()
    assert len(logged) > 0
    for row in logged:
        if row["action"] == "buy" and row["order_type"] == "limit":
            assert row["limit_price"] is not None
        assert row["stopped_at"] == 2  # stage 3 (network) never ran - would be dishonest to claim it did


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match="mode must be one of"):
        cli.run_once(mode="live")
