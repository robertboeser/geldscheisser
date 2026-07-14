"""Thin Binance REST client with weight-limit awareness and retries."""

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)


class RestClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base: str,
        *,
        weight_soft_limit: int = 4500,
        weight_header: str = "x-mbx-used-weight-1m",
    ):
        self.session = session
        self.base = base
        self.weight_soft_limit = weight_soft_limit
        self.weight_header = weight_header

    async def get_json(self, path: str, params: dict | None = None, retries: int = 5):
        url = f"{self.base}{path}"
        for attempt in range(retries):
            try:
                async with self.session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    used = resp.headers.get(self.weight_header)
                    if resp.status in (429, 418):
                        wait = float(resp.headers.get("Retry-After", "60"))
                        log.warning("rate limited on %s (HTTP %s), sleeping %.0fs", path, resp.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500:
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=resp.status
                        )
                    resp.raise_for_status()
                    data = await resp.json()
                    if used and int(used) > self.weight_soft_limit:
                        log.info("used weight %s > %d, backing off 20s", used, self.weight_soft_limit)
                        await asyncio.sleep(20)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if isinstance(e, aiohttp.ClientResponseError) and 400 <= e.status < 500:
                    raise  # not recoverable by retrying (e.g. invalid symbol)
                if attempt == retries - 1:
                    raise
                wait = min(2 ** attempt, 30)
                log.warning("GET %s failed (%s), retry in %ds", path, e.__class__.__name__, wait)
                await asyncio.sleep(wait)
        raise RuntimeError(f"GET {path}: retries exhausted")
