"""Aggregated-trade collector: per-minute taker buy/sell flow.

Buckets the @aggTrade stream into 1-minute rows (`m == true` means the buyer
was the maker, i.e. an aggressive sell). Upserts are additive so partial
buckets from restarts merge instead of clobbering.
"""

import asyncio
import logging
import time

from gs_common.config import load_settings
from gs_common.db import Heartbeat, create_pool, ms_to_dt
from gs_common.log import setup_logging
from gs_common.ws import run_combined_stream

log = logging.getLogger(__name__)

UPSERT = """
INSERT INTO agg_trades_1m (symbol, minute, buy_volume, sell_volume, buy_quote, sell_quote, trades)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (symbol, minute) DO UPDATE SET
    buy_volume  = agg_trades_1m.buy_volume  + EXCLUDED.buy_volume,
    sell_volume = agg_trades_1m.sell_volume + EXCLUDED.sell_volume,
    buy_quote   = agg_trades_1m.buy_quote   + EXCLUDED.buy_quote,
    sell_quote  = agg_trades_1m.sell_quote  + EXCLUDED.sell_quote,
    trades      = agg_trades_1m.trades      + EXCLUDED.trades
"""


class MinuteBuckets:
    def __init__(self):
        # (symbol, minute_ms) -> [buy_vol, sell_vol, buy_quote, sell_quote, trades]
        self.buckets: dict[tuple[str, int], list] = {}

    def add(self, symbol: str, trade_time_ms: int, price: float, qty: float, buyer_is_maker: bool) -> None:
        minute = trade_time_ms - trade_time_ms % 60_000
        b = self.buckets.setdefault((symbol, minute), [0.0, 0.0, 0.0, 0.0, 0])
        quote = price * qty
        if buyer_is_maker:  # taker sold
            b[1] += qty
            b[3] += quote
        else:  # taker bought
            b[0] += qty
            b[2] += quote
        b[4] += 1

    def pop_completed(self, now_ms: int) -> list[tuple]:
        """Rows for minutes that have fully elapsed."""
        current_minute = now_ms - now_ms % 60_000
        done = [k for k in self.buckets if k[1] < current_minute]
        return [
            (sym, ms_to_dt(minute), *self.buckets.pop((sym, minute)))
            for sym, minute in done
        ]


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    hb = Heartbeat(pool, "aggtrades", settings.heartbeat_interval_s)
    buckets = MinuteBuckets()
    streams = [f"{sym.lower()}@aggTrade" for sym in settings.symbols]

    async def on_message(_stream: str, t: dict) -> None:
        buckets.add(t["s"], t["T"], float(t["p"]), float(t["q"]), t["m"])

    async def flush_loop() -> None:
        while True:
            await asyncio.sleep(5)
            rows = buckets.pop_completed(int(time.time() * 1000))
            if rows:
                try:
                    await pool.executemany(UPSERT, rows)
                    hb.bump("minutes_flushed", len(rows))
                except Exception:
                    log.exception("aggtrade flush failed")

    log.info("collecting aggTrades for %d symbols", len(settings.symbols))
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(hb.run())
            tg.create_task(flush_loop())
            tg.create_task(run_combined_stream(settings.spot_ws_base, streams, on_message, name="aggtrades"))
    finally:
        await pool.close()
