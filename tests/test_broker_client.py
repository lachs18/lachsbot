"""Tests for the read-only broker client used only by cli.py reconcile.

These are all against mocked exchange clients: no real broker key exists
yet (broker choice is still SPEC.md open decision #1), and network to any
exchange is blocked from this sandbox regardless. What's verified here is
our own logic - that a write-scoped key is rejected and a view-only key is
accepted - not that a real Coinbase key actually behaves as documented.
"""
from __future__ import annotations

import pytest

from src.broker.client import BrokerKeyNotReadOnly, check_read_only, load_client_from_env


class _FakeClient:
    id = "coinbase"

    def __init__(self, permissions):
        self._permissions = permissions

    def v3_private_get_brokerage_key_permissions(self):
        return self._permissions


def test_check_read_only_raises_when_key_can_trade():
    client = _FakeClient({"can_view": True, "can_trade": True, "can_transfer": False})
    with pytest.raises(BrokerKeyNotReadOnly):
        check_read_only(client)


def test_check_read_only_raises_when_key_can_transfer():
    client = _FakeClient({"can_view": True, "can_trade": False, "can_transfer": True})
    with pytest.raises(BrokerKeyNotReadOnly):
        check_read_only(client)


def test_check_read_only_passes_for_view_only_key():
    client = _FakeClient({"can_view": True, "can_trade": False, "can_transfer": False})
    check_read_only(client)  # must not raise


def test_check_read_only_refuses_a_client_with_no_known_permission_check():
    class _UnknownClient:
        id = "some_other_exchange"

    with pytest.raises(BrokerKeyNotReadOnly):
        check_read_only(_UnknownClient())


def test_load_client_from_env_raises_when_env_vars_missing(monkeypatch):
    monkeypatch.delenv("BROKER_API_KEY_CRYPTO", raising=False)
    monkeypatch.delenv("BROKER_API_SECRET_CRYPTO", raising=False)
    with pytest.raises(RuntimeError):
        load_client_from_env("coinbase", "BROKER_API_KEY_CRYPTO", "BROKER_API_SECRET_CRYPTO")


def test_load_client_from_env_checks_permissions_before_returning(monkeypatch):
    monkeypatch.setenv("BROKER_API_KEY_CRYPTO", "fake-key")
    monkeypatch.setenv("BROKER_API_SECRET_CRYPTO", "fake-secret")

    fake_client = _FakeClient({"can_view": True, "can_trade": True, "can_transfer": False})
    import src.broker.client as client_module
    monkeypatch.setattr(client_module, "build_client", lambda *a, **kw: fake_client)

    with pytest.raises(BrokerKeyNotReadOnly):
        load_client_from_env("coinbase", "BROKER_API_KEY_CRYPTO", "BROKER_API_SECRET_CRYPTO")
