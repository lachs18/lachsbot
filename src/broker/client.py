"""Read-only broker access, used only by `cli.py reconcile`.

This is a deliberate, narrow exception to "TradersPost owns broker
credentials" (see SPEC.md "Secrets"), made necessary because TradersPost
does not expose an order-history API - confirmed via TradersPost's own
blog: still roadmap/waitlist as of writing. TradersPost's own guidance
for this kind of check is to reconcile against the broker's API instead.

Hard rule: this module only ever calls fetch_* methods. It never places,
modifies, or cancels an order, and check_read_only() refuses to proceed
if the configured key can trade or transfer funds at all - a write-scoped
key is a configuration error, not something reconcile works around.
"""
from __future__ import annotations

import os


class BrokerKeyNotReadOnly(RuntimeError):
    """The configured broker API key can trade or transfer funds.
    Reconciliation refuses to run rather than let a trade-capable key
    sit in this codebase's config."""


def build_client(exchange_id: str, api_key: str, api_secret: str):
    import ccxt  # lazy import: keeps ccxt confined to the modules that need it

    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})


def check_read_only(client) -> None:
    """Raises BrokerKeyNotReadOnly unless the key is view-only.

    Coinbase Advanced Trade exposes key scope at
    GET /api/v3/brokerage/key_permissions, returning can_view/can_trade/
    can_transfer/can_receive. ccxt maps this as the implicit method
    v3_private_get_brokerage_key_permissions (confirmed present in the
    installed ccxt build this repo pins). A different configured exchange
    would need its own permission-check call here - this function is
    Coinbase-specific by design, not a generic abstraction over a
    capability most exchanges expose differently or not at all.

    PENDING REAL VERIFICATION: never called against a live key, since no
    broker account exists yet (SPEC.md open decision #1 is still open).
    """
    method = getattr(client, "v3_private_get_brokerage_key_permissions", None)
    if method is None:
        raise BrokerKeyNotReadOnly(
            f"{client.id} has no known key-permission check wired up in this module - "
            "refusing to use an unverified key rather than assume it is read-only"
        )
    permissions = method()
    if permissions.get("can_trade") or permissions.get("can_transfer"):
        raise BrokerKeyNotReadOnly(
            "the configured broker API key has trade or transfer permission. "
            "Reconciliation requires a read-only (view-only) key - generate a new one "
            "scoped to view-only and update BROKER_API_KEY_CRYPTO / BROKER_API_SECRET_CRYPTO."
        )


def fetch_closed_orders(client, symbol: str, since_ms: int | None = None) -> list[dict]:
    return client.fetch_closed_orders(symbol, since=since_ms)


def load_client_from_env(exchange_id: str, api_key_env: str, api_secret_env: str):
    api_key = os.environ.get(api_key_env, "")
    api_secret = os.environ.get(api_secret_env, "")
    if not api_key or not api_secret:
        raise RuntimeError(
            f"{api_key_env} / {api_secret_env} not set - reconciliation needs a read-only broker key (see .env.example)"
        )
    client = build_client(exchange_id, api_key, api_secret)
    check_read_only(client)
    return client
