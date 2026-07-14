"""Live kline collector.

Subscribes to kline websocket streams for every (symbol, interval) pair and
persists candles once they close (`k.x == true`). Historical gaps are the
backfill job's responsibility — run `gs backfill` after downtime.
"""

import asyncio
import logging

from gs_common.config import load_settings
from gs_common.db import Heartbeat, create_pool, ms_to_dt
from gs_common.log import setup_logging
from gs_common.ws import run_combined_stream

log = logging.getLogger(__name__)

UPSERT = """
INSERT INTO klines (symbol, interval, open_time, open, high, low, close,
                    volume, quote_volume, trades, taker_buy_volume, taker_buy_quote_volume)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume,
    quote_volume = EXCLUDED.quote_volume, trades = EXCLUDED.trades,
    taker_buy_volume = EXCLUDED.taker_buy_volume,
    taker_buy_quote_volume = EXCLUDED.taker_buy_quote_volume
"""


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    hb = Heartbeat(pool, "klines", settings.heartbeat_interval_s)

    streams = [
        f"{sym.lower()}@kline_{iv}"
        for sym in settings.symbols
        for iv in settings.kline_intervals
    ]

    async def on_message(_stream: str, data: dict) -> None:
        k = data["k"]
        if not k["x"]:  # candle still open
            return
        await pool.execute(
            UPSERT,
            k["s"], k["i"], ms_to_dt(k["t"]),
            float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
            float(k["v"]), float(k["q"]), int(k["n"]),
            float(k["V"]), float(k["Q"]),
        )
        hb.bump("closed_klines")

    log.info("collecting klines for %d symbols × %s", len(settings.symbols), settings.kline_intervals)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(hb.run())
            tg.create_task(run_combined_stream(settings.spot_ws_base, streams, on_message, name="klines"))
    finally:
        await pool.close()
