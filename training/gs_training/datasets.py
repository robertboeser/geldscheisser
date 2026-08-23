"""Archive Postgres → local Parquet snapshots, and loading them for research.

Two reasons this is a snapshot layer rather than live queries. The archive is a
Raspberry Pi behind a USB2 SSD, so repeated full scans are slow and rude; and an
experiment that re-queries a moving database is not reproducible — the same
notebook run twice would silently train on different data.

**Read-only by construction.** This connects with `asyncpg` directly instead of
`gs_common.create_pool`, which applies schema DDL on connect. The research layer
has no business creating hypertables or touching policies on the archive of
record; if a query here fails because something is missing, that is information,
not a thing to fix by mutating the source.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import asyncpg
import numpy as np
import polars as pl

KLINE_COLUMNS = (
    "symbol", "interval", "open_time", "open", "high", "low", "close",
    "volume", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume",
)

DEFAULT_CACHE = Path("data/parquet")


def _path(cache_dir: Path, symbol: str, interval: str) -> Path:
    return Path(cache_dir) / f"klines_{symbol}_{interval}.parquet"


async def snapshot_klines(
    database_url: str,
    symbols: list[str],
    interval: str,
    cache_dir: Path = DEFAULT_CACHE,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, int]:
    """Pull klines per symbol into Parquet. Overwrites; the archive is the source."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(database_url)
    written: dict[str, int] = {}
    try:
        for symbol in symbols:
            sql = [f"SELECT {', '.join(KLINE_COLUMNS)} FROM klines",
                   "WHERE symbol = $1 AND interval = $2"]
            args: list = [symbol, interval]
            if start is not None:
                args.append(start)
                sql.append(f"AND open_time >= ${len(args)}")
            if end is not None:
                args.append(end)
                sql.append(f"AND open_time <= ${len(args)}")
            sql.append("ORDER BY open_time")
            rows = await conn.fetch(" ".join(sql), *args)
            df = pl.DataFrame(
                {c: [r[c] for r in rows] for c in KLINE_COLUMNS},
                schema_overrides={"open_time": pl.Datetime(time_zone="UTC")},
            )
            df.write_parquet(_path(cache_dir, symbol, interval))
            written[symbol] = df.height
    finally:
        await conn.close()
    return written


def load_klines(symbol: str, interval: str, cache_dir: Path = DEFAULT_CACHE) -> pl.DataFrame:
    path = _path(Path(cache_dir), symbol, interval)
    if not path.exists():
        raise FileNotFoundError(f"no snapshot at {path} — run snapshot_klines first")
    return pl.read_parquet(path).sort("open_time")


def load_panel(
    symbols: list[str], interval: str, cache_dir: Path = DEFAULT_CACHE
) -> tuple[np.ndarray, dict[str, dict[str, np.ndarray]]]:
    """Align symbols onto one bar grid for the backtester.

    Returns `(bar_times, {symbol: {open/high/low/close/volume: array}})`, every
    array the same length, index `i` the same instant for every symbol.

    Alignment is an **inner join** on `open_time`: only bars present for every
    symbol survive. Forward-filling a missing bar would invent prices, and the
    backtester marks to market on every bar, so invented prices become invented
    equity. The cost is small in practice — the gaps in this dataset are Binance
    outages, which hit all symbols at once anyway.
    """
    if not symbols:
        raise ValueError("need at least one symbol")
    frames = {s: load_klines(s, interval, cache_dir) for s in symbols}
    common: set | None = None
    for df in frames.values():
        ts = set(df["open_time"].to_list())
        common = ts if common is None else (common & ts)
    if not common:
        raise ValueError("symbols share no common bars")
    bar_times = np.array(sorted(common))

    panel: dict[str, dict[str, np.ndarray]] = {}
    for s, df in frames.items():
        sub = df.filter(pl.col("open_time").is_in(list(common))).sort("open_time")
        panel[s] = {c: sub[c].to_numpy().astype(float)
                    for c in ("open", "high", "low", "close", "volume")}
    return bar_times, panel


async def _main() -> None:
    import argparse

    from gs_common.config import load_settings

    settings = load_settings()
    p = argparse.ArgumentParser(prog="gs-training-datasets",
                                description="Snapshot archive klines to Parquet")
    p.add_argument("--interval", default="1h")
    p.add_argument("--symbols", default=",".join(settings.symbols))
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    p.add_argument("--database-url", default=settings.database_url)
    args = p.parse_args()

    written = await snapshot_klines(
        args.database_url, [s.strip() for s in args.symbols.split(",")],
        args.interval, Path(args.cache_dir),
    )
    total = sum(written.values())
    for s, n in written.items():
        print(f"  {s:10} {n:>8,}")
    print(f"wrote {total:,} rows to {args.cache_dir}")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
