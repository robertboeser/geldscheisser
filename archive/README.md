# gs-archive — permanent record (runs on the Raspberry Pi)

Pulls new rows from the collector VPS at a fixed interval (default weekly)
into a local, unbounded-retention TimescaleDB. This is the archive of record
that [training/](../training/) reads from — the collector itself only ever
holds a 90-day rolling window.

Why split it this way: the collector needs a reliable, low-latency
connection (an order book gap is unrecoverable), which home internet isn't
always great at — so it runs on a VPS instead. The archive's job is much less
time-sensitive: it just needs to catch up eventually, so a Pi behind a home
connection (or power cycle) is a fine place for it. The collector's 90-day
retention gives roughly 13 weeks of buffer if a sync is missed.

## How syncing works

Each table is paginated by its primary key (reordered time-column-first) —
the archive's own current max row for a table *is* the resume position, so
there's no separate cursor to persist, and re-running (including after a
crash mid-sync) is safe: `sync_table` in [gs_archive/sync.py](gs_archive/sync.py)
just picks up where the local data already ends. Inserts use
`ON CONFLICT DO NOTHING` — the archive is a straight copy, never a live merge.

## Deploy

**As built:** Raspberry Pi 4 Model B (4GB), Debian 13 (trixie) arm64, booting
from a 1TB SATA SSD — root *is* the SSD, so Docker's data root
(`/var/lib/docker`) lands there with nothing to configure and the SD card
isn't in the picture at all. Host name on the tailnet: `gs-archive`.

Requires this Pi to be a peer on the same tailnet as the collector VPS. The
collector side runs Tailscale as a sidecar container that serves its Postgres
at `gs-collector:5432` to other tailnet peers, with no port published on the
VPS host at all — see [../collector/TAILSCALE.md](../collector/TAILSCALE.md).

Here it's the other way round: **Tailscale runs on the Pi host, not as a
sidecar.** The archive only ever dials *out* to the collector, so it has no
reason to own a network namespace, and the containers stay on a normal bridge
network. Docker's embedded resolver forwards to the host's Tailscale DNS, so
`gs-collector` resolves via MagicDNS from inside the containers with no
`dns:`/`extra_hosts` overrides needed.

One-time setup on the Pi (Docker from the official apt repo — Debian's own
`docker.io` package is too old for Compose v2):

```sh
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/debian trixie stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"     # so the Mac's Docker context needs no sudo
```

Then, from the Mac (a Docker context means routine deploys never require
logging into the Pi — same pattern as the collector VPS):

```sh
docker context create gs-archive --docker "host=ssh://robo@gs-archive"

cd archive
cp .env.example .env               # set GS_SOURCE_DATABASE_URL
docker --context gs-archive compose up -d --build   # local TimescaleDB + the sync service
```

`sync` runs one pass immediately on start and then every
`GS_ARCHIVE_SYNC_INTERVAL_S`, so the first sync begins on `up` — no need to
trigger it. `docker compose run --rm archive sync-once` is for an out-of-band
catch-up (or for confirming idempotency: a second run should add 0 rows).

Finally, expose the archive's Postgres to the tailnet so the training Mac can
read it. The published port is bound to `127.0.0.1` — never the LAN — and
`tailscale serve` fronts it, the same mechanism the collector uses. Once, on
the Pi:

```sh
sudo tailscale serve --bg --tcp=5433 tcp://localhost:5433
```

> **Why `serve` rather than binding the container port to the Pi's `100.x`
> address:** it keeps the compose file free of a tailnet IP, and it avoids a
> boot-ordering race — after a reboot Docker may start containers before
> `tailscale0` exists, and a published port bound to a not-yet-configured
> address fails to bind.

### Boot ordering: Docker after Tailscale

Docker captures the host's `/etc/resolv.conf` when a container starts and hands
it to its embedded resolver. If Docker starts before `tailscaled` has written
that file, the containers get a pre-Tailscale resolver and can't resolve
`gs-collector` at all — and because `sync_loop` sleeps the full
`GS_ARCHIVE_SYNC_INTERVAL_S` after a failed run, a boot-order race costs a
week of freshness rather than a retry. `docker.service` ships with only
`After=network-online.target`, so this is closed explicitly:

```sh
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/10-after-tailscaled.conf <<'EOF'
[Unit]
After=tailscaled.service
Wants=tailscaled.service
EOF
sudo systemctl daemon-reload
```

### Batch size on a 4GB Pi

`GS_ARCHIVE_BATCH_SIZE` is set to **2000** here, well below the 20000 default.
`book_snapshots` rows carry top-500-levels-per-side JSONB — measured at ~22KB
each — so 20000-row pages would be ~440MB of raw JSON per page and several GB
once parsed into Python objects, on a box with 3.7GB usable. The archive is
not time-sensitive, so smaller pages cost nothing that matters.

## Check on it

```sh
docker --context gs-archive compose logs -f archive

# From anywhere on the tailnet (e.g. the training Mac):
psql postgresql://gs:gs@gs-archive:5433/geldscheisser -c \
  "SELECT symbol, count(*) FROM klines GROUP BY 1"
```
