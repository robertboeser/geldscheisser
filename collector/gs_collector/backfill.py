"""Historical kline backfill.

Strategy per (symbol, interval): bulk-load complete months from the public
data dumps at data.binance.vision (fast, no API weight), then fill the current
month — and any recent month whose dump is not published yet — via the REST API.

**Gap-filling, not append-only.** The month-by-month completeness check below is
what makes this usable against a database that already holds live data. The earlier
version resumed from `MAX(open_time)`, which meant that once the live collector
was running the resume point was always "now": `complete_months(today, today)`
is empty and the REST cursor starts at the present, so a re-run fetched nothing
at all and still logged success. Any history missing *below* the newest row —
which is all history, on a database whose old chunks were dropped by a retention
policy — was unreachable. Now every month in the window is checked against its
expected candle count and re-fetched if short, so backward fill and mid-history
holes are both handled, and a fully-populated database re-runs as a cheap no-op.

Because of that, the natural target is the **archive** (unbounded), not the
collector (90-day retention, which would delete the history again — and the
archive's forward-only sync could never pull rows older than its watermark):

    GS_DATABASE_URL=postgresql://gs:gs@gs-archive:5433/geldscheisser gs backfill

Note: monthly dump files from 2025-01 onward use microsecond timestamps;
`normalize_ts_ms` handles both generations.
"""

import asyncio
import calendar
import csv
import io
import logging
import zipfile
from datetime import date, datetime, timezone

import aiohttp

from gs_common.config import INTERVAL_MS, Settings, load_settings
from gs_common.db import create_pool, ms_to_dt
from gs_common.log import setup_logging
from gs_common.rest import RestClient

log = logging.getLogger(__name__)

COLUMNS = (
    "symbol", "interval", "open_time", "open", "high", "low", "close",
    "volume", "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume",
)

# Rows per COPY batch. Much larger than the old executemany chunk: staging via
# COPY makes the per-batch cost dominated by the round trip, not the row count.
CHUNK = 50_000

# How far back to chase a missing month over REST. Binance publishes a monthly
# dump a few days into the following month, so the most recent complete month
# can legitimately 404 — that must not leave a hole. Older shortfalls are
# reported instead: they are usually real exchange gaps, and retrying them on
# every run would cost thousands of REST calls forever.
REST_BACKSTOP_MONTHS = 2


def normalize_ts_ms(ts: int) -> int:
    """Binance vision dumps switched to microsecond timestamps in 2025."""
    return ts // 1000 if ts > 10**14 else ts


def complete_months(start: date, today: date) -> list[tuple[int, int]]:
    """(year, month) pairs from `start` up to but excluding the current month."""
    months = []
    y, m = start.year, start.month
    while (y, m) < (today.year, today.month):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return months


def month_bounds_ms(y: int, m: int) -> tuple[int, int]:
    """[start, end) of a month in epoch milliseconds, UTC."""
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def expected_rows(y: int, m: int, interval: str) -> int:
    """Candles a complete month holds — the yardstick for "already backfilled"."""
    days = calendar.monthrange(y, m)[1]
    return days * 86_400_000 // INTERVAL_MS[interval]


def months_to_fetch(
    start: date, today: date, have: dict[tuple[int, int], int], interval: str
) -> list[tuple[int, int]]:
    """Complete months in the window that are absent or short in the database."""
    return [
        (y, m) for y, m in complete_months(start, today)
        if have.get((y, m), 0) < expected_rows(y, m, interval)
    ]


def last_closed_open_ms(as_of_ms: int, interval: str) -> int:
    """Open time of the newest candle already closed at `as_of_ms`.

    Eligibility must be a function of the run-start instant, never of the wall
    clock at the moment a given pair happens to be fetched. Otherwise a long run
    admits a candle for the pairs it reaches after that candle closes and drops
    it for the ones it reached earlier — the same staircase the cap exists to
    prevent, just one candle wide.
    """
    iv = INTERVAL_MS[interval]
    return (as_of_ms // iv) * iv - iv


def recent_months(months: list[tuple[int, int]], today: date, n: int) -> list[tuple[int, int]]:
    """The subset of `months` falling within the last `n` complete months."""
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return [ym for ym in months if ym >= (y, m)]


def parse_vision_csv(raw: bytes, symbol: str, interval: str) -> list[tuple]:
    records = []
    for row in csv.reader(io.StringIO(raw.decode())):
        if not row or not row[0].strip().isdigit():  # header line in newer files
            continue
        records.append((
            symbol, interval, ms_to_dt(normalize_ts_ms(int(row[0]))),
            float(row[1]), float(row[2]), float(row[3]), float(row[4]),
            float(row[5]), float(row[7]), int(row[8]), float(row[9]), float(row[10]),
        ))
    return records


async def upsert_chunks(conn, records: list[tuple]) -> int:
    """COPY into a temp staging table, then INSERT .. SELECT .. ON CONFLICT.

    asyncpg's COPY cannot express ON CONFLICT, and plain executemany measured
    ~850 rows/s over the tailnet to the Pi — hours for a multi-year backfill.
    Staging keeps COPY's speed while preserving idempotent re-runs.

    Returns rows actually inserted, read from the INSERT command tag — not rows
    fetched. Duplicates are the normal case on a re-run and must not be counted
    as progress; a backfill that reports work it did not do is the failure mode
    this module exists to prevent.
    """
    inserted = 0
    for i in range(0, len(records), CHUNK):
        batch = records[i:i + CHUNK]
        async with conn.transaction():
            await conn.execute(
                "CREATE TEMP TABLE kline_stage (LIKE klines) ON COMMIT DROP"
            )
            await conn.copy_records_to_table("kline_stage", records=batch, columns=COLUMNS)
            tag = await conn.execute(
                f"INSERT INTO klines ({', '.join(COLUMNS)}) "
                f"SELECT {', '.join(COLUMNS)} FROM kline_stage "
                f"ON CONFLICT (symbol, interval, open_time) DO NOTHING"
            )
            inserted += int(tag.rsplit(" ", 1)[-1])  # "INSERT 0 <count>"
    return inserted


async def month_counts(conn, symbol: str, interval: str) -> dict[tuple[int, int], int]:
    rows = await conn.fetch(
        """SELECT date_trunc('month', open_time) AS m, count(*) AS n
           FROM klines WHERE symbol = $1 AND interval = $2 GROUP BY 1""",
        symbol, interval,
    )
    return {(r["m"].year, r["m"].month): r["n"] for r in rows}


async def fetch_range_rest(
    rest: RestClient, symbol: str, interval: str, start_ms: int, end_ms: int,
    as_of_ms: int,
) -> list[tuple]:
    """Candles in [start_ms, end_ms) closed as of `as_of_ms`, paged at 1000."""
    interval_ms = INTERVAL_MS[interval]
    cursor, out = start_ms, []
    while cursor < end_ms:
        rows = await rest.get_json("/api/v3/klines", {
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "endTime": end_ms - 1, "limit": 1000,
        })
        rows = [r for r in rows if int(r[6]) <= as_of_ms]  # closed as of run start
        if not rows:
            break
        out.extend(
            (symbol, interval, ms_to_dt(int(r[0])),
             float(r[1]), float(r[2]), float(r[3]), float(r[4]),
             float(r[5]), float(r[7]), int(r[8]), float(r[9]), float(r[10]))
            for r in rows
        )
        cursor = int(rows[-1][0]) + interval_ms
    return out


async def backfill_pair(
    conn, rest: RestClient, session: aiohttp.ClientSession, settings: Settings,
    symbol: str, interval: str, as_of: datetime,
) -> int:
    """Fill history for one pair. `as_of` bounds how *recent* a row may be.

    The archive's sync resumes from a single `(open_time, symbol, interval)`
    watermark, which is only lossless while the archive is a prefix of the
    collector in that order — every row below the watermark must already be
    present. Writing rows *above* the watermark breaks that: anything the
    collector holds below the new maximum but above what this backfill happened
    to reach is then unreachable forever, because the sync only walks forward.

    That is easy to do by accident: pairs are walked sequentially, so without a
    cap each one's REST phase ends at a different "now" and leaves a staircase.
    `as_of` is the single run-start instant every pair is measured against, so
    each interval lands on exactly the same last candle regardless of how long
    the run takes or which pair is reached when."""
    # UTC, not date.today(): open_time and the dump filenames are UTC, so a local
    # date would name the wrong "current month" for hours around each boundary.
    today = datetime.now(timezone.utc).date()
    start = settings.backfill_start_for(interval)
    have = await month_counts(conn, symbol, interval)
    wanted = months_to_fetch(start, today, have, interval)
    as_of_ms = int(as_of.timestamp() * 1000)
    cap_ms = last_closed_open_ms(as_of_ms, interval)  # newest open_time we may write
    cap_dt = ms_to_dt(cap_ms)
    wanted = [ym for ym in wanted if month_bounds_ms(*ym)[0] <= cap_ms]
    total = 0

    def under_cap(records: list[tuple]) -> list[tuple]:
        return [r for r in records if r[2] <= cap_dt]

    # Phase 1: complete months from the public dumps.
    missing_dumps = []
    for y, m in wanted:
        url = (f"{settings.vision_base}/data/spot/monthly/klines/"
               f"{symbol}/{interval}/{symbol}-{interval}-{y:04d}-{m:02d}.zip")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status == 404:  # symbol not listed yet, or dump not published yet
                missing_dumps.append((y, m))
                continue
            resp.raise_for_status()
            payload = await resp.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            raw = zf.read(zf.namelist()[0])
        records = under_cap(parse_vision_csv(raw, symbol, interval))
        n = await upsert_chunks(conn, records)
        total += n
        log.info("%s %s %04d-%02d: %d new candles from dump (%d in file)",
                 symbol, interval, y, m, n, len(records))
        await asyncio.sleep(0.1)

    # Phase 2: REST for the current month, plus recent months whose dump is not
    # published yet. Always re-walks the current month from the 1st rather than
    # from max(open_time): on a database where live collection began mid-month,
    # topping up from the newest row would leave the start of the month empty.
    rest_months = recent_months(missing_dumps, today, REST_BACKSTOP_MONTHS)
    if (start.year, start.month) <= (today.year, today.month):
        rest_months.append((today.year, today.month))

    rest_total = 0
    for y, m in rest_months:
        lo, hi = month_bounds_ms(y, m)
        hi = min(hi, cap_ms + 1)
        if lo >= hi:
            continue
        records = await fetch_range_rest(rest, symbol, interval, lo, hi, as_of_ms)
        if records:
            rest_total += await upsert_chunks(conn, records)
    if rest_total:
        log.info("%s %s: %d new candles via REST", symbol, interval, rest_total)

    stale = [ym for ym in missing_dumps if ym not in set(rest_months)]
    if stale:
        log.warning(
            "%s %s: %d month(s) still short and older than the REST backstop "
            "(oldest %04d-%02d) — likely pre-listing or a real exchange gap",
            symbol, interval, len(stale), *stale[0],
        )
    return total + rest_total


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    # Never redefine the target's retention policy — see create_pool.
    pool = await create_pool(settings, manage_retention=False)
    grand_total = 0
    try:
        async with aiohttp.ClientSession() as session, pool.acquire() as conn:
            rest = RestClient(session, settings.spot_rest_base)
            # One instant for the whole run, so every pair stops on the same
            # candle and each interval's maximum stays flat — see backfill_pair.
            as_of = datetime.now(timezone.utc)
            log.info("as of %s: per-interval caps %s", as_of, {
                iv: str(ms_to_dt(last_closed_open_ms(int(as_of.timestamp() * 1000), iv)))
                for iv in settings.kline_intervals})
            for symbol in settings.symbols:
                for interval in settings.kline_intervals:
                    n = await backfill_pair(conn, rest, session, settings,
                                            symbol, interval, as_of)
                    grand_total += n
                    log.info("done: %s %s (+%d candles)", symbol, interval, n)

    finally:
        await pool.close()
    log.info("backfill complete: %d candles inserted", grand_total)
