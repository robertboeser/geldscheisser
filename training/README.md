# gs-training — research layer (runs on the Mac)

Phase 1 of [the plan](../PLAN.md). The **evaluation harness is built**:
labeling, purged walk-forward splits, an event-driven backtester, metrics and
baselines. The feature library and the model are not — deliberately, since a
model you cannot honestly evaluate is worse than no model.

Kline history is [backfilled into the
archive](../archive/README.md#backfill-history-into-the-archive) (2021 → now for
1h/15m), so kline research can start now. Order book features join once the
archive has accumulated a few weeks of rollups.

Data access: reads the Pi's **archive** (not the live collector) over the LAN
(`GS_DATABASE_URL=postgresql://gs:gs@<pi-host>:5433/geldscheisser`) — the
archive holds unbounded history, while the collector itself only keeps a
90-day rolling window. This also means training data lags live by up to one
archive sync interval (1 day by default, `GS_ARCHIVE_SYNC_INTERVAL_S`) — fine
for a monthly-ish retrain cadence, worth knowing if you ever want very recent
data. Heavy experiments should snapshot query results to local Parquet first so
repeated runs don't hammer the Pi (it is a USB2-attached SSD).

## Experiment tracking

```sh
docker --context colima compose -f training/docker-compose.yml up -d   # MLflow on :5001
```

> **Always pass `--context` here.** This repo's day-to-day Docker context is a
> *remote* machine (`gs-archive`, and `vps` for the collector), so a bare
> `docker compose up -d` in this directory starts MLflow **on the Raspberry Pi** —
> a 4GB box whose whole job is to be a lean archive. It looks like it worked:
> the container runs, the logs are clean, and only `localhost:5001` being dead
> gives it away. `docker context ls` shows which one is active.

MLflow is published on **loopback only** (`127.0.0.1:5001`). It has no
authentication, and the experiment list plus the model registry are not things
to hand to the LAN; reach it from elsewhere over the tailnet or an SSH tunnel.
The image also enables Host-header validation by default, hence `--allowed-hosts`
in the compose file — without it every request is rejected with no useful error.

Trained models are registered here and shipped to the execution VPS.

## Layout

Built (the evaluation harness — deliberately before any model, because
"the backtest is the product"):

```
gs_training/
├── datasets.py   # archive Postgres -> local parquet; aligned multi-symbol panel
├── labels.py     # triple-barrier labeling, CUSUM event sampling, barrier scan
├── splits.py     # purged + embargoed walk-forward, uniqueness weights
├── backtest.py   # event-driven simulation: fees, slippage, capital, concurrency
├── metrics.py    # profit factor, Sharpe/Sortino, drawdown + the stress tests
└── baselines.py  # buy & hold, SMA cross, random-entry-same-exit control
```

Not built yet:

```
├── features.py   # shared feature library — the SAME code the VPS uses live
└── train.py      # LightGBM + walk-forward CV, calibration, logs to MLflow
```

## Using it

```sh
GS_DATABASE_URL=postgresql://gs:gs@gs-archive:5433/geldscheisser \
  python -m gs_training.datasets --interval 1h --cache-dir data/parquet
```

Then label, split and backtest — see the worked example in
[the harness section below](#what-the-harness-already-tells-you).

## Three invariants the harness enforces

1. **One barrier definition.** `BarrierSpec.barrier_prices` and `scan_barriers`
   are used by *both* the labeller and the backtester. If they diverged, the
   backtest would score a strategy the model was never trained on, and it would
   look like a surprising result rather than a bug.
2. **Nothing is labelled from data it could not have seen.** A signal at the
   close of bar `i` fills at the open of `i+1`; barrier widths use volatility
   known at `i`; a bar touching both barriers is a loss, because OHLC does not
   record the intrabar path and assuming the good one invents money.
3. **Equity equals starting cash plus realised P&L.** Asserted at the end of
   every run. This is not decoration — it caught a real bug where a position
   resolving inside its own entry bar was never booked, holding capital for the
   rest of the run and leaking into equity as a mark that never became P&L. The
   headline return said +39.5% while realised P&L was +145.

## What the harness already tells you

Running the baselines over 49,440 aligned 1h bars (2021-01 → 2026-08, 10 symbols,
+1.0σ/−0.5σ horizon-scaled barriers ≈ +14%/−7%, 14-day vertical, 0.1% taker fees
and 5bps slippage each way):

| strategy | trades | hit | return | profit factor | max DD |
|---|---|---|---|---|---|
| **buy & hold (equal weight)** | — | — | **+1084.5%** | — | — |
| sma_cross 50/200 | 829 | 26.1% | −16.9% | 0.96 | 44.7% |
| random entries (5 seeds) | ~1055 | ~25% | −14.8% … +48.5% | 0.97–1.10 | 24–51% |

Read that carefully, because it is the whole reason this exists:

- SMA cross **loses money**, and sits at the bottom of the random-entry band. It
  has no edge; what looked like a strategy is the exit rule plus noise.
- Every barrier strategy is annihilated by simply holding. In a sample where the
  average symbol did +1084%, beating buy & hold — not beating zero — is the bar.
- The random-entry spread is **63 points wide** across five seeds with identical
  logic. Any single backtest result inside that band is indistinguishable from
  luck, which is what ~1,456 effectively independent samples buys you.

## The first model

```sh
python -m gs_training.train --interval 1h --cache-dir data/parquet --splits 5
```

LightGBM on 41 causal features, uniqueness-weighted, isotonic-calibrated on a
held-out tail of each training block, thresholded on **expected value** rather
than probability, and scored by the backtester against a same-count random-entry
baseline in the same window.

| fold | train | test | AUC | signals | return | baseline | PF |
|---|---|---|---|---|---|---|---|
| 0 | 9,962 | 13,033 | 0.523 | 9,054 | −58.1% | −71.0% | 0.61 |
| 1 | 20,386 | 13,033 | 0.530 | 0 | — | — | — |
| 2 | 30,810 | 13,033 | 0.504 | 0 | — | — | — |
| 3 | 41,292 | 13,033 | 0.574 | 416 | +12.3% | +6.0% | 1.40 |
| 4 | 51,716 | 13,036 | 0.498 | 2,239 | −18.1% | −50.7% | 0.75 |

Mean AUC **0.526** — barely distinguishable from a coin flip. Mean return
−21.3% against a baseline of −38.6%, beating it in 3 of 3 traded folds, with a
mean profit factor of 0.92. **It loses money**, and it is nowhere near buy &
hold's +1084%. That is the expected result for a first model on price-only
features, and the point of building the harness first is that it says so plainly
instead of flattering it.

Two details worth reading rather than skipping:

- **Folds 1 and 2 emitted no signal at all**, and that is the EV rule working,
  not a failure. With a +14%/−7% barrier and 0.3% round-trip costs, EV turns
  positive only above p≈0.35, and the base rate of a win is ~28%. A calibrated
  model that never gets confident correctly refuses to trade. Abstentions are
  reported separately and excluded from the return averages — scoring them as
  "0% return" would pull a losing strategy toward break-even.
- **Fold 0 traded 9,054 times and lost 58%.** An early-sample model with almost
  no training data behind it fires constantly. Signal count per fold is the first
  thing to look at when a fold looks strange.

## Sample budget

78,837 labelled events sounds like plenty and is not. A 14-day horizon on 1h bars
overlaps ~336-fold, so `average_uniqueness` puts the effective independent count
at **~1,456** — matching the analytic estimate of 49,440 bars ÷ 336 × 10 symbols.
Those weights belong in `sample_weight` when training; treating 78,837 correlated
rows as 78,837 observations is how a model overfits while its metrics look fine.

The feature library must stay importable by `gs-execution` — one code path for
training and live inference is what prevents train/serve skew.
