# gs-training — research layer (runs on the Mac)

Phase 1 of [the plan](../PLAN.md): feature pipeline, triple-barrier labeling,
LightGBM baselines, walk-forward backtesting. Not built yet — starts once the
Pi has accumulated a few weeks of order book rollups (kline-only research can
start right after backfill).

Data access: reads the Pi's TimescaleDB directly over the LAN
(`GS_DATABASE_URL=postgresql://gs:gs@<pi-host>:5433/geldscheisser`).
Heavy experiments should snapshot query results to local Parquet first so
repeated runs don't hammer the Pi.

Experiment tracking: `docker compose up -d` → MLflow at http://localhost:5001.
Trained models are registered here and shipped to the execution VPS.

Planned layout:

```
gs_training/
├── features.py   # shared feature library — the SAME code the VPS uses live
├── labels.py     # triple-barrier labeling
├── datasets.py   # Pi Postgres -> local parquet snapshots
├── train.py      # LightGBM + walk-forward CV, logs to MLflow
└── backtest.py   # event-driven simulation, fees/slippage
```

The feature library must stay importable by `gs-execution` — one code path for
training and live inference is what prevents train/serve skew.
