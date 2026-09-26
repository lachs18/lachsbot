# Setup: connecting Coinbase, TradersPost, and this repo

Follow this top to bottom. Each step says exactly what to click or type, and how to check it worked before moving to the next one. You should not need to open any code to get through this.

## The shape of it, before you start

There are **two completely separate Coinbase credentials** in this setup. Mixing them up is the one mistake worth understanding before you begin:

| | Lives where | Can it place an order? | Used for |
|---|---|---|---|
| **TradersPost's own Coinbase connection** | Inside TradersPost's settings only | Yes - this is the one that trades | TradersPost placing the actual orders your strategy generates |
| **The reconciliation key** (`BROKER_API_KEY_CRYPTO`) | In this repo's `.env` file | No - must be view-only, the code refuses to use it otherwise | `cli.py reconcile` reading your order history back, to check nothing traded that isn't in the log |

This repo never holds a credential that can trade. That's not a suggestion - `src/broker/client.py` calls Coinbase's own permission-check endpoint before doing anything with the reconciliation key, and refuses to run if that key can trade or transfer funds.

## Step 1: Install the project

```bash
git clone <your-repo-url>
cd lachsbot
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

**Verify it worked:**

```bash
python -m pytest tests/ -q
```

You should see something like `47 passed`. If this doesn't pass, stop here - nothing past this point will work either.

## Step 2: Create a Coinbase account (if you don't have one)

Go to [coinbase.com](https://www.coinbase.com) and create an account, or use an existing one. Coinbase is the confirmed venue for both live/paper execution (via TradersPost) and market data (via this repo, using CCXT) - see `SPEC.md`'s "Open decisions" for why one venue for both matters.

Nothing to verify yet - just confirm you can log in.

## Step 3: Connect Coinbase to TradersPost (the trading credential)

This is the credential that actually places orders. It is configured **entirely inside TradersPost** - you will never paste it into this repo, into `.env`, or anywhere else here.

1. Log into your TradersPost dashboard.
2. Click **Connect Broker** (top right).
3. Choose **Coinbase** and follow TradersPost's prompts to link your account. TradersPost will ask you to authorize the connection from Coinbase's side.
4. When asked, pick your default quote currency (TradersPost supports USD, USDC, USDT, DAI). USD is the simplest choice unless you have a reason to prefer a stablecoin.

**Verify it worked:** in TradersPost's dashboard, your Coinbase connection should show as connected/active under your broker connections list.

## Step 4: Create the TradersPost strategy and get its webhook URL

1. In TradersPost, create a new strategy (or a new strategy subscription on an existing one).
2. Set its **Connected broker** to the Coinbase connection from Step 3.
3. Set the strategy to **paper mode**. Do not go live at this point - `SPEC.md` is explicit that milestone 1 runs on paper end to end first.
4. TradersPost will show you a webhook URL for this strategy - a `https://webhooks.traderspost.io/...` link. Copy it.
5. Open `.env` in this repo (copy from `.env.example` first if you haven't: `cp .env.example .env`), and set:

   ```
   TRADERSPOST_WEBHOOK_URL_CRYPTO=<the URL you copied>
   ```

**Verify it worked:**

```bash
python -c "from dotenv import dotenv_values; print(dotenv_values('.env').get('TRADERSPOST_WEBHOOK_URL_CRYPTO', 'NOT SET'))"
```

Should print your webhook URL, not `NOT SET` and not empty.

## Step 5: Enable "Allow signal overrides" - this one is easy to skip and breaks a real check if you do

`SPEC.md` sets `rejectAfter: 30` on every webhook payload this repo sends, as a crash detector (see `SPEC.md`'s "rejectAfter is 30, and here is why"). **That field is silently ignored unless a setting is turned on inside TradersPost.** If you skip this step, stale signals will be accepted instead of rejected, and acceptance criterion 8 (a forced delay should produce a rejected webhook) will look broken when the actual problem is this one checkbox.

1. In TradersPost, open the same strategy subscription from Step 4.
2. Find its subscription settings (not the top-level strategy settings - this is per-subscription).
3. Turn on **Allow signal overrides**.
4. Turn on **Reject entry if signal is older than** and **Reject exit if signal is older than** as well - both, since this repo can send either an entry or an exit.

**Verify it worked:** there's no local command that can check a TradersPost UI setting. The real verification is behavioral: once you're running live signals, an artificially delayed one (see `tests/test_webhook.py::test_forced_60_second_delay_is_rejected` for what "delayed" means in code terms) should come back rejected from TradersPost, not accepted. If a stale signal ever gets accepted, this setting is the first thing to check.

## Step 6: Create the read-only reconciliation key

This is the second, separate Coinbase credential - the one that lives in this repo. It must never be able to trade.

1. Log into the [Coinbase Developer Platform](https://portal.cdp.coinbase.com/) (this is different from your regular Coinbase login page, though it uses the same account).
2. Go to **Access → API keys**.
3. Click to create a new key. Give it a nickname you'll recognize, e.g. `kronos-1h-reconcile-readonly`.
4. Under **Permission level**, select **View**. Do **not** select Trade or Transfer.
5. Click **Create & Download**, complete 2FA when prompted.
6. Coinbase downloads a JSON file. Open it - it has two fields you need:
   - `"name"` - looks like `organizations/xxxxxxxx/apiKeys/xxxxxxxx`
   - `"privateKey"` - a multi-line block starting with `-----BEGIN EC PRIVATE KEY-----`
7. In `.env`, set:

   ```
   BROKER_API_KEY_CRYPTO=organizations/xxxxxxxx/apiKeys/xxxxxxxx
   BROKER_API_SECRET_CRYPTO="-----BEGIN EC PRIVATE KEY-----
   ...(the rest of the key, exactly as downloaded)...
   -----END EC PRIVATE KEY-----"
   ```

   The private key is multi-line - keep the double quotes around it so it's read as one value. See `.env.example` for the same shape.

**Verify the key is present and read-only, in one command:**

```bash
python -c "
from src.broker.client import load_client_from_env, BrokerKeyNotReadOnly
try:
    load_client_from_env('coinbase', 'BROKER_API_KEY_CRYPTO', 'BROKER_API_SECRET_CRYPTO')
    print('OK: key loaded and confirmed read-only')
except BrokerKeyNotReadOnly as e:
    print('FAILED - key can trade or transfer:', e)
except RuntimeError as e:
    print('FAILED - not set up yet:', e)
"
```

- `OK: key loaded and confirmed read-only` → done with this step.
- `FAILED - key can trade or transfer` → delete the key in the Coinbase Developer Platform and create a new one with **View** permission only.
- `FAILED - not set up yet` → `.env` isn't set or isn't being picked up - double check the two variable names match exactly and that you're running the command from the repo root (so `.env` is found).

## Step 7: Check the whole chain end to end

Once Steps 1-6 are done, in order:

```bash
# 1. Run one full cycle against the live Coinbase data feed and your
#    paper TradersPost strategy. Needs real network access to Coinbase
#    and TradersPost - this will not work from a sandboxed environment
#    with outbound network restrictions.
python -m src.cli run-once

# 2. Start the terminal's local API server
python -m src.server.app
# then open http://127.0.0.1:8787 in a browser
```

The terminal should show the decisions `run-once` just made - a real row per symbol in `config/universe.yaml`, not the placeholder "no decisions logged yet" notice.

```bash
# 3. Once at least one order has actually filled on the TradersPost/Coinbase
#    side, check reconciliation
python -m src.cli reconcile
```

Expect `reconcile OK: N sent decision(s) matched 1:1 against broker order history` in the output. Anything else is a real finding - see `SPEC.md`'s "Reconciliation" section for what each failure mode means.

## Quick reference: what goes where

| Credential | Where it's entered | Where it's stored | Can it trade? |
|---|---|---|---|
| TradersPost's Coinbase connection | TradersPost's own UI (Step 3) | Inside TradersPost only | Yes |
| TradersPost webhook URL | `.env` → `TRADERSPOST_WEBHOOK_URL_CRYPTO` | This repo, gitignored | No (it's a URL, not a key) |
| Reconciliation Coinbase key | `.env` → `BROKER_API_KEY_CRYPTO` / `BROKER_API_SECRET_CRYPTO` | This repo, gitignored | No - enforced in code |

`.env` is gitignored and must never be committed. If you ever suspect a key leaked, revoke it in the Coinbase Developer Platform immediately and issue a new one - regenerating is free and instant.
