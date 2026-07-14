"""Historical kline backfill.

Strategy per (symbol, interval): bulk-load complete months from the public
data dumps at data.binance.vision (fast, no API weight), then top up the
remainder — current month up to the last closed candle — via the REST API.
Resumes from the newest candle already in the database, so it is safe to
re-run any time (e.g. after collector downtime).

Note: monthly dump files from 2025-01 onward use microsecond timestamps;
`normalize_ts_ms` handles both generations.
"""

import asyncio
import csv
import io
import logging
import time
import zipfile
from datetime import date

import aiohttp

from gs_common.config import INTERVAL_MS, Settings, load_settings
from gs_common.db import create_pool, ms_to_dt
from gs_common.log import setup_logging
from gs_common.rest import RestClient

log = logging.getLogger(__name__)

UPSERT = """
INSERT INTO klines (symbol, interval, open_time, open, high, low, close,
                    volume, quote_volume, trades, taker_buy_volume, taker_buy_quote_volume)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (symbol, interval, open_time) DO NOTHING
"""

CHUNK = 5000


def normalize_ts_ms(ts: int) -> int:
    """Binance vision dumps switched to microsecond timestamps in 2025."""
    return ts // 1000 if ts > 10**14 else ts


def complete_months(start: date, today: date) -> list[tuple[int, int]]:
    """(year, month) pairs from `start` up to but excluding the current month."""
    months = []
    y, m = start.year, start.month
    while (y, m) < (today.year, today.month):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def parse_vision_csv(raw: bytes, symbol: str, interval: str) -> list[tuple]:
    records = []
    for row in csv.reader(io.StringIO(raw.decode())):
        if not row or not row[0].strip().isdigit():  # header line in newer files
            continue
        records.append((
            symbol, interval, ms_to_dt(normalize_ts_ms(int(row[0]))),
            float(row[1]), float(row[2]), float(row[3]), float(row[4]),
            float(row[5]), float(row[7]), int(row[8]), float(row[9]), float(row[10]),
        ))
    return records


async def upsert_chunks(pool, records: list[tuple]) -> None:
    for i in range(0, len(records), CHUNK):
        await pool.executemany(UPSERT, records[i:i + CHUNK])


async def backfill_pair(
    pool, rest: RestClient, session: aiohttp.ClientSession, settings: Settings,
    symbol: str, interval: str,
) -> int:
    interval_ms = INTERVAL_MS[interval]
    latest = await pool.fetchval(
        "SELECT max(open_time) FROM klines WHERE symbol = $1 AND interval = $2",
        symbol, interval,
    )
    start = latest.date() if latest else settings.backfill_start
    total = 0

    # Phase 1: complete months from the public dumps.
    for y, m in complete_months(start, date.today()):
        url = (f"{settings.vision_base}/data/spot/monthly/klines/"
               f"{symbol}/{interval}/{symbol}-{interval}-{y:04d}-{m:02d}.zip")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status == 404:  # symbol not listed yet that month
                continue
            resp.raise_for_status()
            payload = await resp.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            raw = zf.read(zf.namelist()[0])
        records = parse_vision_csv(raw, symbol, interval)
        await upsert_chunks(pool, records)
        total += len(records)
        log.info("%s %s %04d-%02d: %d candles from dump", symbol, interval, y, m, len(records))
        await asyncio.sleep(0.1)

    # Phase 2: REST top-up to the last closed candle.
    latest = await pool.fetchval(
        "SELECT max(open_time) FROM klines WHERE symbol = $1 AND interval = $2",
        symbol, interval,
    )
    cursor = (int(latest.timestamp() * 1000) + interval_ms if latest
              else int(time.mktime(settings.backfill_start.timetuple()) * 1000))
    rest_total = 0
    while True:
        now_ms = int(time.time() * 1000)
        if cursor > now_ms - interval_ms:
            break
        rows = await rest.get_json("/api/v3/klines", {
            "symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000,
        })
        # Keep closed candles only.
        rows = [r for r in rows if int(r[6]) < now_ms]
        if not rows:
            break
        records = [(
            symbol, interval, ms_to_dt(int(r[0])),
            float(r[1]), float(r[2]), float(r[3]), float(r[4]),
            float(r[5]), float(r[7]), int(r[8]), float(r[9]), float(r[10]),
        ) for r in rows]
        await upsert_chunks(pool, records)
        rest_total += len(records)
        cursor = int(rows[-1][0]) + interval_ms
    if rest_total:
        log.info("%s %s: %d candles via REST", symbol, interval, rest_total)
    return total + rest_total


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    grand_total = 0
    try:
        async with aiohttp.ClientSession() as session:
            rest = RestClient(session, settings.spot_rest_base)
            for symbol in settings.symbols:
                for interval in settings.kline_intervals:
                    n = await backfill_pair(pool, rest, session, settings, symbol, interval)
                    grand_total += n
                    log.info("done: %s %s (+%d candles)", symbol, interval, n)
    finally:
        await pool.close()
    log.info("backfill complete: %d candles inserted", grand_total)
