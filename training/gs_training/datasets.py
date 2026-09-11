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

FUNDING_COLUMNS = ("symbol", "funding_time", "rate")

# Numeric kline columns exposed on the panel. The first five are what the
# backtester marks to market; the rest are measurements the feature library
# already declares columns for. `load_panel` used to stop after "volume", which
# cost nothing visible — the features simply came out all-NaN and were dropped,
# and `quote_vol_z_168` silently became a duplicate of `vol_z_168` because the
# caller passed base volume into the quote-volume slot. `taker_buy_volume` is
# the expensive one to lose: aggressor-side flow is the only order-flow signal
# in this dataset with history back to 2021.
PANEL_COLUMNS = (
    "open", "high", "low", "close", "volume",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume",
)

DEFAULT_CACHE = Path("data/parquet")


def _path(cache_dir: Path, symbol: str, interval: str) -> Path:
    return Path(cache_dir) / f"klines_{symbol}_{interval}.parquet"


def _funding_path(cache_dir: Path, symbol: str) -> Path:
    return Path(cache_dir) / f"funding_{symbol}.parquet"


def _as_naive_utc(values) -> np.ndarray:
    """tz-aware datetimes → `datetime64[ns]`, matching the panel's bar grid.

    The object form carries no `.astype` and compares awkwardly, so every
    timestamp crossing out of Polars is normalised here rather than at each
    call site.
    """
    return np.array([t.replace(tzinfo=None) for t in values], dtype="datetime64[ns]")


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
    # datetime64, not an object array of tz-aware datetimes: downstream code
    # does arithmetic on these, and the object form silently has no .astype.
    bar_times = _as_naive_utc(sorted(common))

    panel: dict[str, dict[str, np.ndarray]] = {}
    for s, df in frames.items():
        sub = df.filter(pl.col("open_time").is_in(list(common))).sort("open_time")
        panel[s] = {c: sub[c].to_numpy().astype(float) for c in PANEL_COLUMNS}
    return bar_times, panel


async def snapshot_funding(
    database_url: str,
    symbols: list[str],
    cache_dir: Path = DEFAULT_CACHE,
) -> dict[str, int]:
    """Pull perpetual funding rates per symbol into Parquet.

    Worth having precisely because it is *not* like the order book: funding
    prints every 8h and the archive holds it back to 2021-01-01 for every
    symbol, so it can join the model at full history instead of waiting years
    to accumulate. It is also information of a different kind from anything
    else here — every other feature is a transform of price and volume, while
    funding is what leveraged traders are paying to hold their side.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(database_url)
    written: dict[str, int] = {}
    try:
        for symbol in symbols:
            rows = await conn.fetch(
                f"SELECT {', '.join(FUNDING_COLUMNS)} FROM funding_rates "
                "WHERE symbol = $1 ORDER BY funding_time",
                symbol,
            )
            df = pl.DataFrame(
                {c: [r[c] for r in rows] for c in FUNDING_COLUMNS},
                schema_overrides={"funding_time": pl.Datetime(time_zone="UTC")},
            )
            df.write_parquet(_funding_path(cache_dir, symbol))
            written[symbol] = df.height
    finally:
        await conn.close()
    return written


def load_funding(symbol: str, cache_dir: Path = DEFAULT_CACHE) -> pl.DataFrame:
    path = _funding_path(Path(cache_dir), symbol)
    if not path.exists():
        raise FileNotFoundError(f"no funding snapshot at {path} — run snapshot_funding first")
    return pl.read_parquet(path).sort("funding_time")


def align_funding(
    bar_times: np.ndarray,
    symbols: list[str],
    cache_dir: Path = DEFAULT_CACHE,
) -> dict[str, np.ndarray] | None:
    """Funding rate in force at each bar, as a causal step function.

    Funding prints 8-hourly onto a 1h grid, so each bar carries the last rate
    published *at or before* its own `open_time`. Taking the next one instead
    would be a lookahead of up to eight hours that no live system could have —
    and it would look like signal, because funding is highest exactly when the
    crowd is most extended.

    Returns None when any symbol lacks a snapshot, so a missing funding file
    degrades to NaN features (which `usable_mask` drops as a column) rather
    than to an exception or, worse, a silently short universe.
    """
    out: dict[str, np.ndarray] = {}
    for symbol in symbols:
        path = _funding_path(Path(cache_dir), symbol)
        if not path.exists():
            return None
        df = load_funding(symbol, cache_dir)
        if df.height == 0:
            out[symbol] = np.full(bar_times.size, np.nan)
            continue
        ft = _as_naive_utc(df["funding_time"].to_list())
        rate = df["rate"].to_numpy().astype(float)
        # side="right": a rate stamped exactly at the bar open is already known.
        idx = np.searchsorted(ft, bar_times, side="right") - 1
        out[symbol] = np.where(idx >= 0, rate[np.clip(idx, 0, None)], np.nan)
    return out


async def _main() -> None:
    import argparse

    from gs_common.config import load_settings

    settings = load_settings()
    p = argparse.ArgumentParser(prog="gs-training-datasets",
                                description="Snapshot archive klines and funding to Parquet")
    p.add_argument("--interval", default="1h")
    p.add_argument("--symbols", default=",".join(settings.symbols))
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    p.add_argument("--database-url", default=settings.database_url)
    p.add_argument("--skip-funding", action="store_true",
                   help="klines only; funding features then come out NaN")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    written = await snapshot_klines(
        args.database_url, symbols, args.interval, Path(args.cache_dir),
    )
    print("klines:")
    for s, n in written.items():
        print(f"  {s:10} {n:>8,}")
    total = sum(written.values())

    if not args.skip_funding:
        funding = await snapshot_funding(args.database_url, symbols, Path(args.cache_dir))
        print("funding:")
        for s, n in funding.items():
            print(f"  {s:10} {n:>8,}")
        total += sum(funding.values())

    print(f"wrote {total:,} rows to {args.cache_dir}")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
