# gs-execution — live layer (runs on the VPS)

Phases 2 and 3 of [the plan](../PLAN.md): inference on live data, buy/sell
signals to Matrix/Discord, and later the order executor + risk manager.
Not built yet — gated on a trained model that survives a walk-forward backtest.

## Why execution shares the collector's machine

The model needs live candles and order book features to find entry points.
`gs-collector` already runs 24/7 on a VPS with `GS_RETENTION_DAYS=90` — a
rolling window that's plenty for feature lookback — so execution deploys onto
that *same* VPS and reads that *same* database directly. No separate collector
stack, no second retention window to reason about; it's one machine, one
Postgres, two services reading/writing it.

Deploy the collector there well before phase 2 goes live: `docker compose run
--rm backfill` fills the kline window instantly, but order book rollups only
accumulate from the moment the collector starts, and the model needs some
history of those before it's useful.

Model delivery: trained models come from the Mac's MLflow registry (pull on
deploy, hot-reload on new version). Training itself reads from the Pi's
[archive](../archive/), not from this VPS.

Planned layout:

```
gs_execution/
├── inference.py   # candle-close loop: features -> model -> probabilities
├── signals.py     # thresholds, hysteresis, cooldowns, virtual positions
├── notifier.py    # Discord webhook first, Matrix (matrix-nio) second
├── trader.py      # phase 3: order executor (limit entries, OCO exits)
└── risk.py        # phase 3: position/exposure/loss limits, kill switch
```

Deployment: `../collector/docker-compose.yml` on the VPS (as already deployed
for collection), plus (later) an `execution/docker-compose.yml` for the
inference/signal/trader services, pointed at the same `timescaledb` container.
API keys: trade-only, withdrawals disabled, IP-restricted to the VPS.
