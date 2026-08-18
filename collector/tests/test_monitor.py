from gs_common.alerts import Alerter
from gs_common.config import load_settings
from gs_collector.monitor import quality_report


class FakePool:
    """In-memory stand-in for the asyncpg pool calls quality_report makes."""

    def __init__(self, kline_rows, extra_rows, funding_age_hours):
        self.kline_rows = kline_rows
        self.extra_rows = extra_rows  # table name -> list of {"symbol": ..., "n": ...}
        self.funding_age_hours = funding_age_hours

    async def fetch(self, sql, *_args):
        if "FROM klines" in sql:
            return self.kline_rows
        for table, rows in self.extra_rows.items():
            if f"FROM {table}" in sql:
                return rows
        return []

    async def fetchval(self, _sql, *_args):
        return self.funding_age_hours


def make_settings(symbols=("BTCUSDT",), intervals=("1m", "5m", "15m", "1h")):
    settings = load_settings()
    return settings.model_copy(update={"symbols": list(symbols), "kline_intervals": list(intervals)})


def full_coverage_extras(settings):
    """book_rollups/snapshots/aggtrades at exactly their (adjusted) expected count."""
    rollups_per_day = int(86_400 / settings.rollup_interval_s)
    snapshots_per_day = int(86_400 / settings.snapshot_interval_s)
    return {
        "book_rollups": [{"symbol": "BTCUSDT", "n": rollups_per_day}],
        "book_snapshots": [{"symbol": "BTCUSDT", "n": snapshots_per_day}],
        "agg_trades_1m": [{"symbol": "BTCUSDT", "n": 1440 - 1}],
    }


async def test_in_progress_candle_is_not_a_false_positive():
    """Exactly one candle short per interval — the currently-forming one that
    cannot possibly be closed yet — must NOT be reported as a gap."""
    settings = make_settings()
    kline_rows = [
        {"symbol": "BTCUSDT", "interval": "1m", "n": 1439},
        {"symbol": "BTCUSDT", "interval": "5m", "n": 287},
        {"symbol": "BTCUSDT", "interval": "15m", "n": 95},
        {"symbol": "BTCUSDT", "interval": "1h", "n": 23},
    ]
    pool = FakePool(kline_rows, full_coverage_extras(settings), funding_age_hours=1.0)
    report = await quality_report(pool, settings, Alerter(settings))
    assert "⚠️" not in report
    assert "✅ all series ≥99% coverage" in report


async def test_real_gap_is_still_flagged():
    settings = make_settings()
    kline_rows = [
        {"symbol": "BTCUSDT", "interval": "1m", "n": 1439},
        {"symbol": "BTCUSDT", "interval": "5m", "n": 287},
        {"symbol": "BTCUSDT", "interval": "15m", "n": 50},  # genuinely missing candles
        {"symbol": "BTCUSDT", "interval": "1h", "n": 23},
    ]
    pool = FakePool(kline_rows, full_coverage_extras(settings), funding_age_hours=1.0)
    report = await quality_report(pool, settings, Alerter(settings))
    assert "klines BTCUSDT 15m: 50/95" in report


async def test_aggtrade_expected_count_accounts_for_in_progress_minute():
    """The displayed denominator should be 1439 (adjusted), not the naive 1440 —
    otherwise a fully-healthy stack would show as short by exactly 1 forever."""
    settings = make_settings(intervals=("1m",))
    kline_rows = [{"symbol": "BTCUSDT", "interval": "1m", "n": 1439}]
    pool = FakePool(kline_rows, full_coverage_extras(settings), funding_age_hours=1.0)
    report = await quality_report(pool, settings, Alerter(settings))
    assert "aggtrade minutes: 1,439/1,439" in report
    assert "✅ all series ≥99% coverage" in report
