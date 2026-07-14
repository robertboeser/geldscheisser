"""Operational alerting. Discord webhook if configured, always logged.

Matrix can be added later as a second implementation behind the same interface.
"""

import logging
import time

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession | None = None):
        self.webhook = settings.discord_webhook_url
        self.default_cooldown_s = settings.alert_cooldown_s
        self._session = session
        self._owns_session = session is None
        self._last_sent: dict[str, float] = {}

    async def _post(self, message: str) -> None:
        if not self.webhook:
            return
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                self.webhook, json={"content": message[:1900]},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status >= 400:
                    log.error("discord webhook returned %s: %s", resp.status, await resp.text())
        except Exception:
            log.exception("discord webhook post failed")

    async def alert(self, key: str, message: str, cooldown_s: float | None = None) -> None:
        """Rate-limited per key so a flapping collector doesn't spam the channel."""
        log.warning("ALERT [%s] %s", key, message)
        cooldown = self.default_cooldown_s if cooldown_s is None else cooldown_s
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < cooldown:
            return
        self._last_sent[key] = now
        await self._post(f"⚠️ **{key}**\n{message}")

    async def info(self, message: str) -> None:
        log.info("REPORT %s", message.replace("\n", " | "))
        await self._post(message)

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
