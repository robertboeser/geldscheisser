"""Monitor service: heartbeat watchdog + daily data-quality report.

A dead collector must page you — a gap in order book data is unrecoverable.
The watchdog alerts on any heartbeat row that goes stale. The quality report
compares last-24h row counts against what a healthy stack would produce and
flags per-symbol coverage below threshold. Both go to the Discord webhook.
"""

import asyncio
import logging
from datetime import datetime, timezone

from gs_common.alerts import Alerter
from gs_common.config import INTERVAL_MS, Settings, load_settings
from gs_common.db import Heartbeat, create_pool
from gs_common.log import setup_logging

log = logging.getLogger(__name__)

COVERAGE_THRESHOLD = 0.99


async def check_heartbeats(pool, settings: Settings, alerter: Alerter) -> None:
    rows = await pool.fetch(
        """SELECT service, component, last_seen,
                  extract(epoch FROM now() - last_seen) AS age_s
           FROM heartbeats WHERE component = ''"""
    )
    for r in rows:
        if r["service"] == "monitor":
            continue
        if r["age_s"] > settings.heartbeat_stale_s:
            await alerter.alert(
                f"collector-stale:{r['service']}",
                f"`{r['service']}` last heartbeat {r['age_s'] / 60:.1f} min ago "
                f"({r['last_seen']:%Y-%m-%d %H:%M:%S} UTC). "
                "Order book gaps are unrecoverable — check the service.",
            )


async def quality_report(pool, settings: Settings, alerter: Alerter) -> str:
    lines = ["📊 **Data quality — last 24h**"]
    problems: list[str] = []

    kline_counts = {
        (r["symbol"], r["interval"]): r["n"]
        for r in await pool.fetch(
            """SELECT symbol, interval, count(*) AS n FROM klines
               WHERE open_time >= now() - interval '24 hours' GROUP BY 1, 2"""
        )
    }
    expected_total = actual_total = 0
    for sym in settings.symbols:
        for iv in settings.kline_intervals:
            # -1: the most recent candle in the window is still forming and can't
            # possibly be closed (hence not yet in the DB) at report time — not a gap.
            expected = 86_400_000 // INTERVAL_MS[iv] - 1
            actual = kline_counts.get((sym, iv), 0)
            expected_total += expected
            actual_total += actual
            if actual < expected * COVERAGE_THRESHOLD:
                problems.append(f"klines {sym} {iv}: {actual}/{expected}")
    lines.append(f"klines: {actual_total:,}/{expected_total:,}")

    for table, ts_col, per_day, label in (
        # book_rollups/snapshots are point-in-time inserts, not closed periods —
        # no "still forming" row to account for, so their full grid count is correct.
        ("book_rollups", "ts", int(86_400 / settings.rollup_interval_s), "book rollups"),
        ("book_snapshots", "ts", int(86_400 / settings.snapshot_interval_s), "book snapshots"),
        # -1: same reasoning as klines — the current minute's bucket is only
        # flushed once it fully elapses (see aggtrades.MinuteBuckets.pop_completed).
        ("agg_trades_1m", "minute", 1440 - 1, "aggtrade minutes"),
    ):
        counts = {
            r["symbol"]: r["n"]
            for r in await pool.fetch(
                f"""SELECT symbol, count(*) AS n FROM {table}
                    WHERE {ts_col} >= now() - interval '24 hours' GROUP BY 1"""
            )
        }
        total = sum(counts.get(s, 0) for s in settings.symbols)
        lines.append(f"{label}: {total:,}/{per_day * len(settings.symbols):,}")
        for sym in settings.symbols:
            actual = counts.get(sym, 0)
            if actual < per_day * COVERAGE_THRESHOLD:
                problems.append(f"{label} {sym}: {actual}/{per_day}")

    funding_age = await pool.fetchval(
        "SELECT extract(epoch FROM now() - max(funding_time)) / 3600 FROM funding_rates")
    lines.append("funding: none collected yet" if funding_age is None
                 else f"funding: newest {funding_age:.1f}h old")

    if problems:
        shown = problems[:15]
        lines.append(f"⚠️ {len(problems)} series below {COVERAGE_THRESHOLD:.0%} coverage:")
        lines.extend(f"- {p}" for p in shown)
        if len(problems) > len(shown):
            lines.append(f"- … and {len(problems) - len(shown)} more")
    else:
        lines.append("✅ all series ≥99% coverage")

    report = "\n".join(lines)
    await alerter.info(report)
    return report


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    alerter = Alerter(settings)
    hb = Heartbeat(pool, "monitor", settings.heartbeat_interval_s)

    async def watchdog_loop() -> None:
        while True:
            try:
                await check_heartbeats(pool, settings, alerter)
            except Exception:
                log.exception("heartbeat check failed")
            await asyncio.sleep(60)

    async def quality_loop() -> None:
        # Startup report only once data exists (avoids a wall of zeros on first boot).
        if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM klines)"):
            await quality_report(pool, settings, alerter)
        last_run_date = None
        while True:
            await asyncio.sleep(300)
            now = datetime.now(timezone.utc)
            if now.hour == settings.quality_report_hour_utc and now.date() != last_run_date:
                last_run_date = now.date()
                try:
                    await quality_report(pool, settings, alerter)
                except Exception:
                    log.exception("quality report failed")

    log.info("monitor running (stale threshold %.0fs, report at %02d:00 UTC)",
             settings.heartbeat_stale_s, settings.quality_report_hour_utc)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(hb.run())
            tg.create_task(watchdog_loop())
            tg.create_task(quality_loop())
    finally:
        await alerter.close()
        await pool.close()


async def quality_main() -> None:
    """One-shot `gs quality` for ad-hoc checks."""
    settings = load_settings()
    setup_logging(settings.log_level)
    pool = await create_pool(settings)
    alerter = Alerter(settings)
    try:
        report = await quality_report(pool, settings, alerter)
        print(report)
    finally:
        await alerter.close()
        await pool.close()
