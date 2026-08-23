# Implementation Checklist

Progress tracker for [PLAN.md](PLAN.md). Update as things land — the gates
between phases are the important part, don't skip them.

## Phase 0 — Data collection

### Software (built & verified 2026-07)
- [x] Monorepo split by deployment target: `common/` / `collector/` / `archive/` / `training/` / `execution/`
- [x] Settings via `GS_*` env / `.env` (symbols, intervals, cadences, retention)
- [x] TimescaleDB schema: hypertables, compression policies, optional retention (`GS_RETENTION_DAYS`)
- [x] Kline collector (1m/5m/15m/1h websocket, closed candles only)
- [x] Kline backfill (data.binance.vision monthly dumps + REST top-up, resumable)
- [x] Order book collector (snapshot+diff sync, gap detection, auto-resync)
- [x] Order book rollups every 10s (spread, depth, imbalance at 10/50/100/200 bps)
- [x] Order book snapshots every 5min (top 500 levels/side)
- [x] aggTrade collector (per-minute taker buy/sell flow)
- [x] Futures collector (funding rates incl. backfill, open interest)
- [x] Heartbeats + monitor watchdog + daily data-quality report
- [x] Discord webhook alerting (Matrix deferred to phase 2)
- [x] Docker Compose stack (TimescaleDB + collectors + monitor)
- [x] Unit tests (book sync rules, rollup math, parsers) — 13 passing
- [x] Live smoke test against Binance (streams + backfill verified)
- [x] Archive sync job: keyset-paginated pull per table, idempotent (`ON CONFLICT DO NOTHING`)
- [x] Archive Docker Compose stack (TimescaleDB, unbounded + sync service)
- [x] Archive unit tests (pagination/resume/tie-safety, SQL generation) — passing
- [x] Collector network isolation via Tailscale sidecar (2026-08-19): every service shares
      the `tailscale` container's netns (`network_mode: "service:tailscale"`); no port
      published on the host at all — see `collector/TAILSCALE.md`. Supersedes the earlier
      `GS_BIND_ADDR` loopback-binding approach.
- [x] Fixed quality-report false positive (2026-08-16): kline/aggtrade coverage now accounts
      for the always-in-progress current period — was flagging the same ~20 series every day
- [x] Fixed archive sync first-run bug (2026-08-23), found by the first real Pi deploy:
      `sync_table` built its SELECT once, from the watermark state at entry. Starting from an
      empty table that meant the no-WHERE statement (one placeholder, the limit) got reused for
      page 2 onwards once a cursor existed — asyncpg rejected it outright
      (`InterfaceError: the server expects 1 argument ... 4 were passed`), so the *first* sync of
      any table could never get past its first page. Now both statement variants are built up
      front and chosen per page. The suite missed it because the test fake ignored the SQL and
      read the cursor from `*args`; it now asserts placeholder count matches arg count, which
      makes 4 existing tests fail against the old code.

### Deployment — collector (VPS)
- [x] VPS provisioned: Docker + Compose v2.24+
- [x] Discord webhook created and set in `collector/.env`
- [x] `docker compose up -d --build` — all services healthy
- [x] Daily quality report arriving (confirmed 2026-08-16)
- [x] `GS_RETENTION_DAYS=90` confirmed (2026-08-19: `timescaledb_information.jobs` shows
      `drop_after: 90 days` active on all 4 hypertables)
- [x] `TS_AUTHKEY` set in `collector/.env`; redeployed with the Tailscale-sidecar compose
      changes; `tailscale serve --bg --tcp=5432 tcp://localhost:5432` run once (2026-08-19)
- [x] Confirmed reachable at `gs-collector:5432` from another tailnet peer (2026-08-19: raw
      TCP connect succeeds, `tailscale serve status` shows tailnet-only, not funnel)
- [x] Confirmed unreachable on the public IP (2026-08-19: both old port 5433 and 5432 time
      out against the VPS's public IP; only SSH/22 responds)
- [ ] Kline backfill completed (2021 → now, all symbols/intervals)
- [ ] Test alert received (stop a collector, watchdog fires within ~3 min)
- [ ] Coverage ≥99% sustained for one full week (post quality-report fix)
- [ ] Collectors auto-recover after VPS reboot (`restart: unless-stopped` verified)

### Deployment — archive (Raspberry Pi 4 4GB, 1TB SATA SSD) — deployed 2026-08-23
- [x] Pi provisioned: Debian 13 (trixie) arm64, Docker CE 29.7.2 + Compose v5.5.0 from
      Docker's official apt repo (`trixie stable`), SSD is the boot/root device
- [x] Docker data root on the SSD — automatic: the Pi boots from the SSD, so
      `/var/lib/docker` is on `/dev/sda2` (961G, 905G free after the first sync). No SD card involved.
- [x] Pi and collector VPS joined on the same tailnet. Tailscale runs on the Pi *host*
      (not a sidecar as on the VPS) — the archive only dials out, so it needs no netns of
      its own. Docker's embedded resolver forwards to the host's MagicDNS, so `gs-collector`
      resolves from inside the containers on a plain bridge network — verified.
- [x] `GS_SOURCE_DATABASE_URL=postgresql://gs:gs@gs-collector:5432/geldscheisser` (tailnet name,
      not a public IP)
- [x] `docker --context gs-archive compose up -d --build` — both services up, timescaledb healthy
- [x] First sync completed (2026-08-23 01:59): 2,315,704 rows in ~2h57m —
      klines=359,793 · book_rollups=1,533,183 · book_snapshots=51,560 ·
      agg_trades_1m=257,533 · funding_rates=61,885 · open_interest=51,750.
      Peak memory stayed ≥3.0GB available; disk grew ~5GB.
- [x] Postgres reachable from the Mac over the tailnet
      (`psql postgresql://gs:gs@gs-archive:5433/geldscheisser`) — real query verified, not just a TCP accept
- [x] **Not** reachable on the LAN: the container port is bound to `127.0.0.1` and fronted by
      `tailscale serve --bg --tcp=5433 tcp://localhost:5433`; connecting to the Pi's LAN IP:5433
      is refused. Same posture as the collector.
- [x] Confirmed idempotent: back-to-back `sync-once` runs added 27,278 then **110** rows —
      the second run picked up only what the live collector produced in between, so the
      keyset watermark resumes rather than re-copying.
- [x] Unbounded retention confirmed: `timescaledb_information.jobs` shows compression at
      14 days on all 4 hypertables and **no** `drop_after` policy on any of them
- [x] Auto-recovery verified: `systemctl restart docker` → both containers back up
      (`restart: unless-stopped`), and the archive container still resolves `gs-collector`
- [x] Boot-order race closed: added a `docker.service` drop-in with
      `After=/Wants=tailscaled.service`. Without it Docker can start before tailscaled writes
      `/etc/resolv.conf`, containers capture a pre-Tailscale resolver, and the failed sync then
      sleeps a full `GS_ARCHIVE_SYNC_INTERVAL_S` (7 days) before retrying.
- [ ] Full reboot test still pending — the Pi is also a desktop (lightdm/wayvnc/cups) with
      users logged in, so this wants a convenient moment. The drop-in above is the fix being tested.
- [ ] Archive survives a missed sync window (stop it a few days, verify catch-up on restart)
- [ ] Backup story for the SSD decided (even rsync of weekly pg_dump is fine)
- [ ] Consider retry-with-backoff on a failed sync: `sync_loop` currently sleeps the full
      interval after *any* failure, so one transient error costs a week of freshness. The
      90-day collector retention means it isn't data loss, and the Discord alert fires — but
      it needs a human. A few short retries before falling back to the weekly cadence would
      make it self-healing.

**Gate → phase 1:** klines backfilled (research can start now) · ≥4–8 weeks
of continuous order book rollups in the archive before book features join the model.

## Phase 1 — Training (Mac, `training/`)

- [ ] MLflow up (`cd training && docker compose up -d`)
- [ ] Dataset export: archive Postgres → local Parquet snapshots (`datasets.py`)
- [ ] Feature library (`features.py`) — multi-timeframe price/trend/volume features
- [ ] Order book features from rollups (spread/imbalance/depth trends)
- [ ] Cross-market features (BTC regime, funding, open interest)
- [ ] Triple-barrier labeling (`labels.py`), volatility-scaled targets, pluggable
- [ ] Baselines: buy-and-hold, SMA cross, random-entry-same-exit
- [ ] LightGBM classifier + calibrated probabilities
- [ ] Walk-forward validation with purge + embargo (no random splits!)
- [ ] Event-driven backtester: fees, slippage, barrier exits (`backtest.py`)
- [ ] Stress tests: 2× fees, 1-candle entry delay, drop best 5 trades
- [ ] Experiment log in MLflow; untouched final holdout period reserved

**Gate → phase 2:** walk-forward backtest beats baselines net of fees across
≥2 market regimes, and you understand *why* it works.

## Phase 2 — Live signals (same VPS as `collector`, `execution/`)

- [ ] Collector already deployed and healthy on this VPS well before go-live
      *(order book rollups only accumulate from when it started — no shortcut)*
- [ ] Execution deployed alongside it, reading the same `timescaledb` container directly
- [ ] Model delivery: MLflow registry → VPS, hot-reload on new version
- [ ] Inference service: candle-close loop → features → calibrated P(win)
- [ ] Signal engine: threshold + hysteresis, cooldowns, virtual positions (exit signals too)
- [ ] Every signal (emitted *and* suppressed) persisted — forward-test dataset
- [ ] Notifier: Discord webhook; Matrix via matrix-nio if wanted
- [ ] Weekly automated forward-test report (paper P&L vs backtest expectation)

**Gate → phase 3:** 2–3 months of forward-test signals whose live performance
roughly matches the backtest.

## Phase 3 — Automated trading (VPS)

- [ ] Binance API key: trade-only, **withdrawals disabled**, IP-restricted
- [ ] Order executor: limit entries near mid, timeout → re-quote/abandon
- [ ] OCO take-profit + stop-loss placed server-side immediately on fill
- [ ] Time-based exit at T_max (14d barrier)
- [ ] State reconciliation: user-data stream + periodic REST snapshot (exchange = source of truth)
- [ ] Risk manager: per-position cap, max concurrent positions, total exposure cap
- [ ] Daily/weekly loss limit → automatic halt + alert
- [ ] Kill switch (CLI + chat command): cancel all, optionally flatten
- [ ] Pre-order sanity checks (price band, balance, symbol status, lot/notional filters)
- [ ] Full order/trade audit log
- [ ] Spot testnet run: order plumbing verified end-to-end
- [ ] Live with tiny capital (€200–500), fills/slippage match assumptions
- [ ] Scale-up rules defined (only while live tracks the forward test)
- [ ] Performance circuit breaker: pause when live diverges from expectation
- [ ] Scheduled retraining cadence decided (e.g. monthly walk-forward)

## Ongoing operations

- [ ] Collector VPS uptime reviewed via daily quality reports
- [ ] Archive sync freshness checked periodically (last successful sync not too far behind)
- [ ] Pi disk usage trend checked monthly (compression working, SSD headroom)
- [ ] Retrain → redeploy loop exercised at least once before real money
- [ ] Post-incident rule: any unrecoverable data gap gets a cause + fix noted here
