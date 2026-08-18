# Geldscheißer

AI crypto swing-trading broker for Binance. See [PLAN.md](PLAN.md) for the full
roadmap and [CHECKLIST.md](CHECKLIST.md) for implementation progress.

The project is split by deployment target — four modules, four machines:

| Directory | Package | Runs on | Purpose |
|---|---|---|---|
| [common/](common/) | `gs-common` | everywhere | Config, DB schema, Binance clients, alerting |
| [collector/](collector/) | `gs-collector` | **VPS** | 24/7 market-data collection into a rolling TimescaleDB window |
| [archive/](archive/) | `gs-archive` | **Raspberry Pi** | Pulls the collector's data on a fixed interval into an unbounded local copy |
| [training/](training/) | `gs-training` | **Mac** | Phase 1: features, labels, model training, backtests (reads the archive) |
| [execution/](execution/) | `gs-execution` | **same VPS as collector** | Phase 2/3: inference, signals, auto-trading (reads the collector directly) |

**Current state: Phase 0** — the collection and archive stacks are built and verified:

| Service | Module | What it does |
|---|---|---|
| `klines` | collector | Closed candles (1m/5m/15m/1h) from websocket streams |
| `orderbook` | collector | Local book per symbol (snapshot + diff sync); 10s feature rollups + 5min top-500 snapshots |
| `aggtrades` | collector | Per-minute taker buy/sell flow |
| `futures` | collector | Funding rates (with full backfill) + open interest from USDT perps |
| `monitor` | collector | Heartbeat watchdog + daily data-quality report → Discord |
| `backfill` | collector | One-shot kline history loader (data.binance.vision dumps + REST top-up) |
| `sync` | archive | Pulls new rows from the collector into an unbounded local archive (default weekly) |

## Why collection and archive are on different machines

The order book collector is the one component where a gap is unrecoverable — candle history
can always be re-downloaded, but order book data only exists if you record it live. That makes
it worth running on whatever connection is actually reliable, which is a cheap VPS, not home
internet/power. The archive's job is much less time-sensitive: a periodic pull tolerates being
offline for a while, since the collector's 90-day retention gives roughly 13 weeks of buffer
to catch up from if a sync is missed.

## Deploy the collector (VPS)

Requires Docker + Compose v2.24+ on the VPS. Two ways to run the commands below:
directly on the VPS after cloning the repo there, or — recommended, see next
section — from your Mac over a Docker context, which never requires manually
logging into the VPS for routine deploys.

```sh
git clone <this-repo> && cd geldscheisser/collector
cp .env.example .env               # set GS_DISCORD_WEBHOOK_URL for alerts
docker compose up -d --build       # TimescaleDB (90-day window) + all collectors, 24/7
docker compose run --rm backfill   # kline history (resumable, re-run any time)
```

Check on it:

```sh
docker compose logs -f orderbook
docker compose run --rm klines quality    # ad-hoc data-quality report
```

> **Why the collectors must stay up:** candle history can always be re-downloaded,
> but order book data only exists if you record it live. Gaps are unrecoverable.
> The monitor alerts on stale collectors — take those alerts seriously.

> **Postgres's port is loopback-only by default.** `GS_BIND_ADDR` (in `.env`,
> defaults to `127.0.0.1`) controls what the exposed `5433` binds to on the
> VPS. It's not reachable from anywhere else until you deliberately set it —
> e.g. to this VPS's Tailscale/WireGuard IP once the archive needs to pull
> from here. Never set it to `0.0.0.0` or a public IP.

### Deploy from the Mac via SSH (recommended)

A Docker context runs your local `docker compose` commands against the VPS's
Docker daemon over an SSH tunnel — the build context is sent over SSH and
built remotely, so nothing needs to be manually copied to or run on the
server itself. No Docker port is ever exposed on the network; it rides the
same SSH connection you'd use to log in directly.

Set up once:

```sh
ssh-copy-id deploy@<vps-ip>                                   # SSH key auth
ssh deploy@<vps-ip> 'sudo usermod -aG docker $USER'            # run docker without sudo, then re-login once
docker context create vps --docker "host=ssh://deploy@<vps-ip>"
docker context ls                                              # confirm "vps" is listed
```

Deploy or redeploy, any time, from the `collector/` directory on your Mac:

```sh
docker --context vps compose up -d --build
docker --context vps compose run --rm backfill
docker --context vps compose logs -f orderbook
```

Keep `collector/.env` (Discord webhook, later API-adjacent secrets) on your
Mac and out of git — it's picked up locally and shipped to the VPS as part
of each `compose up`, the same as it would be if you ran these commands on
the server directly.

## Deploy the archive (Raspberry Pi)

Put the VPS and the Pi on the same private network (Tailscale or WireGuard) first —
the archive should never reach the collector's Postgres over the open internet.

```sh
cd archive
cp .env.example .env               # set GS_SOURCE_DATABASE_URL to the collector's private address
docker compose up -d --build       # local TimescaleDB (unbounded) + the weekly sync job
docker compose run --rm archive sync-once   # optional: trigger a sync immediately
```

Docker's data root (or at least the `archivedata` volume) should live on the Pi's SSD,
not the SD card — see [archive/README.md](archive/README.md) for details.

## Local development (Mac)

```sh
python3 -m venv .venv
.venv/bin/pip install -e common -e 'collector[dev]' -e 'archive[dev]'
.venv/bin/pytest collector/tests archive/tests   # unit tests
.venv/bin/gs orderbook                           # run a collector service against GS_DATABASE_URL
```

Configuration is env-driven (prefix `GS_`, optional `.env`) — see
[collector/.env.example](collector/.env.example), [archive/.env.example](archive/.env.example),
and [common/gs_common/config.py](common/gs_common/config.py) for all knobs.

Training (phase 1) reads the Pi's archive over the LAN and tracks experiments
in MLflow: `cd training && docker compose up -d` → http://localhost:5001.
