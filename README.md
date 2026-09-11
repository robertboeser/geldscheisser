# Geldscheißer

AI crypto swing-trading broker for Binance. See [PLAN.md](PLAN.md) for the full
roadmap, [CHECKLIST.md](CHECKLIST.md) for implementation progress, and
[CLAUDE.md](CLAUDE.md) for the invariants that are easy to violate.

The project is split by deployment target — four modules, four machines:

| Directory | Package | Runs on | Purpose |
|---|---|---|---|
| [common/](common/) | `gs-common` | everywhere | Config, DB schema, Binance clients, alerting |
| [collector/](collector/) | `gs-collector` | **VPS** | 24/7 market-data collection into a rolling TimescaleDB window |
| [archive/](archive/) | `gs-archive` | **Raspberry Pi** | Pulls the collector's data on a fixed interval into an unbounded local copy |
| [training/](training/) | `gs-training` | **Mac** | Phase 1: labeling, walk-forward, backtester (built); features + model (next) |
| [execution/](execution/) | `gs-execution` | **same VPS as collector** | Phase 2/3: inference, signals, auto-trading (reads the collector directly) |

**Current state: Phase 0** — the collection and archive stacks are built and verified:

| Service | Module | What it does |
|---|---|---|
| `klines` | collector | Closed candles (1m/5m/15m/1h) from websocket streams |
| `orderbook` | collector | Local book per symbol (snapshot + diff sync); 10s feature rollups + 5min top-500 snapshots |
| `aggtrades` | collector | Per-minute taker buy/sell flow |
| `futures` | collector | Funding rates (with full backfill) + open interest from USDT perps |
| `monitor` | collector | Heartbeat watchdog + daily data-quality report → Discord |
| `backfill` | collector | One-shot kline history loader (data.binance.vision dumps + REST top-up); gap-filling, so it also runs against the archive |
| `sync` | archive | Pulls new rows from the collector into an unbounded local archive (default daily) |

## Why collection and archive are on different machines

The order book collector is the one component where a gap is unrecoverable — candle history
can always be re-downloaded, but order book data only exists if you record it live. That makes
it worth running on whatever connection is actually reliable, which is a cheap VPS, not home
internet/power. The archive's job is much less time-sensitive: a periodic pull tolerates being
offline for a while, since the collector's 90-day retention is the real safety margin — at the
archive's daily cadence that's ~90 consecutive missed runs before anything is actually lost.

## Deploy the collector (VPS)

Requires Docker + Compose v2.24+ on the VPS. Two ways to run the commands below:
directly on the VPS after cloning the repo there, or — recommended, see next
section — from your Mac over a Docker context, which never requires manually
logging into the VPS for routine deploys.

```sh
git clone <this-repo> && cd geldscheisser/collector
cp .env.example .env               # set GS_DISCORD_WEBHOOK_URL and TS_AUTHKEY
docker compose up -d --build       # TimescaleDB (90-day window) + all collectors, 24/7
docker compose run --rm backfill   # kline history for the 90-day window (re-run any time)
docker compose exec tailscale tailscale serve --bg --tcp=5432 tcp://localhost:5432   # once
```

Check on it:

```sh
docker compose logs -f orderbook
docker compose run --rm klines quality    # ad-hoc data-quality report
```

> **Backfilling the collector only fills its 90-day window.** That is all the
> live features need. Multi-year history for training goes into the *archive*
> instead — see [Backfill history into the archive](archive/README.md#backfill-history-into-the-archive).
> Pointing the backfill at the collector and expecting the archive to inherit
> the history does not work: retention drops the old chunks, and the archive's
> sync is forward-only.

> **Why the collectors must stay up:** candle history can always be re-downloaded,
> but order book data only exists if you record it live. Gaps are unrecoverable.
> The monitor alerts on stale collectors — take those alerts seriously.

> **No port is published on the host at all — not even loopback.** Every
> service shares a `tailscale` sidecar container's network namespace
> (see [collector/TAILSCALE.md](collector/TAILSCALE.md)); Postgres is reachable
> only at `gs-collector:5432` over the tailnet, once `tailscale serve` (above)
> has run. The only thing ever reachable from outside this host is SSH.
> For ad-hoc commands, use `docker compose exec <any-service> gs quality`
> rather than running `gs` directly on the bare host — there's nothing there
> to connect to anymore.

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

The collector VPS already serves its Postgres over the tailnet via a
Tailscale sidecar container (see [collector/TAILSCALE.md](collector/TAILSCALE.md))
— no port published on the VPS host at all. The Pi (`gs-archive`) is a peer on
that same tailnet, running Tailscale on the **host** rather than as a sidecar:
the archive only dials *out* to the collector, so it has no reason to own a
network namespace. Docker's embedded resolver forwards to the host's MagicDNS,
so `gs-collector` resolves from inside the containers on a plain bridge network.

```sh
cd archive
cp .env.example .env                                 # set GS_SOURCE_DATABASE_URL to gs-collector:5432
docker --context gs-archive compose up -d --build    # local TimescaleDB (unbounded) + the daily sync job
```

`sync` runs one pass immediately on start, then every
`GS_ARCHIVE_SYNC_INTERVAL_S` — the first sync begins on `up`, nothing to
trigger. Then once, on the Pi, to let the training Mac read the archive over
the tailnet (the container's port is bound to `127.0.0.1`, never the LAN):

```sh
sudo tailscale serve --bg --tcp=5433 tcp://localhost:5433
```

The Pi boots from its SSD, so Docker's data root is already on the SSD with
nothing to configure — the SD card isn't in the picture. See
[archive/README.md](archive/README.md) for the full as-built notes, including
why `GS_ARCHIVE_BATCH_SIZE` is lowered on a 4GB Pi.

## Local development (Mac)

```sh
python3 -m venv .venv
.venv/bin/pip install -e common -e 'collector[dev]' -e 'archive[dev]' -e 'training[dev]'
.venv/bin/pytest common/tests collector/tests archive/tests training/tests   # unit tests
.venv/bin/gs orderbook                           # run a collector service against GS_DATABASE_URL
```

Configuration is env-driven (prefix `GS_`, optional `.env`) — see
[collector/.env.example](collector/.env.example), [archive/.env.example](archive/.env.example),
and [common/gs_common/config.py](common/gs_common/config.py) for all knobs.

Training (phase 1) reads the Pi's archive over the tailnet
(`postgresql://gs:gs@gs-archive:5433/geldscheisser`) and tracks experiments
in MLflow: `docker --context colima compose -f training/docker-compose.yml up -d`
→ http://localhost:5001. Pass `--context` — this repo's active Docker context is a
*remote* machine, so a bare `docker compose up -d` starts MLflow on the Pi.
