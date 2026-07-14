# Geldscheißer

AI crypto swing-trading broker for Binance. See [PLAN.md](PLAN.md) for the full roadmap.

The project is split by deployment target — three machines, three jobs:

| Directory | Package | Runs on | Purpose |
|---|---|---|---|
| [common/](common/) | `gs-common` | everywhere | Config, DB schema, Binance clients, alerting |
| [collector/](collector/) | `gs-collector` | **Raspberry Pi** (and later the VPS) | 24/7 market-data collection into TimescaleDB |
| [training/](training/) | `gs-training` | **Mac** | Phase 1: features, labels, model training, backtests |
| [execution/](execution/) | `gs-execution` | **VPS** | Phase 2/3: inference, signals, auto-trading |

**Current state: Phase 0** — the collection stack is built and verified:

| Service | What it records |
|---|---|
| `klines` | Closed candles (1m/5m/15m/1h) from websocket streams |
| `orderbook` | Local book per symbol (snapshot + diff sync); 10s feature rollups + 5min top-500 snapshots |
| `aggtrades` | Per-minute taker buy/sell flow |
| `futures` | Funding rates (with full backfill) + open interest from USDT perps |
| `monitor` | Heartbeat watchdog + daily data-quality report → Discord |
| `backfill` | One-shot kline history loader (data.binance.vision dumps + REST top-up) |

## Deploy collection on the Raspberry Pi

Requires Docker + Compose v2.24+ on the Pi (arm64 images all exist), with
Docker's data root (or at least the `tsdata` volume) on the SSD, not the SD card.

```sh
git clone <this-repo> && cd geldscheisser/collector
cp .env.example .env               # set GS_DISCORD_WEBHOOK_URL for alerts
docker compose up -d --build       # TimescaleDB + all collectors, 24/7
docker compose run --rm backfill   # kline history (resumable, re-run any time)
```

Check on it:

```sh
docker compose logs -f orderbook
docker compose run --rm klines quality    # ad-hoc data-quality report
psql postgresql://gs:gs@<pi-host>:5433/geldscheisser -c \
  "SELECT symbol, count(*) FROM book_rollups GROUP BY 1"
```

> **Why the collectors must stay up:** candle history can always be re-downloaded,
> but order book data only exists if you record it live. Gaps are unrecoverable.
> The monitor alerts on stale collectors — take those alerts seriously.

The same compose file later deploys to the VPS with `GS_RETENTION_DAYS=90` —
the execution box feeds itself a rolling data window instead of depending on
the Pi (see [execution/README.md](execution/README.md)).

## Local development (Mac)

```sh
python3 -m venv .venv
.venv/bin/pip install -e common -e 'collector[dev]'
.venv/bin/pytest collector/tests      # unit tests (book sync, rollups, parsers)
.venv/bin/gs orderbook                # run any service against GS_DATABASE_URL
```

Configuration is env-driven (prefix `GS_`, optional `.env`) — see
[collector/.env.example](collector/.env.example) and
[common/gs_common/config.py](common/gs_common/config.py) for all knobs.

Training (phase 1) reads the Pi's database over the LAN and tracks experiments
in MLflow: `cd training && docker compose up -d` → http://localhost:5001.
