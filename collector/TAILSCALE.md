# Collector network isolation via a Tailscale sidecar

**Status: implemented in `collector/docker-compose.yml` 2026-08-19, not yet
deployed/verified on the actual VPS.** The compose file below is what's
currently checked in — this doc now describes the as-built design rather
than a proposal. Remaining before it's confirmed working: `TS_AUTHKEY` set on
the VPS, redeployed, `tailscale serve` run once, and reachability verified
both ways (see the checklist items in `CHECKLIST.md`).

## Goal

Let the archive (Pi) reach the collector's Postgres without publishing *any*
port on the VPS host — not even loopback-bound via `GS_BIND_ADDR` — so the
only thing reachable from outside the host at all is SSH.

## Why Tailscale, not WireGuard

Self-hosted WireGuard needs an inbound UDP port open on at least one peer for
the handshake — normally the VPS, since it has the stable public IP and the
Pi is the one behind NAT. That directly conflicts with "nothing but SSH
exposed." Tailscale avoids this: it dials *out* to Tailscale's coordination
service and uses NAT traversal/relays, so neither side needs an inbound port
at all. It's built on WireGuard, just with that coordination layer added.

## Mechanism

Tailscale's documented pattern for this is a sidecar container that owns a
shared network namespace; other containers attach to it via
`network_mode: "service:tailscale"`. Forwarding a raw TCP port (Postgres
isn't HTTP, so `tailscale serve`'s HTTPS-proxy mode doesn't apply) works via:

```
tailscale serve --bg --tcp=5432 tcp://localhost:5432
```

The important constraint: this targets `localhost`, meaning whatever it
forwards to must share the tailscale container's network namespace — not
just sit on the same Docker bridge network reachable by DNS name. That's why
this isn't a small bolt-on: **every** collector service needs to join that
shared namespace, not just `timescaledb`, since they all currently reach
Postgres via the bridge network's `timescaledb:5432` DNS name, which stops
resolving once `timescaledb` leaves that network for the shared namespace.

## Compose sketch

```yaml
services:
  tailscale:
    image: tailscale/tailscale:latest
    hostname: gs-collector
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY}
      TS_STATE_DIR: /var/lib/tailscale
      TS_USERSPACE: "false"        # kernel networking — needed for `serve`
    volumes:
      - tailscale-state:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add: [NET_ADMIN, NET_RAW]
    restart: unless-stopped

  timescaledb:
    network_mode: "service:tailscale"
    # no `ports:` section at all — nothing published to the host
    ...

  klines:                     # and orderbook, aggtrades, futures, monitor, backfill
    network_mode: "service:tailscale"
    environment:
      GS_DATABASE_URL: postgresql://gs:gs@localhost:5432/geldscheisser   # was "timescaledb"
    ...

volumes:
  tailscale-state:
```

`GS_BIND_ADDR` and the `ports:` mapping it currently governs become
unnecessary entirely under this design — there's no published host port to
bind at all, which is strictly tighter than the current loopback-only default.

## Setup steps

1. Generate a Tailscale pre-auth key in the admin console — tagged (e.g.
   `tag:collector`) and non-ephemeral, since this is a long-lived server, not
   a throwaway node. Put it in `collector/.env` as `TS_AUTHKEY`.
2. Update `collector/docker-compose.yml` per the sketch above: add the
   `tailscale` service, switch every other service to
   `network_mode: "service:tailscale"`, change `GS_DATABASE_URL`'s host from
   `timescaledb` to `localhost` everywhere it's set, drop the `ports:`
   section, add `depends_on: [tailscale]` where `depends_on: timescaledb`
   exists today (ordering still matters even though it's now a namespace
   dependency rather than a network one).
3. Deploy, then once: `docker compose exec tailscale tailscale serve --bg --tcp=5432 tcp://localhost:5432`.
4. Confirm Postgres is reachable at `gs-collector:5432` (Tailscale MagicDNS)
   from another tailnet peer, and confirm it is **not** reachable from the
   public internet at all (e.g. `nc -zv <public-ip> 5433` should simply fail
   — there's nothing listening there anymore).

## Before applying to the live VPS

- Validate the restructured compose file (`docker compose config`, then a
  full `up` cycle) somewhere non-production first — this touches every
  service's networking on a stack that's actively collecting irreplaceable
  order-book data.
- Have a rollback plan: keep the current (pre-restructure) compose file
  committed/tagged so a revert is a one-line `git checkout` away.
- Double check whether `condition: service_healthy` ordering (currently used
  for `timescaledb`) still behaves as expected once dependents reach it via
  `network_mode: "service:tailscale"` rather than the bridge network —
  hasn't been verified here.

## The archive side is unaffected in concept

The Pi still needs to be a Tailscale peer either way (as a container, or the
host — whichever the existing archive setup assumes) — this doc only changes
*how* the collector side exposes itself, not the fact that both ends need to
be on the same tailnet.
