"""Reconnecting consumer for Binance combined websocket streams.

Binance closes connections after 24h and on maintenance; the loop reconnects
with capped exponential backoff. `on_connect` fires after every (re)connect —
the order book collector uses it to resync its books.
"""

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, dict], Awaitable[None]]


async def run_combined_stream(
    ws_base: str,
    streams: list[str],
    on_message: MessageHandler,
    *,
    name: str,
    on_connect: Callable[[], Awaitable[None]] | None = None,
) -> None:
    url = f"{ws_base}/stream?streams={'/'.join(streams)}"
    backoff = 1.0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url, heartbeat=30, max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=10)
                ) as ws:
                    log.info("[%s] connected (%d streams)", name, len(streams))
                    backoff = 1.0
                    if on_connect is not None:
                        await on_connect()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            await on_message(payload["stream"], payload["data"])
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
                    log.warning("[%s] websocket closed by server", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[%s] websocket error: %s: %s", name, e.__class__.__name__, e)
        sleep_s = backoff + random.uniform(0, backoff / 2)
        log.info("[%s] reconnecting in %.1fs", name, sleep_s)
        await asyncio.sleep(sleep_s)
        backoff = min(backoff * 2, 60.0)
