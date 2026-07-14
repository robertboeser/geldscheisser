"""Live order book collector.

Maintains a local book per symbol from the REST depth snapshot plus the
`@depth@100ms` diff stream, using Binance's documented sync algorithm
(buffer diffs, fetch snapshot, drop stale diffs, verify sequence continuity,
resync on any gap). Persists:

- `book_rollups` every ~10s: best bid/ask, spread, and depth/imbalance inside
  10/50/100/200 bps bands around mid — the compact features research will use.
- `book_snapshots` every ~5min: top 500 levels per side, for recomputing
  richer features later.

The spot diff stream carries *all* book changes; only the initial REST
snapshot is depth-truncated, so an untouched level is still valid and the
local book stays correct between resyncs.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp

from gs_common.config import Settings, load_settings
from gs_common.db import Heartbeat, create_pool
from gs_common.log import setup_logging
from gs_common.rest import RestClient
from gs_common.ws import run_combined_stream

log = logging.getLogger(__name__)

BANDS_BPS = (10, 50, 100, 200)

INSERT_ROLLUP = """
INSERT INTO book_rollups (symbol, ts, best_bid, best_ask, mid, spread_bps,
    bid_qty_10bps, ask_qty_10bps, imb_10bps,
    bid_qty_50bps, ask_qty_50bps, imb_50bps,
    bid_qty_100bps, ask_qty_100bps, imb_100bps,
    bid_qty_200bps, ask_qty_200bps, imb_200bps)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)
ON CONFLICT (symbol, ts) DO NOTHING
"""

INSERT_SNAPSHOT = """
INSERT INTO book_snapshots (symbol, ts, last_update_id, bids, asks)
VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)
ON CONFLICT (symbol, ts) DO NOTHING
"""

MAX_BUFFER = 20_000  # bound diff buffering while a resync is in flight


class OrderBook:
    """Book state + Binance diff-stream sync logic (transport-free, unit-testable)."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: dict[str, float] = {}  # price string -> base qty
        self.asks: dict[str, float] = {}
        self.last_update_id = 0
        self.prev_u: int | None = None
        self.live = False
        self.buffer: list[dict] = []

    def load_snapshot(self, last_update_id: int, bids: list, asks: list) -> None:
        self.bids = {p: float(q) for p, q in bids}
        self.asks = {p: float(q) for p, q in asks}
        self.last_update_id = last_update_id
        self.prev_u = None

    def apply(self, event: dict) -> bool:
        """Apply one diff event. Returns False on a sequence gap (resync needed)."""
        first_after_snapshot = self.prev_u is None
        if first_after_snapshot:
            if event["u"] <= self.last_update_id:
                return True  # predates the snapshot, skip
            if event["U"] > self.last_update_id + 1:
                return False  # updates between snapshot and this event were missed
        else:
            if event["u"] <= self.prev_u:
                return True  # duplicate/old
            if event["U"] != self.prev_u + 1:
                return False
        for price, qty in event["b"]:
            if float(qty) == 0.0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = float(qty)
        for price, qty in event["a"]:
            if float(qty) == 0.0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = float(qty)
        self.prev_u = event["u"]
        self.last_update_id = event["u"]
        return True

    def drain_buffer(self) -> bool:
        """Replay buffered diffs onto a fresh snapshot. False -> snapshot too old, refetch."""
        for event in self.buffer:
            if not self.apply(event):
                return False
        self.buffer.clear()
        return True

    def reset(self) -> None:
        self.live = False
        self.buffer.clear()
        self.prev_u = None


def compute_rollup(bids: dict[str, float], asks: dict[str, float],
                   bands_bps=BANDS_BPS) -> dict | None:
    """Band depth/imbalance features. None if the book is empty or crossed."""
    if not bids or not asks:
        return None
    best_bid = max(float(p) for p in bids)
    best_ask = min(float(p) for p in asks)
    if best_bid >= best_ask:
        return None  # crossed book: state is corrupt, caller must resync
    mid = (best_bid + best_ask) / 2
    out = {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": (best_ask - best_bid) / mid * 10_000,
    }
    bid_levels = [(float(p), q) for p, q in bids.items()]
    ask_levels = [(float(p), q) for p, q in asks.items()]
    for band in bands_bps:
        lo = mid * (1 - band / 10_000)
        hi = mid * (1 + band / 10_000)
        bid_qty = sum(q for p, q in bid_levels if p >= lo)
        ask_qty = sum(q for p, q in ask_levels if p <= hi)
        total = bid_qty + ask_qty
        out[f"bid_qty_{band}bps"] = bid_qty
        out[f"ask_qty_{band}bps"] = ask_qty
        out[f"imb_{band}bps"] = (bid_qty - ask_qty) / total if total > 0 else None
    return out


class OrderBookCollector:
    def __init__(self, settings: Settings, pool, hb: Heartbeat, session: aiohttp.ClientSession):
        self.settings = settings
        self.pool = pool
        self.hb = hb
        self.rest = RestClient(session, settings.spot_rest_base)
        self.books = {sym: OrderBook(sym) for sym in settings.symbols}
        self._sync_tasks: dict[str, asyncio.Task] = {}

    async def on_message(self, _stream: str, event: dict) -> None:
        book = self.books.get(event["s"])
        if book is None:
            return
        if book.live:
            if book.apply(event):
                self.hb.bump("depth_events")
            else:
                self.trigger_resync(book, "sequence gap")
        else:
            book.buffer.append(event)
            if len(book.buffer) > MAX_BUFFER:
                del book.buffer[: len(book.buffer) - MAX_BUFFER]

    async def on_connect(self) -> None:
        # Fresh websocket -> diff continuity is broken for every symbol.
        for i, book in enumerate(self.books.values()):
            book.reset()
            # Stagger to spread REST snapshot weight.
            asyncio.get_running_loop().call_later(
                i * 0.25, self.trigger_resync, book, "websocket (re)connect"
            )

    def trigger_resync(self, book: OrderBook, reason: str) -> None:
        book.live = False
        existing = self._sync_tasks.get(book.symbol)
        if existing is not None and not existing.done():
            return
        log.info("[%s] resync: %s", book.symbol, reason)
        self._sync_tasks[book.symbol] = asyncio.create_task(self._resync(book))

    async def _resync(self, book: OrderBook) -> None:
        while True:
            try:
                snap = await self.rest.get_json(
                    "/api/v3/depth",
                    {"symbol": book.symbol, "limit": self.settings.depth_snapshot_limit},
                )
            except Exception as e:
                log.warning("[%s] snapshot fetch failed: %s, retrying", book.symbol, e)
                await asyncio.sleep(5)
                continue
            book.load_snapshot(snap["lastUpdateId"], snap["bids"], snap["asks"])
            # drain_buffer + the live flag happen in one synchronous step, so no
            # diff event can interleave between them.
            if book.drain_buffer():
                book.live = True
                self.hb.bump("resyncs")
                log.info("[%s] book live (lastUpdateId=%d, %d bids / %d asks)",
                         book.symbol, book.last_update_id, len(book.bids), len(book.asks))
                return
            # Buffered diffs start after the snapshot -> snapshot too old, refetch.
            await asyncio.sleep(0.5)

    async def rollup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.rollup_interval_s)
            ts = datetime.now(timezone.utc)
            rows = []
            for book in self.books.values():
                if not book.live:
                    continue
                r = compute_rollup(book.bids, book.asks)
                if r is None:
                    self.trigger_resync(book, "empty or crossed book")
                    continue
                rows.append((
                    book.symbol, ts, r["best_bid"], r["best_ask"], r["mid"], r["spread_bps"],
                    *(v for band in BANDS_BPS for v in
                      (r[f"bid_qty_{band}bps"], r[f"ask_qty_{band}bps"], r[f"imb_{band}bps"])),
                ))
            if rows:
                try:
                    await self.pool.executemany(INSERT_ROLLUP, rows)
                    self.hb.bump("rollups", len(rows))
                except Exception:
                    log.exception("rollup insert failed")

    async def snapshot_loop(self) -> None:
        n_levels = self.settings.snapshot_levels
        while True:
            await asyncio.sleep(self.settings.snapshot_interval_s)
            ts = datetime.now(timezone.utc)
            rows = []
            for book in self.books.values():
                if not book.live:
                    continue
                self._prune(book)
                bids = sorted(book.bids.items(), key=lambda kv: float(kv[0]), reverse=True)[:n_levels]
                asks = sorted(book.asks.items(), key=lambda kv: float(kv[0]))[:n_levels]
                rows.append((book.symbol, ts, book.last_update_id,
                             json.dumps(bids), json.dumps(asks)))
            if rows:
                try:
                    await self.pool.executemany(INSERT_SNAPSHOT, rows)
                    self.hb.bump("snapshots", len(rows))
                except Exception:
                    log.exception("snapshot insert failed")

    @staticmethod
    def _prune(book: OrderBook, max_distance: float = 0.10) -> None:
        """Bound memory: drop levels >10% from mid. Far outside anything we
        persist; the daily websocket reconnect resyncs the book anyway."""
        r = compute_rollup(book.bids, book.asks, bands_bps=())
        if r is None:
            return
        mid = r["mid"]
        lo, hi = mid * (1 - max_distance), mid * (1 + max_distance)
        for price in [p for p in book.bids if float(p) < lo]:
            del book.bids[price]
        for price in [p for p in book.asks if float(p) > hi]:
            del book.asks[price]


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    hb = Heartbeat(pool, "orderbook", settings.heartbeat_interval_s)
    streams = [f"{sym.lower()}@depth@100ms" for sym in settings.symbols]
    try:
        async with aiohttp.ClientSession() as session:
            collector = OrderBookCollector(settings, pool, hb, session)
            log.info("collecting order books for %d symbols", len(settings.symbols))
            async with asyncio.TaskGroup() as tg:
                tg.create_task(hb.run())
                tg.create_task(collector.rollup_loop())
                tg.create_task(collector.snapshot_loop())
                tg.create_task(run_combined_stream(
                    settings.spot_ws_base, streams, collector.on_message,
                    name="orderbook", on_connect=collector.on_connect,
                ))
    finally:
        await pool.close()
