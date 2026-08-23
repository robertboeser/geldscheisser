"""Archive sync: pull new rows from the collector into the local, unbounded
archive on a fixed interval (default daily).

Each table is paginated by its full primary key, reordered time-column-first
(`keyset_cols`) — the destination's own current max row *is* the resume
position, so there is no separate cursor to persist and re-runs (including
partial ones) are simply idempotent. Inserts use `ON CONFLICT DO NOTHING`:
the archive is a straight copy of history, never a live merge, so unlike the
collector's own upserts it must never need to overwrite a row it already has.

**Invariant for anything else that writes to the archive.** Because the resume
position is a single `(time, ...)` tuple and the walk only ever moves forward,
the archive must stay a *prefix* of the collector in that ordering: every source
row below the archive's maximum has to be present already. A writer that pushes
the maximum up unevenly silently strands everything in between — the sync can
never look back. `gs backfill` is the one other writer, and it takes a run-wide
cap for exactly this reason (see `backfill_pair`): it fills history *below* the
watermark and leaves the recent tail to this job.

The collector's retention window (e.g. 90 days) only needs to outlive the
sync interval by a comfortable margin — at a daily cadence that is ~90
consecutive missed runs of slack. A missed run does not lose data, it just
delays archiving it.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import asyncpg

from gs_common.alerts import Alerter
from gs_common.config import load_settings
from gs_common.db import Heartbeat, create_pool
from gs_common.log import setup_logging

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableSync:
    name: str
    keyset_cols: tuple[str, ...]    # pagination/ordering key, time column first — must
                                    # be a (possibly reordered) full primary key so tuples
                                    # are unique and keyset pagination can't skip/duplicate
    select_cols: tuple[str, ...]    # full row, in INSERT column order
    conflict_cols: tuple[str, ...]  # actual primary key, for ON CONFLICT
    casts: dict[str, str] = field(default_factory=dict)  # column -> pg type, e.g. jsonb


TABLES = [
    TableSync(
        name="klines",
        keyset_cols=("open_time", "symbol", "interval"),
        select_cols=("symbol", "interval", "open_time", "open", "high", "low", "close",
                     "volume", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume"),
        conflict_cols=("symbol", "interval", "open_time"),
    ),
    TableSync(
        name="book_rollups",
        keyset_cols=("ts", "symbol"),
        select_cols=("symbol", "ts", "best_bid", "best_ask", "mid", "spread_bps",
                     "bid_qty_10bps", "ask_qty_10bps", "imb_10bps",
                     "bid_qty_50bps", "ask_qty_50bps", "imb_50bps",
                     "bid_qty_100bps", "ask_qty_100bps", "imb_100bps",
                     "bid_qty_200bps", "ask_qty_200bps", "imb_200bps"),
        conflict_cols=("symbol", "ts"),
    ),
    TableSync(
        name="book_snapshots",
        keyset_cols=("ts", "symbol"),
        select_cols=("symbol", "ts", "last_update_id", "bids", "asks"),
        conflict_cols=("symbol", "ts"),
        casts={"bids": "jsonb", "asks": "jsonb"},
    ),
    TableSync(
        name="agg_trades_1m",
        keyset_cols=("minute", "symbol"),
        select_cols=("symbol", "minute", "buy_volume", "sell_volume", "buy_quote", "sell_quote", "trades"),
        conflict_cols=("symbol", "minute"),
    ),
    TableSync(
        name="funding_rates",
        keyset_cols=("funding_time", "symbol"),
        select_cols=("symbol", "funding_time", "rate"),
        conflict_cols=("symbol", "funding_time"),
    ),
    TableSync(
        name="open_interest",
        keyset_cols=("ts", "symbol"),
        select_cols=("symbol", "ts", "oi", "oi_value"),
        conflict_cols=("symbol", "ts"),
    ),
]


def _select_sql(table: TableSync, has_watermark: bool) -> str:
    cols = ", ".join(table.select_cols)
    order = ", ".join(table.keyset_cols)
    if has_watermark:
        key = ", ".join(table.keyset_cols)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(table.keyset_cols)))
        limit_ph = f"${len(table.keyset_cols) + 1}"
        where = f"WHERE ({key}) > ({placeholders}) "
    else:
        where = ""
        limit_ph = "$1"
    return f"SELECT {cols} FROM {table.name} {where}ORDER BY {order} LIMIT {limit_ph}"


def _insert_sql(table: TableSync) -> str:
    cols = ", ".join(table.select_cols)
    placeholders = ", ".join(
        f"${i + 1}::{table.casts[c]}" if c in table.casts else f"${i + 1}"
        for i, c in enumerate(table.select_cols)
    )
    conflict = ", ".join(table.conflict_cols)
    return f"INSERT INTO {table.name} ({cols}) VALUES ({placeholders}) ON CONFLICT ({conflict}) DO NOTHING"


async def sync_table(
    source, dest, table: TableSync, batch_size: int, lag: timedelta = timedelta(0)
) -> int:
    """Copy rows at or after (destination max - `lag`) into dest, in pages.

    The lag is what makes this correct, not just efficient. Resuming exactly at
    the watermark assumes rows are *inserted* in keyset order — false for
    klines, where a 15m candle is written when it closes, fifteen minutes after
    its own open_time, by which point 1m candles have already pushed the
    watermark past it. Its tuple sorts below the cursor, so a forward-only walk
    skips it permanently. Restarting a little behind the watermark catches every
    such late arrival; the re-read costs nothing because inserts are
    ON CONFLICT DO NOTHING.

    Returns rows *transferred*, which with a non-zero lag includes the overlap
    the archive already holds — so a quiet sync no longer reports 0. Reporting
    true insert counts would mean staging each page (see `gs backfill`'s
    `upsert_chunks`) rather than `executemany`, which cannot return a count.
    """
    order_desc = ", ".join(f"{c} DESC" for c in table.keyset_cols)
    last = await dest.fetchrow(
        f"SELECT {', '.join(table.keyset_cols)} FROM {table.name} ORDER BY {order_desc} LIMIT 1"
    )
    if last is None:
        cursor = None
    elif lag:
        # Time column first, then the minimum of every remaining key column, so
        # the comparison admits all rows at the rewound instant. Those are all
        # text (symbol/interval), whose minimum is the empty string.
        cursor = (last[table.keyset_cols[0]] - lag,) + ("",) * (len(table.keyset_cols) - 1)
    else:
        cursor = tuple(last[c] for c in table.keyset_cols)
    # Both variants up front, chosen per page: a sync starting from an empty
    # table has no watermark for its *first* page only — every page after it
    # does, so the statement has to change once the cursor appears.
    sql_first = _select_sql(table, has_watermark=False)
    sql_resume = _select_sql(table, has_watermark=True)
    insert_sql = _insert_sql(table)

    total = 0
    while True:
        if cursor:
            rows = await source.fetch(sql_resume, *cursor, batch_size)
        else:
            rows = await source.fetch(sql_first, batch_size)
        if not rows:
            break
        records = [tuple(r[c] for c in table.select_cols) for r in rows]
        await dest.executemany(insert_sql, records)
        total += len(records)
        cursor = tuple(rows[-1][c] for c in table.keyset_cols)
        if len(rows) < batch_size:
            break
    return total


async def sync_once(source, dest, batch_size: int, lag: timedelta = timedelta(0)) -> dict[str, int]:
    return {table.name: await sync_table(source, dest, table, batch_size, lag) for table in TABLES}


def _redact(dsn: str) -> str:
    return dsn.split("@")[-1] if "@" in dsn else dsn


async def main() -> None:
    """Long-running service: sync on a fixed interval (default daily)."""
    settings = load_settings()
    setup_logging(settings.log_level)
    if not settings.source_database_url:
        raise SystemExit("GS_SOURCE_DATABASE_URL is required to run the archive")

    # The archive is unbounded by definition, regardless of any stray
    # GS_RETENTION_DAYS picked up from the environment.
    dest_pool = await create_pool(settings.model_copy(update={"retention_days": 0}))
    source_pool = await asyncpg.create_pool(settings.source_database_url, min_size=1, max_size=3)
    hb = Heartbeat(dest_pool, "archive", settings.heartbeat_interval_s)
    alerter = Alerter(settings)

    async def sync_loop() -> None:
        while True:
            try:
                results = await sync_once(
                    source_pool, dest_pool, settings.archive_batch_size,
                    timedelta(seconds=settings.archive_sync_lag_s))
                total = sum(results.values())
                log.info("sync complete: %s (total %d rows)", results, total)
                if total:
                    await alerter.info(
                        "📦 Archive sync: " + ", ".join(f"{k}=+{v}" for k, v in results.items() if v)
                    )
                hb.bump("sync_runs")
                hb.bump("rows_synced", total)
            except Exception:
                log.exception("archive sync failed")
                await alerter.alert("archive-sync-failed", "Archive sync run failed — check logs.")
            await asyncio.sleep(settings.archive_sync_interval_s)

    log.info("archive syncing every %.0fs from %s", settings.archive_sync_interval_s,
             _redact(settings.source_database_url))
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(hb.run())
            tg.create_task(sync_loop())
    finally:
        await alerter.close()
        await source_pool.close()
        await dest_pool.close()


async def sync_once_main() -> None:
    """One-shot entry point — for cron/systemd-timer style invocation instead
    of the long-running service, or for a manual catch-up run."""
    settings = load_settings()
    setup_logging(settings.log_level)
    if not settings.source_database_url:
        raise SystemExit("GS_SOURCE_DATABASE_URL is required to run the archive")
    dest_pool = await create_pool(settings.model_copy(update={"retention_days": 0}))
    source_pool = await asyncpg.create_pool(settings.source_database_url, min_size=1, max_size=3)
    try:
        results = await sync_once(source_pool, dest_pool, settings.archive_batch_size,
                                  timedelta(seconds=settings.archive_sync_lag_s))
        total = sum(results.values())
        print(f"synced {total} rows: {results}")
    finally:
        await source_pool.close()
        await dest_pool.close()
