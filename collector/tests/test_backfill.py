from datetime import date, datetime, timezone

from gs_common.config import Settings

from gs_collector.backfill import (
    complete_months,
    expected_rows,
    month_bounds_ms,
    months_to_fetch,
    normalize_ts_ms,
    parse_vision_csv,
    recent_months,
)


def test_normalize_ts_ms():
    assert normalize_ts_ms(1_700_000_000_000) == 1_700_000_000_000  # ms stays ms
    assert normalize_ts_ms(1_735_689_600_000_000) == 1_735_689_600_000  # µs (2025+ dumps)


def test_complete_months_excludes_current():
    months = complete_months(date(2024, 11, 15), date(2025, 2, 10))
    assert months == [(2024, 11), (2024, 12), (2025, 1)]
    assert complete_months(date(2025, 2, 1), date(2025, 2, 10)) == []


def test_parse_vision_csv_with_and_without_header():
    body = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore\n"
        "1700000000000,37000.1,37100.0,36900.0,37050.5,120.5,1700000059999,"
        "4462850.2,5000,60.2,2230000.1,0\n"
    )
    records = parse_vision_csv(body.encode(), "BTCUSDT", "1m")
    assert len(records) == 1
    r = records[0]
    assert r[0] == "BTCUSDT" and r[1] == "1m"
    assert r[2] == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
    assert r[3] == 37000.1 and r[6] == 37050.5
    assert r[8] == 4462850.2  # quote_volume, column 7 in the csv
    assert r[9] == 5000

    no_header = parse_vision_csv(body.encode().split(b"\n", 1)[1], "BTCUSDT", "1m")
    assert no_header == records


def test_expected_rows_matches_month_length():
    assert expected_rows(2025, 1, "1h") == 31 * 24
    assert expected_rows(2025, 2, "1h") == 28 * 24
    assert expected_rows(2024, 2, "1h") == 29 * 24  # leap year
    assert expected_rows(2025, 1, "1m") == 31 * 1440
    assert expected_rows(2025, 1, "15m") == 31 * 96


def test_months_to_fetch_skips_only_complete_months():
    today = date(2026, 8, 23)
    have = {
        (2026, 5): expected_rows(2026, 5, "1h"),      # complete -> skip
        (2026, 6): expected_rows(2026, 6, "1h") - 3,  # short -> refetch (mid-history gap)
        (2026, 8): 100,                               # current month is never a dump
    }
    assert months_to_fetch(date(2026, 4, 1), today, have, "1h") == [
        (2026, 4), (2026, 6), (2026, 7),
    ]


def test_months_to_fetch_backfills_below_existing_data():
    """The regression that mattered: history *older* than the newest row.

    The old resume-from-max(open_time) logic returned nothing here, because the
    database's newest candle was already current."""
    today = date(2026, 8, 23)
    have = {(2026, m): expected_rows(2026, m, "1h") for m in (6, 7)}  # only recent data
    wanted = months_to_fetch(date(2026, 1, 1), today, have, "1h")
    assert wanted == [(2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5)]


def test_month_bounds_ms_utc_and_year_rollover():
    lo, hi = month_bounds_ms(2025, 1)
    assert lo == int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert hi == int(datetime(2025, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)
    _, dec_hi = month_bounds_ms(2025, 12)
    assert dec_hi == int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def test_recent_months_bounds_the_rest_backstop():
    today = date(2026, 8, 23)
    missing = [(2021, 3), (2026, 6), (2026, 7)]
    # An unpublished dump for a just-ended month gets chased over REST...
    assert recent_months(missing, today, 2) == [(2026, 6), (2026, 7)]
    # ...a five-year-old pre-listing month does not.
    assert recent_months([(2021, 3)], today, 2) == []


def test_backfill_start_for_interval_override():
    s = Settings(backfill_start=date(2021, 1, 1),
                 backfill_start_overrides="1m:2025-09-01,5m:2025-09-01")
    assert s.backfill_start_for("1h") == date(2021, 1, 1)   # default
    assert s.backfill_start_for("1m") == date(2025, 9, 1)   # override
    assert s.backfill_start_for("5m") == date(2025, 9, 1)
