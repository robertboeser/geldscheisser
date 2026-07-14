"""Postgres/TimescaleDB access: pool, idempotent schema, heartbeats.

The schema is applied on every service start. Timescale-specific statements
(hypertables, compression) degrade gracefully to plain Postgres so unit/dev
setups without the extension still work — production runs on TimescaleDB.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import asyncpg

from .config import Settings

log = logging.getLogger(__name__)

_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS klines (
        symbol                  text NOT NULL,
        interval                text NOT NULL,
        open_time               timestamptz NOT NULL,
        open                    double precision NOT NULL,
        high                    double precision NOT NULL,
        low                     double precision NOT NULL,
        close                   double precision NOT NULL,
        volume                  double precision NOT NULL,
        quote_volume            double precision NOT NULL,
        trades                  bigint NOT NULL,
        taker_buy_volume        double precision NOT NULL,
        taker_buy_quote_volume  double precision NOT NULL,
        PRIMARY KEY (symbol, interval, open_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS book_rollups (
        symbol          text NOT NULL,
        ts              timestamptz NOT NULL,
        best_bid        double precision NOT NULL,
        best_ask        double precision NOT NULL,
        mid             double precision NOT NULL,
        spread_bps      double precision NOT NULL,
        bid_qty_10bps   double precision NOT NULL,
        ask_qty_10bps   double precision NOT NULL,
        imb_10bps       double precision,
        bid_qty_50bps   double precision NOT NULL,
        ask_qty_50bps   double precision NOT NULL,
        imb_50bps       double precision,
        bid_qty_100bps  double precision NOT NULL,
        ask_qty_100bps  double precision NOT NULL,
        imb_100bps      double precision,
        bid_qty_200bps  double precision NOT NULL,
        ask_qty_200bps  double precision NOT NULL,
        imb_200bps      double precision,
        PRIMARY KEY (symbol, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS book_snapshots (
        symbol          text NOT NULL,
        ts              timestamptz NOT NULL,
        last_update_id  bigint NOT NULL,
        bids            jsonb NOT NULL,
        asks            jsonb NOT NULL,
        PRIMARY KEY (symbol, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agg_trades_1m (
        symbol      text NOT NULL,
        minute      timestamptz NOT NULL,
        buy_volume  double precision NOT NULL,
        sell_volume double precision NOT NULL,
        buy_quote   double precision NOT NULL,
        sell_quote  double precision NOT NULL,
        trades      bigint NOT NULL,
        PRIMARY KEY (symbol, minute)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS funding_rates (
        symbol       text NOT NULL,
        funding_time timestamptz NOT NULL,
        rate         double precision NOT NULL,
        PRIMARY KEY (symbol, funding_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS open_interest (
        symbol    text NOT NULL,
        ts        timestamptz NOT NULL,
        oi        double precision NOT NULL,
        oi_value  double precision NOT NULL,
        PRIMARY KEY (symbol, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heartbeats (
        service   text NOT NULL,
        component text NOT NULL DEFAULT '',
        last_seen timestamptz NOT NULL,
        detail    text NOT NULL DEFAULT '',
        PRIMARY KEY (service, component)
    )
    """,
]

# Applied best-effort; harmless failures (no timescale, policy exists) are logged at debug.
_TIMESCALE = [
    "CREATE EXTENSION IF NOT EXISTS timescaledb",
    "SELECT create_hypertable('klines', 'open_time', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)",
    "SELECT create_hypertable('book_rollups', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)",
    "SELECT create_hypertable('book_snapshots', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)",
    "SELECT create_hypertable('agg_trades_1m', 'minute', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)",
    """ALTER TABLE klines SET (timescaledb.compress,
        timescaledb.compress_segmentby = 'symbol, interval', timescaledb.compress_orderby = 'open_time')""",
    """ALTER TABLE book_rollups SET (timescaledb.compress,
        timescaledb.compress_segmentby = 'symbol', timescaledb.compress_orderby = 'ts')""",
    """ALTER TABLE book_snapshots SET (timescaledb.compress,
        timescaledb.compress_segmentby = 'symbol', timescaledb.compress_orderby = 'ts')""",
    """ALTER TABLE agg_trades_1m SET (timescaledb.compress,
        timescaledb.compress_segmentby = 'symbol', timescaledb.compress_orderby = 'minute')""",
    "SELECT add_compression_policy('klines', INTERVAL '14 days', if_not_exists => TRUE)",
    "SELECT add_compression_policy('book_rollups', INTERVAL '14 days', if_not_exists => TRUE)",
    "SELECT add_compression_policy('book_snapshots', INTERVAL '14 days', if_not_exists => TRUE)",
    "SELECT add_compression_policy('agg_trades_1m', INTERVAL '14 days', if_not_exists => TRUE)",
]


_HYPERTABLES = ["klines", "book_rollups", "book_snapshots", "agg_trades_1m"]


def _retention_statements(retention_days: int) -> list[str]:
    """Short-retention mode for the execution VPS; the Pi (0) keeps everything."""
    if retention_days > 0:
        return [
            f"SELECT add_retention_policy('{t}', INTERVAL '{retention_days} days', if_not_exists => TRUE)"
            for t in _HYPERTABLES
        ]
    return [f"SELECT remove_retention_policy('{t}', if_exists => TRUE)" for t in _HYPERTABLES]


async def create_pool(settings: Settings) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    await ensure_schema(pool, settings.retention_days)
    return pool


async def ensure_schema(pool: asyncpg.Pool, retention_days: int = 0) -> None:
    async with pool.acquire() as conn:
        # Serialize concurrent service startups.
        await conn.execute("SELECT pg_advisory_lock(815001)")
        try:
            for stmt in _TABLES:
                await conn.execute(stmt)
            timescale_ok = True
            for stmt in _TIMESCALE + _retention_statements(retention_days):
                try:
                    await conn.execute(stmt)
                except asyncpg.PostgresError as e:
                    if stmt is _TIMESCALE[0]:
                        timescale_ok = False
                    log.debug("timescale statement skipped (%s): %s", e.__class__.__name__, e)
            if not timescale_ok:
                log.warning("timescaledb extension unavailable — running on plain Postgres")
        finally:
            await conn.execute("SELECT pg_advisory_unlock(815001)")


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


class Heartbeat:
    """Periodic per-service liveness row + message counters, watched by the monitor."""

    def __init__(self, pool: asyncpg.Pool, service: str, interval_s: float = 30.0):
        self.pool = pool
        self.service = service
        self.interval_s = interval_s
        self.counters: dict[str, int] = {}

    def bump(self, counter: str, n: int = 1) -> None:
        self.counters[counter] = self.counters.get(counter, 0) + n

    async def beat(self, component: str = "") -> None:
        detail = json.dumps(self.counters) if self.counters else ""
        await self.pool.execute(
            """INSERT INTO heartbeats (service, component, last_seen, detail)
               VALUES ($1, $2, now(), $3)
               ON CONFLICT (service, component)
               DO UPDATE SET last_seen = now(), detail = EXCLUDED.detail""",
            self.service, component, detail,
        )

    async def run(self) -> None:
        while True:
            try:
                await self.beat()
            except Exception:
                log.exception("heartbeat write failed")
            await asyncio.sleep(self.interval_s)
