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

    # Backfill
    backfill_start: date = date(2021, 1, 1)

    # Data retention in days. 0 = keep everything (the Pi, archive of record).
    # The execution VPS sets e.g. 90 — it only needs enough history to compute
    # live features, not the full archive.
    retention_days: int = 0

    # Health / monitoring
    heartbeat_interval_s: float = 30.0
    heartbeat_stale_s: float = 180.0
    quality_report_hour_utc: int = 6
    alert_cooldown_s: float = 1800.0

    @field_validator("symbols", "kline_intervals", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("symbols")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.upper() for s in v]


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
