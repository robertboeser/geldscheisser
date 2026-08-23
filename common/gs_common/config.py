"""Central settings. Everything is overridable via GS_* env vars or a .env file."""

from datetime import date
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GS_", env_file=".env", extra="ignore")

    symbols: Annotated[list[str], NoDecode] = [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT",
    ]
    kline_intervals: Annotated[list[str], NoDecode] = ["1m", "5m", "15m", "1h"]

    database_url: str = "postgresql://gs:gs@localhost:5433/geldscheisser"
    discord_webhook_url: str = ""
    log_level: str = "INFO"

    spot_rest_base: str = "https://api.binance.com"
    spot_ws_base: str = "wss://stream.binance.com:9443"
    futures_rest_base: str = "https://fapi.binance.com"
    vision_base: str = "https://data.binance.vision"

    # Order book collection
    depth_snapshot_limit: int = 1000   # levels fetched per REST snapshot for book sync
    snapshot_levels: int = 500         # levels persisted per side in book_snapshots
    rollup_interval_s: float = 10.0    # book_rollups cadence
    snapshot_interval_s: float = 300.0 # book_snapshots cadence

    # Backfill. `backfill_start` is the default earliest month to pull; the
    # override map lets fine intervals stop earlier, e.g.
    # GS_BACKFILL_START_OVERRIDES="1m:2025-09-01,5m:2025-09-01" — a 14-day swing
    # horizon does not need minute candles from 2021, and they dominate row count.
    backfill_start: date = date(2021, 1, 1)
    backfill_start_overrides: Annotated[dict[str, date], NoDecode] = {}

    # Data retention in days. 0 = keep everything. The collector normally runs
    # on the VPS with a rolling window (e.g. 90) — it only needs enough history
    # for live features; the archive (unbounded) is the permanent record.
    retention_days: int = 90

    # Health / monitoring
    heartbeat_interval_s: float = 30.0
    heartbeat_stale_s: float = 180.0
    quality_report_hour_utc: int = 6
    alert_cooldown_s: float = 1800.0

    # Archive (Pi): pulls new rows from the collector at source_database_url
    # into this machine's own (unbounded) database on a fixed interval.
    source_database_url: str = ""
    archive_sync_interval_s: float = 86_400.0  # 1 day
    archive_batch_size: int = 20_000
    # How far back before its watermark each sync restarts. Must exceed the
    # largest kline interval: a 1h candle is written a full hour after its
    # open_time, by which point 1m candles have pushed the watermark past it,
    # and a forward-only walk would never see it. Overlap is free (ON CONFLICT
    # DO NOTHING), so the only cost of a larger value is re-reading rows.
    archive_sync_lag_s: float = 7_200.0  # 2h

    @field_validator("symbols", "kline_intervals", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("backfill_start_overrides", mode="before")
    @classmethod
    def _parse_start_overrides(cls, v):
        """Parse `1m:2025-09-01,5m:2025-09-01` into {interval: date}."""
        if not isinstance(v, str):
            return v
        out = {}
        for item in v.split(","):
            item = item.strip()
            if not item:
                continue
            interval, _, day = item.partition(":")
            if not day:
                raise ValueError(f"expected 'interval:YYYY-MM-DD', got {item!r}")
            out[interval.strip()] = date.fromisoformat(day.strip())
        return out

    @field_validator("symbols")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.upper() for s in v]

    def backfill_start_for(self, interval: str) -> date:
        return self.backfill_start_overrides.get(interval, self.backfill_start)


def load_settings() -> Settings:
    return Settings()


INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
