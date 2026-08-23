# Geldscheißer — AI Crypto Swing-Trading Broker for Binance

## Goal

Build a system that:

1. **Collects** market data from Binance (candlesticks: 1m, 5m, 15m, 1h + order book).
2. **Trains** a model on that data to identify profitable buy/sell points for **swing trades** (positions held for days, up to ~2 weeks — no intraday scalping).
3. **Signals** opportunities in real time via Matrix or Discord.
4. **(Phase 2)** Places orders automatically via the Binance API based on the model's output.

## Guiding principles

- **Candles can be backfilled, order books cannot.** Binance lets you download years of historical klines for free (REST API + [Binance public data dumps](https://data.binance.vision)). Order book snapshots/deltas are *not* available historically — you only get them by recording the live stream (or buying them). **Consequence: start the order book recorder on day one**, even before anything else exists. Every day it isn't running is training data lost forever.
- **The backtest is the product.** A model is worthless until it survives a realistic backtest (fees, slippage, no lookahead). Build the evaluation harness before investing heavily in modeling.
- **Signal-only before auto-trading.** The notification phase doubles as a long forward test with zero capital at risk.
- **Everything is timestamped, append-only, and reproducible.** Raw data is never mutated; features and labels are derived artifacts that can be regenerated.

---

## Architecture overview

```
┌──────────────────────────────────────────────────────────────┐
│                        Data Layer                            │
│  collector-klines   collector-orderbook   collector-extras   │
│        │                    │                   │            │
│        └──────────┬─────────┴───────────────────┘            │
│                   ▼                                          │
│        Storage: TimescaleDB (hot) + Parquet archive (cold)   │
└───────────────────┬──────────────────────────────────────────┘
                    ▼
┌──────────────────────────────────────────────────────────────┐
│                     Research Layer                           │
│  feature pipeline → labeling → model training → backtester   │
│  (offline, batch, reproducible experiments)                  │
└───────────────────┬──────────────────────────────────────────┘
                    ▼
┌──────────────────────────────────────────────────────────────┐
│                      Live Layer                              │
│  inference service → signal engine → notifier (Matrix/       │
│  Discord)                → [Phase 2] order executor + risk   │
│                            manager (Binance API)             │
└──────────────────────────────────────────────────────────────┘
```

Each box is an independent process/container so the collectors keep running while you iterate on models.

## Deployment topology — four modules, four machines

| Machine | Module | Role | What runs there |
|---|---|---|---|
| **VPS** (cheap, reliable connection) | `collector` | **Collection** — live, rolling window | TimescaleDB (`GS_RETENTION_DAYS=90`) + all collectors + monitor, 24/7. Later shares this box with `execution`. |
| **Raspberry Pi 4** (SATA SSD over USB3, at home) | `archive` | **Archive** — permanent record | TimescaleDB (unbounded) + a sync job that pulls new rows from the collector VPS on a fixed interval (default daily) |
| **Mac** | `training` | **Training** — research | Feature pipeline, labeling, LightGBM training, backtesting; MLflow for experiments/model registry |
| **VPS** (same box as `collector`) | `execution` | **Execution** — live | Reads the collector's own DB directly (no separate stack); inference, signal engine, notifier; phase 3 adds the order executor + risk manager |

Why collection moved off the Pi: the order book collector is the one component where a gap
is unrecoverable, so it belongs on whichever connection is actually reliable — a VPS, not
home internet/power. The Pi's job becomes much less time-sensitive: a periodic batch pull
tolerates the Pi being offline for a while, since the collector's 90-day window is the real
safety margin — at the archive's daily cadence that's ~90 consecutive missed runs before
anything is actually lost.

Data flows:

- **collector → archive:** the Pi's `archive` service pulls new rows from the collector VPS
  at a fixed interval (default 1 day) over a private network (Tailscale/WireGuard — never
  expose the collector's Postgres publicly). Sync is keyset-paginated and idempotent, so
  missed or partial runs just catch up next time; nothing is ever lost as long as a sync
  happens within the 90-day window.
- **archive → training:** the Mac reads the Pi's (unbounded) TimescaleDB over the LAN and
  snapshots query results to local Parquet for fast repeated training runs. Training data is
  therefore up to one sync interval (1 day by default) behind live — fine for a monthly-ish
  retraining cadence. Multi-year candle history is loaded straight into the archive rather
  than inherited from the collector: retention drops the collector's old chunks, and the
  sync only ever moves forward from the archive's newest row.
- **training → execution:** trained model artifacts ship via the MLflow registry (pull on
  deploy, hot-reload on new version).
- **execution ⇄ collector:** since they share one machine and one database, execution simply
  reads the collector's live data directly — no separate stack, no second retention window to
  reason about.
- Collector and archive run identical schema/code (`gs-common`), which is what guarantees
  train/live data has the same shape regardless of which copy produced it.

## Tech stack (proposal)

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Ecosystem for both Binance connectivity and ML |
| Binance connectivity | `python-binance` or `ccxt` + raw websockets (`websockets`/`aiohttp`) | Official streams for klines, depth diffs, trades |
| Hot storage | TimescaleDB (Postgres extension) | Time-series queries, compression, continuous aggregates |
| Cold storage | Parquet files (partitioned by symbol/day) via `polars`/`pyarrow` | Cheap, fast for batch training reads |
| Dataframes | `polars` (fall back to `pandas` where needed) | Fast on large kline/orderbook datasets |
| Baseline models | LightGBM / XGBoost | Strong tabular baselines, fast iteration, interpretable |
| Later models | PyTorch (temporal CNN / transformer) — only if boosted trees plateau | Don't start with deep learning |
| Experiment tracking | MLflow (self-hosted, lightweight) | Compare runs, store models |
| Backtesting | Custom event-driven backtester (simple) — `vectorbt` for quick scans | Off-the-shelf tools rarely model your exact label/exit logic |
| Notifications | Matrix via `matrix-nio` **or** Discord webhook (webhook is ~10 lines, start there) | |
| Orchestration | Docker Compose per machine; `systemd` timers or `cron` for batch jobs | Pi: collection stack; Mac: MLflow; VPS: collection (short retention) + live services |
| Config/secrets | `.env` + `pydantic-settings`; API keys never in git | |

---

## Phase 0 — Foundation & data collection (start immediately)

**Deliverable: collectors running 24/7, data verifiably accumulating.**

### 0.1 Project scaffolding
- Monorepo with one directory per deployment target, plus a shared library:
  ```
  common/       # gs-common: config, db schema, binance clients, alerting (installed everywhere)
  collector/    # gs-collector: recorders + monitor + compose  → VPS
  archive/      # gs-archive: pulls from the collector on a fixed interval → Raspberry Pi
  training/     # gs-training: features, labels, training, backtest + MLflow compose → Mac
  execution/    # gs-execution: inference, signals, notifier, trader → same VPS as collector
  ```
  Each part is its own Python package (`pip install ./common ./collector ...`), so every
  machine installs only what it runs.
- Docker Compose per target (collector stack + TimescaleDB on the VPS; archive stack +
  TimescaleDB on the Pi; MLflow on the Mac).
- Structured logging + a heartbeat/health metric per collector (a dead collector must page you — a gap in order book data is unrecoverable).

### 0.2 Symbol universe
- Start with a fixed list of 5–15 liquid USDT pairs (e.g. BTC, ETH, SOL, BNB, XRP, …). Liquid pairs have tighter spreads and more meaningful order books.
- Keep the list in config; the schema must support adding symbols later.

### 0.3 Kline collector
- Live: subscribe to `kline_1m` websocket streams per symbol; persist only **closed** candles. Derive 5m/15m/1h from 1m (or subscribe to those streams too as a cross-check).
- Backfill: pull full history for all symbols/intervals from the REST API + monthly dumps from `data.binance.vision`. This gives you *years* of candle history immediately.
- Store: `(symbol, interval, open_time, open, high, low, close, volume, quote_volume, trades, taker_buy_volume)`.

### 0.4 Order book collector (the time-critical one)
- Maintain a local book per symbol: REST depth snapshot + `depth@100ms` diff stream, with the standard Binance sync algorithm (buffer diffs, apply from `lastUpdateId`).
- **Don't store every diff long-term** (it's enormous). Persist:
  - A full snapshot (top ~500 levels) every 1–5 minutes.
  - Precomputed rollups every 10s: best bid/ask, spread, mid, depth-weighted imbalance at 0.1%/0.5%/1%/2% from mid, cumulative bid/ask volume in those bands.
- Raw diffs can additionally be dumped to compressed Parquet for a rolling window (e.g. 30 days) if you want the option to recompute rollups later.

### 0.5 Extras (cheap, likely useful)
- `aggTrade` stream rollups (buy/sell taker volume per minute — order-flow signal).
- Funding rates and open interest from Binance Futures for the same symbols (strong sentiment signal for multi-day horizons, and fully backfillable).

### 0.6 Data quality
- Daily job: check for kline gaps, orderbook sync losses, duplicate rows; report to the notification channel (this also exercises the notifier early).

**Exit criterion: ≥4–8 weeks of continuous order book rollups + backfilled multi-year klines.** Model work on klines can start immediately; orderbook features join once enough live data exists.

---

## Phase 1 — Research: features, labels, model, backtest

**Deliverable: a trained model with a backtest that beats buy-and-hold and simple baselines after fees, validated walk-forward.**

### 1.1 Labeling — define what "a buy point" is (the most important decision)
For a days-to-2-weeks horizon, use the **triple-barrier method** (López de Prado):
- At time *t*, look forward up to *T_max* (e.g. 14 days).
- Upper barrier: profit target (e.g. +X%, or volatility-scaled: k·ATR).
- Lower barrier: stop loss (e.g. −Y% or k·ATR).
- Label = which barrier is hit first (win / loss / timeout).
- Train a classifier for *P(win)*; a "buy point" is where *P(win)·target − P(loss)·stop − fees > threshold*.
- Sell points come for free: they're either the barriers themselves (exit rule) or a symmetric short-side label used as an exit signal for open longs (spot = long-only).

Alternatives to benchmark against: fixed-horizon return sign, trend-scanning labels. Keep labeling pluggable.

### 1.2 Features
- **Price/trend (from candles, multi-timeframe):** returns over multiple lookbacks, EMA/SMA distances and crosses, RSI, MACD, Bollinger position, ATR/realized volatility, candle-shape stats, distance from rolling highs/lows (breakout context), day-of-week/hour.
- **Volume/flow:** volume z-scores, taker buy/sell imbalance, quote-volume trends.
- **Order book (from the 10s rollups, aggregated to 5m/1h):** spread stats, depth imbalance at multiple bands, book-depth trends, snapshot-to-snapshot depletion.
- **Cross-market:** BTC returns/vol as regime features for altcoins, funding rate, open interest change.
- All features must be computable **live** from the same code path used in training (one shared feature library — this kills the #1 source of train/live skew).

### 1.3 Model progression
1. **Baselines first** (must be beaten): buy-and-hold, SMA cross, random entries with the same exit rule.
2. **LightGBM classifier** on tabular features → probability of hitting the profit barrier.
3. Calibrate probabilities (isotonic/Platt) — thresholding on calibrated probabilities is what turns a classifier into a trading rule.
4. Only if trees plateau: sequence models (temporal CNN/transformer on raw multi-timeframe candles + book rollups).

### 1.4 Validation — where most trading ML dies
- **Walk-forward / purged time-series splits only.** Never random splits. Purge + embargo samples whose label windows overlap the test period (labels look up to 14 days ahead → huge leakage risk).
- Watch for: lookahead in features, survivorship in symbol selection, and overlapping labels inflating sample counts.
- Metrics: precision at the chosen signal threshold, profit factor, Sharpe/Sortino, max drawdown, **net of fees** — not accuracy.

### 1.5 Backtester
- Event-driven simulation over the walk-forward test segments: signals → entries → barrier exits, with taker fees (0.1% spot, less with BNB), conservative slippage, and per-position sizing.
- Stress it: double the fees, delay entries by one candle, drop the best 5 trades — the edge must survive.

**Exit criterion: walk-forward backtest with positive expectancy after fees across ≥2 market regimes, and you understand *why* it works.**

---

## Phase 2 — Live signals & notifications

**Deliverable: real-time messages like "BUY signal: SOLUSDT @ 142.30, P(win)=0.67, target +6.2%, stop −3.1%, expires in 14d" with full logging for forward-test evaluation.**

Runs on the **collector VPS**, reading that same collector's data directly (see deployment
topology) — deploy the collector stack early so live order book features have history when
the first model goes live.

### 2.1 Inference service
- Loop (event-driven on candle close, e.g. every 5m/1h depending on the model's decision timeframe):
  1. Pull latest candles + orderbook rollups from TimescaleDB.
  2. Compute features via the **shared feature library**.
  3. Run the model, get calibrated probabilities per symbol.
- Model loaded from MLflow registry; hot-reload on new model version.

### 2.2 Signal engine
- Threshold + hysteresis (no signal spam when probability oscillates around the threshold).
- Cooldown per symbol; deduplication; "position awareness" even in signal-only mode (track virtual open positions so it also emits the matching SELL/exit signals).
- Every emitted and suppressed signal is persisted → this is your **forward-test dataset**.

### 2.3 Notifier
- Start with a **Discord webhook** (trivial: one HTTP POST). Add **Matrix** via `matrix-nio` if preferred — the notifier is an interface with two implementations.
- Message includes: symbol, side, price, probability, suggested target/stop, model version, and a chart snapshot link if you want to get fancy.
- Also route operational alerts here (collector died, data gap, model errors).

### 2.4 Forward-test evaluation
- Weekly automated report: how did the last weeks' signals perform vs. the backtest expectation? (paper P&L, hit rate, drawdown).
- **Gate for Phase 3: ≥2–3 months of forward-test signals whose live performance roughly matches the backtest.**

---

## Phase 3 — Automated trading

**Deliverable: unattended execution on Binance with strict risk controls.**

### 3.1 Execution ladder (in order, no skipping)
1. **Binance Spot Testnet** — validate order plumbing (API mechanics only; testnet liquidity is fake).
2. **Live, tiny capital** (e.g. €200–500) — validate real fills, slippage, and edge cases.
3. **Gradual scale-up** only while live results track the forward test.

### 3.2 Order executor
- On BUY signal: place limit order near mid (post-only where possible to pay maker fees); timeout → re-quote or abandon.
- On entry fill: immediately place an **OCO order** (take-profit + stop-loss) matching the label barriers — so exits are enforced server-side even if your box dies.
- Time-based exit: cancel/market-out at *T_max* (the 14-day barrier).
- Full order/trade audit log; reconcile local state against the account via user-data websocket stream + periodic REST snapshot.

### 3.3 Risk manager (vetoes the model — non-negotiable rules)
- Max % of capital per position (e.g. 5–10%), max concurrent positions, max total exposure.
- Daily/weekly loss limit → automatic trading halt + alert.
- Kill switch: one command (and one chat command in Matrix/Discord) to cancel all orders and optionally flatten.
- Sanity checks before every order: price within X% of last trade, sufficient balance, symbol trading status, min-notional/lot-size filters.
- API key permissions: **trade only, withdrawals disabled**, IP-restricted.

### 3.4 Operations
- Everything in Docker on a VPS/home server with auto-restart; state recovery on restart from the exchange (source of truth) not local memory.
- Monitoring: process health, websocket connectivity, order latency, position drift — all alerting into the same chat channel.

---

## Honest risk assessment (read before writing code)

- **Most retail ML trading systems don't beat fees.** Crypto spot round-trip costs ~0.15–0.2%; the model needs a real, persistent edge, not just in-sample accuracy. The walk-forward + forward-test gates exist to prove this before money is at risk.
- **Regime change:** a model trained in a bull market can fail abruptly. Plan for scheduled retraining (e.g. monthly walk-forward) and a performance circuit breaker that pauses trading when live results diverge from expectation.
- **Order book value at this horizon:** for multi-day holds, order book features mainly help *entry/exit timing*, less *direction*. Expect candles + flow + funding to carry most of the signal; don't be surprised if book features contribute modestly.
- **Overfitting is the default outcome.** Every experiment that touches test data burns it a little. Log all experiments (MLflow), keep a final untouched holdout period.
- Spot is long-only: "sell points" mean exiting longs, not shorting. Shorting would require margin/futures — explicitly out of scope for now.

## Milestones

| # | Milestone | Rough effort |
|---|---|---|
| M0 | Scaffolding, Compose stack, kline backfill done | 1–2 weekends |
| M1 | Orderbook + kline collectors stable 24/7 with alerting | 1–2 weekends (then it just runs) |
| M2 | Feature pipeline + triple-barrier labels + LightGBM baseline + walk-forward backtest | 3–6 weekends (iterative) |
| M3 | Backtest beats baselines net of fees (the hard gate — may take longer) | open-ended |
| M4 | Live inference + Discord/Matrix signals + forward-test logging | 1–2 weekends |
| M5 | 2–3 months forward test matching backtest | calendar time |
| M6 | Testnet executor + risk manager | 2–3 weekends |
| M7 | Live with small capital | calendar time |

**First concrete step:** M0/M1 — get the order book recorder running this week. All research depends on the data it collects, and that data cannot be bought back later.
