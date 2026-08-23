# Implementation Checklist

Progress tracker for [PLAN.md](PLAN.md). Update as things land — the gates
between phases are the important part, don't skip them.

## Phase 0 — Data collection

### Software (built & verified 2026-07)
- [x] Monorepo split by deployment target: `common/` / `collector/` / `archive/` / `training/` / `execution/`
- [x] Settings via `GS_*` env / `.env` (symbols, intervals, cadences, retention)
- [x] TimescaleDB schema: hypertables, compression policies, optional retention (`GS_RETENTION_DAYS`)
- [x] Kline collector (1m/5m/15m/1h websocket, closed candles only)
- [x] Kline backfill (data.binance.vision monthly dumps + REST top-up, gap-filling)
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
- [x] **Fixed backfill being unable to fill history under live data** (2026-08-23), found
      when scoping phase 1: `backfill_pair` resumed from `MAX(open_time)`, so once the live
      collector was running the resume point was always "now" — `complete_months(today, today)`
      is empty and the REST cursor started at the present. A re-run therefore fetched **nothing
      and logged success**. Everything below the newest row was unreachable, which on a database
      whose old chunks had been dropped by retention meant all history. Compounding it, the
      archive's sync is forward-only (`WHERE (open_time, symbol, interval) > watermark`), so even
      a correctly backfilled collector could never hand that history to the Pi. Now every complete
      month in the window is checked against its expected candle count and re-fetched if short —
      backward fill, mid-history gaps, and cheap no-op re-runs all fall out of the same rule.
      Related fixes in the same pass:
      · counts reported are rows *inserted* (from the INSERT command tag), not rows downloaded —
        a backfill that overstates its work is the failure mode this module exists to prevent
      · `GS_BACKFILL_START_OVERRIDES="1m:...,5m:..."` so fine intervals need not reach 2021
      · staging via `COPY` + `INSERT .. SELECT .. ON CONFLICT` instead of `executemany`
        (~3,000 rows/s vs the ~850 measured on the first Pi sync)
      · `create_pool(manage_retention=False)` for batch jobs — `gs backfill` pointed at the
        archive with the gs-common default of 90 would have attached a retention policy to the
        unbounded database and dropped the history it had just written
      · `GS_RETENTION_DAYS: "0"` pinned in `archive/docker-compose.yml` so the archive's
        defining property is stated at the deploy site, not inherited
- [x] **Fixed backfill stranding rows from the archive sync** (2026-08-23) — caused by the
      first full run above, caught by verifying the *sync watermark* rather than just row
      counts. The sync resumes from a single `(open_time, symbol, interval)` tuple and only
      walks forward, so it is lossless only while the archive is a **prefix** of the collector
      in that order. Backfill walks pairs sequentially; over a 49-minute run each pair's REST
      phase ended at a different "now", leaving a 44-minute staircase (BTCUSDT 1m ended 12:34,
      LTCUSDT 1m 13:18). The global maximum jumped to the newest step, so every collector row
      below it but above its own pair's step became unreachable **forever** — 286 rows, silently.
      Nothing in the row counts showed it: the totals only grew, and a naive count comparison
      *hides* it because the archive legitimately holds years the collector does not.
      Fix: one run-wide cap, read once at start (existing `max(open_time)`, or a single "now"
      on an empty target), applied to both the dump and REST paths. History is backfill's job;
      the recent tail is the sync's. The invariant is now stated in `gs_archive/sync.py` so the
      next writer does not rediscover it the hard way.
      The first cap attempt was still wrong, and verifying flatness rather than trusting the
      fix is what caught it: the cap bounded *open* time, but "is this candle closed?" was
      still evaluated against the wall clock at the moment each pair was fetched. A 15m candle
      opening 13:15 closes 13:30, so a run spanning 13:30 excluded it for the pairs it reached
      first and included it for the rest — the same staircase, one candle wide, 2 rows stranded.
      Eligibility is now a pure function of the run-start instant (`last_closed_open_ms`), so
      every pair of an interval lands on the same last candle no matter how long the run takes.

- [x] **Fixed the archive sync silently dropping late-arriving rows** (2026-08-23) — a
      pre-existing bug, not caused by the backfill; found while verifying the backfill's own
      watermark behaviour, and proved by running a real `sync-once` and watching it *not*
      recover rows it had skipped. `sync_table` resumed at exactly the destination's max
      keyset tuple, which assumes rows are *inserted* in keyset order. False for `klines`,
      which mixes intervals in one table: a 15m candle is written when it **closes**, fifteen
      minutes after its own `open_time`, by which point 1m candles have pushed the watermark
      past it. Its tuple sorts below the cursor, so the forward-only walk skipped it for good —
      roughly one candle per symbol per coarse interval, every sync, forever.
      Fix: `GS_ARCHIVE_SYNC_LAG_S` (default 2h, must exceed the largest kline interval) rewinds
      each sync a little behind its watermark. Overlap is free — inserts are already
      `ON CONFLICT DO NOTHING`. Verified: a lagged `sync-once` recovered all 20 rows the
      un-lagged one had abandoned.
      Two consequences worth knowing:
      · the returned count is rows *transferred*, so it now includes the re-read overlap — a
        quiet sync no longer reports exactly 0. Exact insert counts would mean staging pages
        the way `gs backfill` does; `executemany` cannot return one.
      · **Deployed to the Pi 2026-08-23 14:00** — verified in the running container
        (`sync_table` carries the `lag` parameter, effective `archive_sync_lag_s=7200.0`),
        and the sync-on-start moved 11,205 rows.
- [x] Reconciled archive against collector across all six tables (2026-08-23): five match
      exactly; `klines` shows the archive as a strict **superset** — zero rows the collector
      has that the archive lacks, and 457 rows the archive has that the *collector* is missing
      (websocket-era gaps the backfill repaired from the dumps/REST). No accumulated loss from
      the sync bug: the archive was only deployed the same day, and the backfill's current-month
      pass re-covered exactly the window in which losses could have built up.

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
- [x] Kline backfill of the collector's own 90-day window (all it needs for live
      features — multi-year history lives in the archive instead, see below)
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
- [x] First full sync completed on the vetted SSD (2026-08-23 11:57:32 UTC): 2,186,235 rows in
      this run, ~1h13m. Row counts match the collector —
      klines=370,563 · book_rollups=1,570,523 · book_snapshots=52,770 · agg_trades_1m=263,533 ·
      funding_rates=61,895 · open_interest=52,951. Memory held ≥3.19GB available throughout;
      disk grew to 8.4G of 459G. Measured rate: ~850 rows/s on narrow tables, but only
      ~32 rows/s on book_snapshots (22KB JSONB rows over a 37.7 MB/s USB2 link) — that one
      table is most of the wall-clock.
- [x] Confirmed idempotent: back-to-back `sync-once` runs added 3,751 rows then **exactly 0**.
      The keyset watermark resumes rather than re-copying.
- [x] Unbounded retention confirmed: compression at 14 days on all 4 hypertables and **no**
      `drop_after` policy on any of them.
- [x] Postgres reachable from the Mac over the tailnet
      (`psql postgresql://gs:gs@gs-archive:5433/geldscheisser` — real query, not just a TCP
      accept), and **refused** on the Pi's LAN address. Verified again after the reboot.
- [x] **Reboot test PASSED** (2026-08-23 14:00) — the check that caught the counterfeit drive:
      filesystem `clean` with no error count and no I/O errors in dmesg (vs 61 EFSCORRUPTED
      before); Postgres recovered cleanly to "ready to accept connections" with no PANIC; both
      containers back up via `restart: unless-stopped`; every row count ≥ the pre-reboot
      baseline, so nothing was lost. The ordering drop-in did its job — tailscaled 14:00:42,
      docker 14:00:52 — and **MagicDNS resolution from inside the container after boot is now
      verified for the first time** (`gs-collector` → 100.72.199.65), which is precisely what
      the drop-in exists to guarantee. `tailscale serve` survived (it lives in tailscaled
      state), and the sync-on-start caught up the 156-row delta 3s after the DB was ready.
      The persistent journal also retained the pre-reboot boot, so it works.
- [ ] **Ethernet not yet working.** A cable was attached 2026-08-23 but the link carries no
      traffic: carrier up and 100Mb/s negotiated, yet `rx_packets=0` against 66 DHCP DISCOVERs
      sent, NetworkManager cycling on `ip-config-unavailable`, and eth0 now DOWN. Zero inbound
      frames (not even broadcasts) plus 100Mb/s on a gigabit-capable Pi 4 points at a bad cable
      (only 2 of 4 pairs usable) or a dead switch port. Still on `wlan0`, where a Wi-Fi flap
      already dropped the tailnet and LAN together for ~a minute mid-sync. Retest with:
      `cat /sys/class/net/eth0/statistics/rx_packets` (must go non-zero),
      `ip -brief addr show eth0` (needs a v4 address), `ethtool eth0 | grep Speed` (want 1000Mb/s).
      Once it gets a lease NetworkManager prefers it automatically (metric 100 vs 600) — no config
      needed, and Wi-Fi stays as failover.
- [ ] **Consider a USB3 bridge.** The JM20329 is USB2-only: measured 37.7 MB/s, which is why
      the first full sync takes hours. Fine for a daily delta; an ASM1153E/JMS578 enclosure
      would be a strict upgrade.
- [x] **Multi-year kline history loaded into the archive** (2026-08-23): 8,633,620 candles —
      1h/15m from 2021-01-01, 1m/5m from 2025-09-01, all 10 symbols. Verified by contiguity
      per series (not by row counts, which hide exactly this class of fault): every series is
      gap-free apart from seven windows that are real Binance outages (2021-02-11, 03-06,
      04-20, 04-25, 08-13, 09-29, 2023-03-24 — identical on every symbol and both intervals,
      and absent from the public dumps too). All four intervals end on a flat maximum.
- [x] **Post-redeploy reconciliation (2026-08-23)**: zero rows the collector holds that the
      archive lacks, across all six tables; archive retention still absent; all intervals flat;
      no unexpected holes. The archive is a strict superset — it also carries 457 klines the
      *collector* is missing, live-collection gaps the backfill repaired from the dumps/REST.
- [ ] Archive survives a missed sync window (stop it a few days, verify catch-up on restart)
- [ ] Backup story for the SSD decided (even rsync of weekly pg_dump is fine)
- [ ] Consider retry-with-backoff on a failed sync: `sync_loop` sleeps the full interval after
      *any* failure, so one transient error costs a day of freshness (and Wi-Fi flaps make
      transient errors likelier here). The 90-day collector retention means it is not data
      loss, and the Discord alert fires. A few short retries before falling back to the daily
      cadence would make it self-healing.

**Gate → phase 1:** klines backfilled *into the archive* (research can start once
that lands — the collector's own window is not the training set) · ≥4–8 weeks of
continuous order book rollups in the archive before book features join the model.

## Phase 1 — Training (Mac, `training/`)

- [ ] MLflow up (`cd training && docker compose up -d`)
- [x] Dataset export: archive Postgres → local Parquet snapshots (`datasets.py`),
      plus `load_panel` for an inner-joined multi-symbol bar grid. Read-only by
      construction: connects with raw asyncpg rather than `gs_common.create_pool`,
      which would apply schema DDL to the archive of record.
- [ ] Feature library (`features.py`) — multi-timeframe price/trend/volume features
- [ ] Order book features from rollups (spread/imbalance/depth trends)
- [ ] Cross-market features (BTC regime, funding, open interest)
- [x] Triple-barrier labeling (`labels.py`), volatility-scaled targets, CUSUM event
      sampling. Volatility is scaled to the holding period (`sigma·sqrt(max_bars)`) —
      feeding a per-bar sigma into a 336-bar horizon put the barriers ~18x too close,
      so nothing ever reached the vertical barrier and the labels silently described
      scalping rather than swing trading.
- [x] Baselines: buy-and-hold, SMA cross, random-entry-same-exit (`baselines.py`)
- [ ] LightGBM classifier + calibrated probabilities
- [x] Walk-forward validation with purge + embargo (`splits.py`), plus
      `assert_no_leakage` (called per fold in every experiment) and
      `average_uniqueness` for overlap-aware sample weights
- [x] Event-driven backtester: fees, slippage, barrier exits, capital and
      concurrency limits (`backtest.py`). Exits go through the *same*
      `scan_barriers` the labeller uses, so the backtest cannot drift from the
      training target.
- [x] Stress tests: 2× fees, 1-candle entry delay, drop best 5 trades (`metrics.py`)
- [x] Harness unit tests — 48 passing, covering the pessimism choices (same-bar
      double touch = loss, gaps through the stop fill at the open, no fill on the
      decision bar) and the leakage guarantees
- [x] **Fixed a backtest accounting bug** (2026-08-23), caught by cross-checking two
      numbers that must agree: the run reported `total_return=+39.5%` while realised
      P&L was `+145`. A position whose barrier resolved inside its own entry bar was
      appended to `open_trades` *after* that bar's close-out step, so it never closed —
      it held capital for the rest of the run and leaked into equity as an unrealised
      mark that never became P&L. Fixed by settling same-bar resolutions inline, and
      `run_backtest` now raises if any position is still open at the final bar.
      Correcting it flipped `sma_cross` from +39.5% to −16.9%.

**First real finding (2026-08-23), before any model exists:** over 49,440 aligned 1h
bars, `sma_cross 50/200` returns −16.9% (PF 0.96) and sits at the *bottom* of the
random-entry band (−14.8% … +48.5% across 5 seeds), while equal-weight buy & hold
returns +1084.5%. The 63-point spread between identical-logic random seeds is the
number to keep in mind: any single backtest result inside that band is luck, which is
what ~1,456 effectively independent samples buys.
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
