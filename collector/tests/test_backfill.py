from datetime import date, datetime, timezone

from gs_collector.backfill import (
    complete_months,
    normalize_ts_ms,
    parse_vision_csv,
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
