"""Futures sentiment collector: funding rates + open interest (USDT perps).

Both are strong regime/sentiment features for multi-day horizons. Funding is
fully backfillable (done at startup); open-interest history only reaches back
~30 days, so it is polled forward — another collect-from-day-one dataset.
Symbols without a USDT perp are detected once and skipped.
"""

import asyncio
import logging
import time

import aiohttp

from gs_common.config import load_settings
from gs_common.db import Heartbeat, create_pool, ms_to_dt
from gs_common.log import setup_logging
from gs_common.rest import RestClient

log = logging.getLogger(__name__)

UPSERT_FUNDING = """
INSERT INTO funding_rates (symbol, funding_time, rate) VALUES ($1, $2, $3)
ON CONFLICT (symbol, funding_time) DO NOTHING
"""
UPSERT_OI = """
INSERT INTO open_interest (symbol, ts, oi, oi_value) VALUES ($1, $2, $3, $4)
ON CONFLICT (symbol, ts) DO NOTHING
"""

OI_POLL_S = 300
FUNDING_POLL_S = 3600


def _is_bad_symbol(e: Exception) -> bool:
    return isinstance(e, aiohttp.ClientResponseError) and e.status in (400, 404)


async def backfill_funding(pool, rest: RestClient, symbol: str, start_ms: int) -> int:
    latest = await pool.fetchval(
        "SELECT max(funding_time) FROM funding_rates WHERE symbol = $1", symbol)
    cursor = int(latest.timestamp() * 1000) + 1 if latest else start_ms
    total = 0
    while True:
        rows = await rest.get_json("/fapi/v1/fundingRate", {
            "symbol": symbol, "startTime": cursor, "limit": 1000,
        })
        if not rows:
            break
        await pool.executemany(UPSERT_FUNDING, [
            (symbol, ms_to_dt(int(r["fundingTime"])), float(r["fundingRate"])) for r in rows
        ])
        total += len(rows)
        cursor = int(rows[-1]["fundingTime"]) + 1
        if len(rows) < 1000:
            break
    return total


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    hb = Heartbeat(pool, "futures", settings.heartbeat_interval_s)
    start_ms = int(time.mktime(settings.backfill_start.timetuple()) * 1000)
    no_perp: set[str] = set()

    async def poll_funding(rest: RestClient) -> None:
        while True:
            for symbol in settings.symbols:
                if symbol in no_perp:
                    continue
                try:
                    n = await backfill_funding(pool, rest, symbol, start_ms)
                    hb.bump("funding_rows", n)
                except Exception as e:
                    if _is_bad_symbol(e):
                        no_perp.add(symbol)
                        log.warning("%s has no USDT perp, skipping futures data", symbol)
                    else:
                        log.exception("funding poll failed for %s", symbol)
            await hb.beat("funding")
            await asyncio.sleep(FUNDING_POLL_S)

    async def poll_open_interest(rest: RestClient) -> None:
        while True:
            for symbol in settings.symbols:
                if symbol in no_perp:
                    continue
                try:
                    rows = await rest.get_json("/futures/data/openInterestHist", {
                        "symbol": symbol, "period": "5m", "limit": 12,
                    })
                    await pool.executemany(UPSERT_OI, [
                        (symbol, ms_to_dt(int(r["timestamp"])),
                         float(r["sumOpenInterest"]), float(r["sumOpenInterestValue"]))
                        for r in rows
                    ])
                    hb.bump("oi_rows", len(rows))
                except Exception as e:
                    if _is_bad_symbol(e):
                        no_perp.add(symbol)
                        log.warning("%s has no USDT perp, skipping futures data", symbol)
                    else:
                        log.exception("open-interest poll failed for %s", symbol)
            await asyncio.sleep(OI_POLL_S)

    log.info("collecting funding + open interest for %d symbols", len(settings.symbols))
    try:
        async with aiohttp.ClientSession() as session:
            rest = RestClient(session, settings.futures_rest_base)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(hb.run())
                tg.create_task(poll_funding(rest))
                tg.create_task(poll_open_interest(rest))
    finally:
        await pool.close()
