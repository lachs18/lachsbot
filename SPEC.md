# kronos-1h build spec

Hand this to Claude Code as the build target. Written against the decision terminal mock, so the event schema below and the terminal's fields are the same thing.

## Scope

Build a signal-to-broker pipeline for liquid instruments on hourly bars, plus a local terminal that records every decision and the reason behind it.

**Optimise for one thing: nothing reaches the broker that is not explained in the event log first.** If an order appears in TradersPost with no matching decision chain in the local database, that is the highest-severity bug in the system, regardless of whether the trade made money.

### In scope

- Hourly-bar signals over a fixed universe of liquid symbols
- Order routing through TradersPost webhooks only
- A SQLite event log as the single source of truth for decisions
- A local read-only web terminal over that log
- Paper trading end to end before any live connection

### Explicitly out of scope

- Memecoins, DEXes, on-chain execution, wallet handling. Different project, different risk model
- Any code that holds a broker API key or a private key. TradersPost owns broker credentials, this repo never does
- Sub-minute or intrabar logic. TradersPost documents itself as not intended for strategies below the 1-minute timeframe, and the whole design assumes hourly bars
- Order management beyond entry and exit. No trailing logic, no scaling in, no averaging down
- Any web-facing deployment. This runs on localhost and nowhere else

### A non-goal worth stating out loud

This system is not expected to be profitable at milestone 5. It is expected to be legible: every decision reconstructable, every rejection explained, every model claim measurable against what actually happened. Profitability is a later question that cannot be asked honestly until this part works.

## Architecture

Five layers, each with exactly one job. No layer reaches past its neighbour.

| Layer | Owns | Never does |
| --- | --- | --- |
| Data | Fetching and caching OHLCV bars | Decide anything |
| Signal | Turning bars into a direction and a confidence | Know about sizing, orders or brokers |
| Risk | Turning a signal into a quantity, or into a refusal | Know which model produced the signal |
| Execution | Building and posting the webhook, reading the response | Decide whether to trade |
| Terminal | Reading the event log and rendering it | Write to the log, or place an order |

### Flow, one bar close

1. Scheduler wakes a few seconds after the hourly bar closes
2. Data layer pulls the last 512 closed bars per symbol and validates them (no gaps, no stale timestamps, no zero volume rows)
3. Signal layer runs the model once per symbol and returns `direction`, `predicted_move`, `confidence`
4. Every symbol is written to the log at this point, including the ones that will not trade. A skipped symbol is an event, not an absence
5. Risk layer receives the surviving signals as a set, not one at a time, and returns a target quantity per symbol or a documented refusal
6. Execution layer posts one webhook per symbol with a quantity and records the HTTP status and body
7. Fill confirmation is read back and appended to the same decision row

### Two rules that shape everything

**The log is written before the action, not after.** Every stage writes its event as it completes, so a crash mid-run leaves a truncated chain in the database rather than a silent gap. A chain that stops at stage 2 with no stage 3 event is itself the diagnostic.

**The risk layer sees the whole set at once.** Sizing five candidates independently and summing them is how exposure caps get blown. One call in, one allocation out.

## Event schema

This is the contract. The terminal reads these tables and nothing else, so changing a column name here means changing the terminal in the same commit.

SQLite, WAL mode, one file at `data/decisions.db`.

### Table `decisions`

One row per symbol per bar close. Created at stage 3 and updated in place as later stages complete.

```sql
CREATE TABLE decisions (
  id            TEXT PRIMARY KEY,     -- uuid4
  bar_close     TEXT NOT NULL,        -- ISO 8601 UTC, the bar this decision is about
  created_at    TEXT NOT NULL,        -- ISO 8601 UTC, when stage 3 finished
  symbol        TEXT NOT NULL,
  strategy      TEXT NOT NULL,        -- 'kronos-1h'
  mode          TEXT NOT NULL,        -- 'paper' | 'live'

  -- stage 1: signal
  direction     TEXT,                 -- 'long' | 'short' | 'flat'
  predicted_pct REAL,                 -- forecast move over the horizon, percent
  confidence    REAL,                 -- 0..1
  horizon_bars  INTEGER,
  context_bars  INTEGER,

  -- stage 2: risk
  target_weight REAL,                 -- 0..1, null if the signal never reached risk
  quantity      REAL,
  ref_price     REAL,

  -- stage 3: execution
  action        TEXT,                 -- 'buy' | 'sell' | 'exit' | null
  order_type    TEXT,                 -- 'market' | 'limit'
  limit_price   REAL,
  webhook_status INTEGER,             -- HTTP status from TradersPost
  webhook_body  TEXT,                 -- exact JSON sent, verbatim
  webhook_resp  TEXT,                 -- exact response received, verbatim

  -- stage 4: fill
  fill_qty      REAL,
  fill_price    REAL,
  fill_at       TEXT,
  venue         TEXT,

  -- outcome
  stopped_at    INTEGER NOT NULL,     -- 1..4, the last stage reached
  outcome       TEXT NOT NULL,        -- 'filled' | 'halted' | 'failed' | 'pending'
  reason        TEXT,                 -- required whenever outcome != 'filled'
  latency_ms    INTEGER NOT NULL,     -- bar_close to webhook sent
  stage_ms      TEXT                  -- JSON: {"data":n,"inference":n,"risk":n,"network":n}
);

CREATE INDEX idx_decisions_bar ON decisions(bar_close DESC);
CREATE INDEX idx_decisions_symbol ON decisions(symbol, bar_close DESC);
```

### Table `forecasts`

The predicted path, stored so it can be compared against reality later. One row per decision.

```sql
CREATE TABLE forecasts (
  decision_id TEXT PRIMARY KEY REFERENCES decisions(id),
  context     TEXT NOT NULL,   -- JSON array of closes fed to the model
  path        TEXT NOT NULL,   -- JSON array of predicted closes
  band        TEXT NOT NULL,   -- JSON array of [lo, hi] per predicted step
  model       TEXT NOT NULL,   -- 'NeoQuasar/Kronos-small'
  model_sha   TEXT             -- weights hash, so a model swap is visible in the data
);
```

### Table `outcomes`

Written when a position closes. This is what makes the forecast-against-outcome panel possible.

```sql
CREATE TABLE outcomes (
  decision_id  TEXT PRIMARY KEY REFERENCES decisions(id),
  closed_at    TEXT NOT NULL,
  realized_pct REAL NOT NULL,   -- entry to exit, percent
  realized_path TEXT NOT NULL,  -- JSON array of actual closes over the same horizon
  exit_reason  TEXT NOT NULL
);
```

### Rules the writer must enforce

- `reason` is **not nullable in practice**. Any row with `outcome` of `halted` or `failed` and an empty `reason` fails a test. A refusal without a stated reason is the thing this whole system exists to prevent
- `webhook_body` and `webhook_resp` are stored verbatim, never re-serialised. When TradersPost rejects something, the exact bytes are what you need
- Rows are append-and-update only. Nothing is ever deleted. Corrections are new rows referencing the old id
- `stage_ms` is always populated, even on a halt, so the latency picture survives failures

### Terminal API

The terminal is read-only and talks to a tiny local server, not to SQLite directly.

| Endpoint | Returns |
| --- | --- |
| `GET /api/decisions?limit=200` | Recent decisions, newest first |
| `GET /api/decisions/{id}` | One decision plus its forecast and outcome |
| `GET /api/funnel?date=today` | Stage counts and drop reasons for the session |
| `GET /api/positions` | Current open positions as last known |
| `GET /api/scorecard` | Closed decisions joined to outcomes, for the two analysis panels |

No POST routes. The terminal cannot cause anything to happen, by construction.

## TradersPost integration

One strategy in TradersPost, one webhook URL, one asset class. The URL lives in an environment variable and never in the repo.

### Payload

```json
{
  "ticker": "SPY",
  "action": "buy",
  "orderType": "limit",
  "quantity": 14,
  "limitPrice": 612.80,
  "time": "2026-09-17T13:00:00Z",
  "rejectAfter": 30,
  "metadata": { "decision_id": "<uuid>", "confidence": 0.62 }
}
```

`metadata` carries the decision id so a TradersPost order can always be traced back to its row. Put custom fields there rather than at the top level, which is what the field is for.

### rejectAfter is 30, and here is why

`rejectAfter` accepts 1 to 30 seconds and rejects a signal older than that, measured against the `time` field when supplied.

**It is a crash detector, not a price guard.** On hourly bars, ten seconds is 0.28% of the bar. Price barely moves in that window, so a tight cutoff costs real trades and buys almost no protection. What it genuinely catches is the other case: the process hangs, comes back four minutes later, and flushes a queued signal built on a bar the market has long since left behind.

So:

- **Set it to 30.** The maximum the field allows
- **Every rejection is an alert, not a miss.** At 30 seconds, a rejection means something upstream broke. Log it at error level and surface it in the terminal in red. A cutoff that fires routinely is a cutoff you will learn to ignore
- **Protect price with `limitPrice`, not with the clock.** A limit order states directly what you will not pay. A time cutoff only guesses at it
- **Fix inference time rather than budgeting around it.** Measure it from day one and keep it under two seconds. Kronos-small is 24.7M parameters, which is fast on GPU and slow on CPU. If it ever exceeds five seconds, that is a bug ticket, not a tuning knob

### Limit price policy

Entries are limit orders at the reference price plus a tolerance band, expressed in basis points and configurable per asset class. Exits are market orders, because the cost of not exiting is larger than the cost of a slightly worse fill.

Use `cancelAfter` so an unfilled entry does not sit on the book into the next bar and collide with the next decision.

### Reconciliation

**Amended after milestone 1 review.** The original wording below assumed TradersPost exposes an order-history API. It does not: as of writing, TradersPost's own documentation states account and order data access is still roadmap/waitlist only, for every user, regardless of account status. TradersPost's own guidance for this kind of check is to reconcile against the broker's API directly instead.

Once a day, compare the **configured broker's** order history (via CCXT, using a read-only API key - see Secrets) against the `decisions` table. Broker orders do not carry our `decision_id`: TradersPost places the order at the broker on our behalf, and there is no confirmed mechanism for our webhook's `metadata` to survive that hop (unverified - no live broker account exists yet to check). So matching is heuristic rather than exact: a sent decision counts as matched if the broker shows a closed order for the same symbol, same side, and a quantity within 0.5%, filled within a configurable window of when the webhook was sent. Any sent decision with no such match is a critical alert. This is weaker than an exact `decision_id` match would be, but it is what a broker order object actually offers, and it still catches the one failure this check exists for: an order that reached the market with no corresponding row in our own log.

This is the check that enforces the one rule from the scope section, and it needs to exist before going live, not after.

## Repository and dependencies

Python 3.12, `uv` for dependency management, `ruff` for linting. No framework beyond a minimal web server.

```
kronos-1h/
  config/
    universe.yaml        # symbols, asset class, tolerance bands
    risk.yaml            # floors, caps, minimum ticket
  src/
    data/                # bar fetching, caching, validation
    signal/              # model wrapper, returns direction + confidence
    risk/                # skfolio wrapper, returns quantities or refusals
    execution/           # webhook builder and poster
    broker/              # read-only broker API client, used only by reconcile (see Secrets)
    log/                 # SQLite writer, the only module allowed to write
    server/              # read-only JSON API for the terminal
    cli.py               # run-once, run-scheduled, replay, reconcile
  terminal/
    index.html           # the terminal, single file
  tests/
  data/decisions.db
  .env.example
```

### The three repos and what each is allowed to do

| Repo | Role | Hard boundary |
| --- | --- | --- |
| Kronos (MIT) | Loaded inside `src/signal/` as a library | Never imported anywhere else. Swapping it out must touch one module |
| skfolio (BSD-3) | Loaded inside `src/risk/` | Receives returns and constraints, never symbols' meaning or model confidence |
| nautilus_trader (LGPL-3.0) | Backtesting only, from milestone 3 | Never in the live path. Its execution engine is unused, TradersPost is the executor |

Kronos ships `KronosPredictor`, which handles preprocessing, normalisation and inference and needs a DataFrame with `open`, `high`, `low`, `close`, plus optional `volume` and `amount`. `max_context` for Kronos-small and Kronos-base is 512, and inputs should not exceed it.

### Repos deliberately not used

Named here so nobody re-adds them later wondering why they were left out.

- **freqtrade**, **hummingbot** and **vibetrading** each own execution. TradersPost now does that job, and two things competing for it is how orders get duplicated. vibetrading additionally wants a private key in a `.env` beside LLM-generated strategy code, which this design rules out entirely
- **prediction-market-backtesting** targets Kalshi and Polymarket and vendors its own pinned NautilusTrader. Different market, and the pin would fight milestone 3
- If a later milestone genuinely needs one of these, it **replaces** TradersPost rather than sitting beside it. That is a redesign decision, not an addition

### Interfaces between layers

Each layer exposes one function with a typed signature and no knowledge of its neighbours. Written this way, milestone 2 replaces one function body and nothing else.

```python
# signal
def forecast(bars: pd.DataFrame, symbol: str) -> Signal
# risk
def allocate(signals: list[Signal], state: PortfolioState) -> list[Allocation | Refusal]
# execution
def submit(alloc: Allocation) -> WebhookResult
```

A `Refusal` carries a mandatory `reason` string. It is a first-class return value, not an exception, because refusals are data this system is built to collect.

## Milestone 1: prove the pipe

No Kronos, no skfolio, no backtest. A deliberately stupid signal, so that when something breaks you know it is the plumbing.

### Build

- Data layer against one provider, three symbols, hourly bars, with validation
- Signal layer as a 10/30 SMA crossover returning the same `Signal` type Kronos will later return, with `confidence` hardcoded to 0.6
- Risk layer as fixed fractional sizing, 5% of a hardcoded equity figure, with a minimum ticket refusal path
- Execution layer posting to a TradersPost **paper** strategy
- SQLite writer implementing the full schema, including `forecasts` written with a straight-line dummy path
- Read-only JSON API with all five endpoints
- The terminal wired to the API instead of its mock data
- `cli.py run-once` and `cli.py run-scheduled`

### Acceptance criteria

Claude Code checks itself against these. All must pass before milestone 2 starts.

1. `run-once` on a closed bar produces exactly one `decisions` row per symbol in the universe, including symbols that did not trade
2. Every row with `outcome` not equal to `filled` has a non-empty `reason`. A test asserts this and fails the build otherwise
3. `webhook_body` in the database is byte-identical to what TradersPost received
4. Killing the process mid-run leaves a row with `stopped_at` matching the last completed stage, and no orphan order at the broker
5. The terminal renders a real run with no mock data remaining in the file, and its funnel counts match a direct SQL count
6. `latency_ms` and `stage_ms` are populated on every row, including halted ones
7. `cli.py reconcile` compares the **broker's** order history (not TradersPost's - see "Reconciliation" below for why) to the decisions table and exits non-zero on any unmatched order
8. A forced 60-second delay produces a rejected webhook, an error-level log line, and a red row in the terminal

### Done means

You watch a signal travel from bar close to paper fill, and the terminal shows the whole chain with the reason at every step. That is the milestone. Everything after it is swapping components into a frame that already works.

## Milestones 2 to 5

Each has an entry condition. Do not start one before the condition is met, and say so plainly if it is not.

### Milestone 2: Kronos replaces the dummy signal

**Entry condition:** every milestone 1 criterion passing for five consecutive trading days.

Replace the body of `forecast()`. Nothing else changes. Write the real predicted path and confidence band into `forecasts`, define how a probabilistic forecast becomes a single confidence number and document that mapping in the repo, and record `model_sha` so a weights change is visible in the data.

**Done when:** inference is under two seconds per symbol, and the terminal's forecast chart draws from stored paths rather than anything generated client-side.

### Milestone 3: backtest

**Entry condition:** at least 200 real decisions logged, so the live pipeline's behaviour is known before it is simulated.

nautilus_trader over two years of the same universe and timeframe, modelling commission, slippage and the limit-order fill logic from the live path. The backtest must reuse `forecast()` and `allocate()` unchanged. If it needs its own copy of the logic, the layering is wrong and that is the finding.

**Done when:** a replay of the logged live period through the backtester reproduces the logged decisions. Any divergence is a bug in one of the two, and finding out which is the point of this milestone.

### Milestone 4: skfolio sizing

**Entry condition:** milestone 3 reproduces live decisions.

Replace fixed fractional sizing with skfolio across the universe. It receives the full candidate set in one call and returns weights. Estimation window, covariance estimator and constraints all live in `risk.yaml` and are versioned, because they are the parameters most likely to be quietly tuned into overfitting.

**Done when:** gross exposure never exceeds the configured cap in backtest or live, and every zero weight carries a reason.

### Milestone 5: live

**Entry condition:** all of the following, no exceptions.

- Sixty consecutive days on paper with no unexplained decision row
- `reconcile` clean every day for thirty days
- Backtest and live agree on replay
- Kill switches tested by triggering them deliberately, not by reading the code
- Position sizes set so that the maximum loss on any single day is an amount you would shrug at

Start at roughly a tenth of intended size and leave it there for a month. The first month live is a test of the operational setup, not of the strategy.

## Operating rules

### Kill switches

All of these halt new entries and require a manual restart. None of them liquidates automatically, because forced liquidation during an outage is how a bad hour becomes a bad month.

| Trigger | Threshold |
| --- | --- |
| Daily loss | Configurable, checked before every submission |
| Consecutive webhook failures | 3 |
| Stale data | Newest bar older than 2 intervals |
| Reconciliation mismatch | Any unmatched order |
| Manual | A `HALT` file in the repo root, checked every cycle |

The `HALT` file is deliberately crude. It works when the process is wedged, when you are on your phone, and when nothing else does.

### Secrets

- `.env` is gitignored, `.env.example` is committed with empty values
- The repo holds the TradersPost webhook URL and nothing else for the *trading* path. There is no code path in `src/execution/` or `src/signal/` or `src/risk/` that could use a broker key
- Nothing is logged that contains the webhook URL or the broker key, including on error

**Deliberate exception: a read-only broker API key, for reconciliation only.** `src/broker/client.py` holds a broker API key (`BROKER_API_KEY_CRYPTO` / `BROKER_API_SECRET_CRYPTO` in `.env`) so `cli.py reconcile` can pull the broker's own order history - see "Reconciliation" above for why this exists instead of a TradersPost API call. This is a narrower version of the same rule, not a break from it: the key must be scoped read-only (view-only, no trade, no transfer) before it is used, and `check_read_only()` calls the broker's own permission-check endpoint and refuses to run reconciliation otherwise, raising `BrokerKeyNotReadOnly`. `src/broker/client.py` is the only module allowed to hold or use this key, and it never calls an order-placement or order-cancellation method - only `fetch_closed_orders` and the permission check. A key with trade or transfer scope is treated as a configuration error, not something reconcile works around.

### Never in the repo

- Live account numbers, balances, or statements
- Copied model weights. Fetch from Hugging Face by name and record the hash
- Any dependency not in `pyproject.toml`. Crypto tooling is a heavily targeted supply chain and a stray transitive dependency is the usual entry point

### Testing

- Every refusal path has a test asserting the reason string is non-empty
- The webhook builder is tested against recorded TradersPost responses, including rejections, not against a mock you wrote to agree with you
- A replay command reruns any past bar from stored data and must reproduce the logged decision exactly. This is the regression suite that matters

### Swiss context, not a code concern

Automated trading raises transaction frequency by design, which in Switzerland can move you from private capital gains toward *gewerbsmässiger Wertschriftenhandel*, where gains are taxed as income and AHV applies. Frequency, holding period, leverage and use of borrowed capital are the usual criteria. Worth a conversation with a Treuhänder before milestone 5, not after. This is not tax advice.

## Open decisions

Claude Code cannot start milestone 1 without answers to the first three. The rest can be decided while it builds.

### Blocking

1. **Which broker.** This gates everything. TradersPost connects to brokers but does not hold money, so the question is which broker accepts a Swiss resident and is also supported by TradersPost. Interactive Brokers is the one clearly built for it. Alpaca supports many non-US residents but not every jurisdiction and asks you to confirm your country with them directly. Confirm the broker first, then confirm TradersPost supports it, in that order
2. **Asset class and universe.** Stocks, futures or crypto, and which 5 to 10 symbols. This determines the data provider, the trading calendar, and whether the scheduler runs 24/7 or on session hours
3. **Data provider.** Broker feed, or a separate source. Affects cost, history depth and whether backtest and live see the same bars, which matters more than it sounds

### Decide during milestone 1

4. **Starting capital and daily loss limit.** Both go in config from the first commit, even on paper, so the kill switch is exercised from day one
5. **Confidence floor.** 0.55 is a placeholder carried over from the mock. It is an assumption until the scorecard has enough closed trades to test it, and the terminal says so on screen
6. **Horizon.** How many bars ahead Kronos predicts and how long positions are held. These should match, and currently nothing forces them to
7. **Exit rule.** Time-based, signal-reversal, or both. Milestone 1 needs something, even if it is only "exit after N bars"

### What to do with a disagreement

If Claude Code finds that something in this spec cannot be built as written, the correct response is to stop and say which part and why, not to improvise a workaround. A spec that quietly drifts during implementation is how the event log stops matching the terminal, and that mismatch is the one failure this design cannot tolerate.
