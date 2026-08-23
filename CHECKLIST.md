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

### Deployment — archive (Raspberry Pi 4 4GB, SanDisk Ultra II 500GB SSD) — redeployed 2026-08-23

> **Incident, resolved by hardware replacement.** The first attempt on this node ran on a
> counterfeit fake-capacity USB stick reporting 1TB (Chipsbank CBM2199 controller,
> placeholder `VendorCo`/`ProductCode` strings, a suspiciously round 2,048,000,000 sectors).
> It silently discarded writes past its real size; the page cache hid that for hours, then
> the first reboot produced 61 EFSCORRUPTED errors, `ext4_validate_block_bitmap` failures at
> a regular 128-block-group stride, Postgres `PANIC: replication checkpoint has wrong magic
> 4294967295`, and `exec format error` on a binary that had been running fine. The tell in
> hindsight: the first sync took ~2h57m to move ~1GB. Nothing irreplaceable was lost — the
> archive is fully reconstructible from the collector. **Lesson kept as the acceptance test
> below.**

- [x] Pi provisioned: Debian 13 (trixie) arm64, Docker CE 29.7.2 + Compose v5.5.0 from
      Docker's official apt repo (`trixie stable`)
- [x] **Storage vetted before use** — the check that the counterfeit drive would have failed:
      (a) real SMART passthrough `smartctl -d sat -a` → `SanDisk Ultra II 500GB`, serial
      present, **PASSED**, 0 reallocated sectors, 0 program/erase failures, 0 CRC errors
      (21,706 power-on hours — used, but no wear indicators); (b) sector count 976,773,168 =
      500,107,862,016 bytes, the drive's true size, not a round number; (c) durability test —
      5GB written with direct I/O (exceeds the 3.7GB of RAM), caches dropped, re-read twice:
      identical checksums both times. Bridge is a real `JMicron JM20329 SATA Bridge`.
- [x] Docker data root on the SSD — automatic: the Pi boots from it, so `/var/lib/docker` is
      on `/dev/sda2` (459G, 433G free)
- [x] Pi and collector VPS on the same tailnet (new node → new IP `100.94.83.38`). Tailscale
      runs on the Pi *host*, not a sidecar — the archive only dials out. Docker's embedded
      resolver forwards to the host MagicDNS, so a container resolves `gs-collector` on a plain
      bridge network and reaches its Postgres — both verified before deploying.
- [x] Stale SSH host keys for the reinstalled box replaced (key read from the box itself, not
      keyscanned), so the `gs-archive` Docker context connects over the tailnet name
- [x] `GS_SOURCE_DATABASE_URL=postgresql://gs:gs@gs-collector:5432/geldscheisser` (tailnet name,
      not a public IP)
- [x] `docker --context gs-archive compose up -d --build` — both services up, timescaledb healthy
- [x] `tailscale serve --bg --tcp=5433 tcp://localhost:5433` re-applied on the new node
      (serve config lives in tailscaled state, so a rebuild loses it)
- [x] Boot-order race closed again: `docker.service` drop-in with `After=/Wants=tailscaled.service`
- [x] **Healthcheck race fixed** (`archive/docker-compose.yml`): `pg_isready` now runs with
      `-h 127.0.0.1`. On a fresh volume the postgres entrypoint runs initdb against a temp
      server with `listen_addresses=''`, so the default unix-socket check reported healthy
      while the real server was still starting — `archive` then started, failed to connect and
      died. Self-healing via `restart:`, but it dirtied the logs on every fresh volume; seen
      on both deployments before being fixed.
- [x] **Persistent journal enabled.** Raspberry Pi OS ships
      `40-rpi-volatile-storage.conf` (RAM-only journal, to spare SD cards), which meant a
      connectivity drop-out during this deploy left no evidence at all. Overridden with a
      `99-persistent.conf` drop-in — it must sort *after* the rpi one to win — capped at 200M.
- [ ] First full sync completed on the vetted SSD; row counts match the collector
- [ ] Confirmed idempotent: a second `sync-once` adds only newly-collected rows
- [ ] Unbounded retention confirmed (compression policies present, no `drop_after`)
- [ ] Postgres reachable from the Mac over the tailnet, and **not** on the LAN
- [ ] Reboot test — the check that caught the counterfeit drive. Re-run on this hardware;
      also the first real test of the MagicDNS-after-boot behaviour the drop-in guards against.
- [ ] **Move the node to Ethernet.** It is on `wlan0` (`eth0` down). A Wi-Fi flap during this
      deploy dropped the tailnet and the LAN simultaneously for ~a minute mid-sync (the box
      itself stayed up; the sync resumed idempotently). Wi-Fi is a poor fit for a node whose
      job is pulling GBs.
- [ ] **Consider a USB3 bridge.** The JM20329 is USB2-only: measured 37.7 MB/s, which is why
      the first full sync takes hours. Fine for a daily delta; an ASM1153E/JMS578 enclosure
      would be a strict upgrade.
- [ ] Archive survives a missed sync window (stop it a few days, verify catch-up on restart)
- [ ] Backup story for the SSD decided (even rsync of weekly pg_dump is fine)
- [ ] Consider retry-with-backoff on a failed sync: `sync_loop` sleeps the full interval after
      *any* failure, so one transient error costs a day of freshness (and Wi-Fi flaps make
      transient errors likelier here). The 90-day collector retention means it is not data
      loss, and the Discord alert fires. A few short retries before falling back to the daily
      cadence would make it self-healing.

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
