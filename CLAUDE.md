# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI crypto swing-trading broker for Binance (positions held days to ~2 weeks, spot,
long-only). [PLAN.md](PLAN.md) is the roadmap and [CHECKLIST.md](CHECKLIST.md) tracks
progress — **read CHECKLIST.md first in a new session**; it carries the incident log and
the reasoning behind decisions that are not visible in the code.

Current state: phase 0 (collection + archive) deployed and running; phase 1 (research)
has its evaluation harness and a first LightGBM baseline. Phases 2–3 (live signals,
auto-trading) are unbuilt.

## Commands

```sh
python3 -m venv .venv
.venv/bin/pip install -e common -e 'collector[dev]' -e 'archive[dev]' -e 'training[dev]'

.venv/bin/pytest common/tests collector/tests archive/tests training/tests   # all tests
.venv/bin/pytest training/tests/test_labels.py -q                            # one file
.venv/bin/pytest training/tests -q -k "causal"                               # one pattern
```

Research pipeline (Mac):

```sh
GS_DATABASE_URL=postgresql://gs:gs@gs-archive:5433/geldscheisser \
  .venv/bin/python -m gs_training.datasets --interval 1h --cache-dir data/parquet
.venv/bin/python -m gs_training.train --interval 1h --cache-dir data/parquet --splits 5
docker --context colima compose -f training/docker-compose.yml up -d   # MLflow :5001
```

Deploys are per-module Docker Compose stacks driven from the Mac over remote contexts:
`docker --context vps compose ...` (collector), `docker --context gs-archive compose ...`
(archive). See each module's README for the full sequence.

## Deployment topology — four modules, four machines

Each directory is its own installable package so a machine installs only what it runs.
`common/` (`gs-common`: config, DB schema, Binance clients, alerting) is installed
everywhere and is what guarantees the collector and archive produce identically shaped data.

| Module | Machine | Role |
|---|---|---|
| `collector/` | VPS | 24/7 collection into a **90-day rolling** TimescaleDB |
| `archive/` | Raspberry Pi 4 | **Unbounded** permanent record; pulls from the collector |
| `training/` | Mac | Research: features, labels, walk-forward, backtest, MLflow |
| `execution/` | same VPS as collector | Phases 2–3, unbuilt |

Data flows: collector → archive (daily pull over Tailscale) → training (Parquet
snapshots) → execution (models via MLflow registry). Execution reads the collector's DB
directly since they share a machine. Postgres is never exposed publicly on either box —
only over the tailnet (`gs-collector:5432`, `gs-archive:5433`).

## Invariants that are easy to violate

Each of these was a real bug. They do not announce themselves — they produce plausible
output while being wrong.

**Always pass `--context` to docker.** The active context is a *remote* machine, so a
bare `docker compose up -d` deploys to the Pi or the VPS. It looks like it worked.

**Backfill targets the archive, not the collector.** The collector's 90-day retention
drops history before it can be synced, and the archive's sync is forward-only, so
history written to the collector can never reach the Pi. `gs backfill` fills gaps by
month rather than appending, and opens its pool with `manage_retention=False` — batch
jobs must never redefine the target DB's retention (the gs-common default of 90 would
silently prune the unbounded archive).

**The archive must stay a prefix of the collector** in `(time, ...)` keyset order. The
sync resumes from a single watermark and only walks forward, so any writer that raises
the maximum unevenly strands rows permanently. Two defences: backfill takes a run-wide
cap and writes only below the watermark, and the sync rewinds `GS_ARCHIVE_SYNC_LAG_S`
(2h) before its watermark to catch late arrivals — a 15m candle is written when it
*closes*, by which time 1m candles have pushed the watermark past it. Consequence: sync
row counts include the re-read overlap, so a quiet sync no longer reports 0.

**One barrier definition.** `BarrierSpec.barrier_prices` and `scan_barriers` in
[labels.py](training/gs_training/labels.py) are used by both the labeller and the
backtester. If they diverge, the backtest scores a strategy the model was never trained
on, and it reads as a surprising result rather than a bug.

**Features must be causal and prefix-stable.** Value at `i` depends only on bars `<= i`,
and computing on `close[:k]` must reproduce the full-series values for the first `k`
rows — that is exactly what live inference does. Both are asserted in
[test_features.py](training/tests/test_features.py). A violation yields a model that
backtests beautifully and loses money on the VPS.

**Volatility scales to the holding period.** Barriers use `sigma * sqrt(max_bars)`. A
per-bar sigma on a 336-bar horizon puts them ~18x too close, nothing reaches the
vertical barrier, and the labels silently describe scalping instead of swing trading.

**Equity == starting cash + realised P&L.** `run_backtest` asserts nothing is left open
at the final bar. This caught a position that resolved inside its own entry bar and was
never booked, holding capital all run and leaking into equity as a mark that never
became P&L — the run reported +39.5% while realised P&L was +145.

**Row counts are not sample size.** A 14-day horizon on 1h bars overlaps ~336-fold:
~79,000 labelled events are ~1,456 effectively independent observations. Use
`average_uniqueness` as `sample_weight`, compute it *per symbol*, and never use random
splits — `walk_forward` purges training samples whose label window reaches into the test
block and embargoes the shadow after it.

**Report abstention, not zero.** A walk-forward fold where expected value never cleared
the threshold has abstained; averaging it in as "0% return" flatters a losing strategy.

## Conventions

- Config is env-driven with the `GS_` prefix via pydantic-settings, all in
  [common/gs_common/config.py](common/gs_common/config.py); `.env` files are per-module
  and out of git.
- Timestamps are UTC everywhere. Use `datetime.now(timezone.utc)`, not `date.today()` —
  a local date names the wrong month around boundaries.
- The training layer is **read-only** against the archive: it connects with raw asyncpg
  rather than `gs_common.create_pool`, which would apply schema DDL to the record of truth.
- Tests encode the pessimistic choices deliberately (a bar touching both barriers is a
  loss; gaps through the stop fill at the open; no fill on the decision bar). If one of
  those assertions flips, the backtest has started inventing money.
- Trading decisions use expected value (`p·target − (1−p)·stop − costs`), never `p > 0.5`.
