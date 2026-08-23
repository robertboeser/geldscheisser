# gs-archive — permanent record (runs on the Raspberry Pi)

Pulls new rows from the collector VPS at a fixed interval (default daily)
into a local, unbounded-retention TimescaleDB. This is the archive of record
that [training/](../training/) reads from — the collector itself only ever
holds a 90-day rolling window.

Why split it this way: the collector needs a reliable, low-latency
connection (an order book gap is unrecoverable), which home internet isn't
always great at — so it runs on a VPS instead. The archive's job is much less
time-sensitive: it just needs to catch up eventually, so a Pi behind a home
connection (or power cycle) is a fine place for it. The collector's 90-day
retention is the real safety margin: at a daily cadence that's ~90
consecutive missed runs before anything is actually lost.

## How syncing works

Each table is paginated by its primary key (reordered time-column-first) —
the archive's own current max row for a table *is* the resume position, so
there's no separate cursor to persist, and re-running (including after a
crash mid-sync) is safe: `sync_table` in [gs_archive/sync.py](gs_archive/sync.py)
just picks up where the local data already ends. Inserts use
`ON CONFLICT DO NOTHING` — the archive is a straight copy, never a live merge.

## Backfill history into the archive

The sync job only ever moves data *forward*: it resumes from the archive's own
newest row and pulls what is greater than it. That is the right behaviour for a
tail-follower, but it means history older than the archive's watermark can never
arrive that way — and the collector cannot supply it either, because its 90-day
retention drops those chunks long before a daily sync would see them.

So multi-year candle history is loaded **directly into the archive**, from the
Mac, over the tailnet:

```sh
cd geldscheisser
GS_DATABASE_URL=postgresql://gs:gs@gs-archive:5433/geldscheisser \
GS_BACKFILL_START=2021-01-01 \
GS_BACKFILL_START_OVERRIDES="1m:2025-09-01,5m:2025-09-01" \
  python -m gs_collector backfill
```

Notes:

- **It fills gaps, it does not append.** Every complete month in the window is
  compared against the candle count that month should hold and re-fetched if it
  is short, so this works against a database that already holds live data —
  backward fill and mid-history holes both. A fully-populated database re-runs
  in seconds and reports `+0 candles`; the count is rows actually inserted, not
  rows downloaded.
- **`GS_BACKFILL_START_OVERRIDES` keeps the fine intervals cheap.** A 14-day
  swing horizon does not need minute candles from 2021, and they would be ~60%
  of all rows. 1h/15m go back to 2021; 1m/5m cover roughly the last year. The
  window is trivially extended later by re-running with an earlier date.
- **It will not touch the retention policy.** `gs backfill` opens its pool with
  `manage_retention=False`, so it cannot attach the gs-common default of 90 days
  to this unbounded database — which would otherwise silently drop the history
  it had just written.
- Run it from the Mac, not the Pi: the download is the Mac's bandwidth and the
  Pi only sees the COPY stream. Measured ~3,000 rows/s end to end over the
  tailnet against the USB2 SSD.

## Deploy

**As built:** Raspberry Pi 4 Model B (4GB), Debian 13 (trixie) arm64, booting
from a **SanDisk Ultra II 500GB SATA SSD** behind a JMicron JM20329 USB–SATA
bridge, on its own power supply. Root *is* the SSD, so Docker's data root
(`/var/lib/docker`) lands there with nothing to configure and no SD card is
involved. Tailnet name `gs-archive` (`100.94.83.38`); also reachable on the
LAN as `gs-archive.local`.

Three things worth knowing about this box:

- **The SSD was vetted before use.** A previous drive here was a counterfeit
  fake-capacity USB stick that reported 1TB, silently discarded writes beyond
  its real size, and destroyed the archive on the first reboot — the page cache
  hid the problem for hours. So the acceptance check is now: real SMART
  passthrough (`smartctl -d sat -a`) showing a plausible model/serial and 0
  reallocated sectors, a sector count matching the real product, and a
  write-past-RAM/drop-caches/re-read byte comparison. This drive passes all
  three (500,107,862,016 bytes, SMART PASSED, 0 reallocated, identical
  checksums across two cache-dropped reads).
- **The link is USB 2.0, not USB 3.** The JM20329 is a USB2-only bridge, so
  throughput measures ~37.7 MB/s regardless of which port it uses. That is
  fine for a daily delta (tens of MB) but makes the *first* full sync take
  hours. A USB3 bridge (ASM1153E / JMS578) would be a strict upgrade.
- **The node is on Wi-Fi** (`wlan0`). An Ethernet cable was attached but that
  link carries no traffic — carrier up at 100Mb/s, yet `rx_packets=0` against
  66 DHCP DISCOVERs, so eth0 never got a lease. Zero inbound frames plus
  100Mb/s on a gigabit-capable Pi 4 suggests a bad cable or dead switch port.
  A Wi-Fi flap during this deploy took out the tailnet and the LAN together for
  ~a minute mid-sync; the sync is idempotent so it just resumed, but Ethernet
  would remove a whole class of transient failure. Once eth0 gets a lease
  NetworkManager prefers it automatically (route metric 100 vs Wi-Fi's 600).

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
whole day of freshness rather than a retry. `docker.service` ships with only
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

### Persistent logs

Raspberry Pi OS ships `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`
to keep the journal in RAM and spare the SD card. This box boots from an SSD, and
a memory-only journal meant an unexplained drop-out during deployment left no
evidence behind at all. Overridden with a drop-in that must sort *after* the
rpi one:

```sh
sudo tee /etc/systemd/journald.conf.d/99-persistent.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
EOF
sudo mkdir -p /var/log/journal
sudo systemctl restart systemd-journald && sudo journalctl --flush
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
