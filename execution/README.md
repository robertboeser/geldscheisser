# gs-execution — live layer (runs on the VPS)

Phases 2 and 3 of [the plan](../PLAN.md): inference on live data, buy/sell
signals to Matrix/Discord, and later the order executor + risk manager.
Not built yet — gated on a trained model that survives a walk-forward backtest.

## Why the VPS runs its own collectors

The model needs live candles and order book features to find entry points.
Rather than depending on the home network / the Pi being reachable, the VPS
runs the same `gs-collector` stack with `GS_RETENTION_DAYS=90`: a rolling
window that is plenty for feature lookback, small enough for a cheap VPS disk.
`docker compose run --rm backfill` fills the kline window instantly on first
deploy; order book rollups accumulate from the moment it starts (independent
of the Pi's archive).

Model delivery: trained models come from the Mac's MLflow registry (pull on
deploy, hot-reload on new version).

Planned layout:

```
gs_execution/
├── inference.py   # candle-close loop: features -> model -> probabilities
├── signals.py     # thresholds, hysteresis, cooldowns, virtual positions
├── notifier.py    # Discord webhook first, Matrix (matrix-nio) second
├── trader.py      # phase 3: order executor (limit entries, OCO exits)
└── risk.py        # phase 3: position/exposure/loss limits, kill switch
```

Deployment: `../collector/docker-compose.yml` with the VPS .env, plus (later)
an `execution/docker-compose.yml` for inference/signal/trader services.
API keys: trade-only, withdrawals disabled, IP-restricted to the VPS.
