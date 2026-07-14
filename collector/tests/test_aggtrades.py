from gs_collector.aggtrades import MinuteBuckets


def test_bucketing_and_flush():
    b = MinuteBuckets()
    t0 = 1_700_000_000_000  # arbitrary ms inside some minute
    minute0 = t0 - t0 % 60_000
    b.add("BTCUSDT", t0, price=100.0, qty=2.0, buyer_is_maker=False)   # taker buy
    b.add("BTCUSDT", t0 + 1000, price=100.0, qty=1.0, buyer_is_maker=True)  # taker sell
    b.add("BTCUSDT", minute0 + 60_000, price=101.0, qty=3.0, buyer_is_maker=False)  # next minute

    # Only the first minute has fully elapsed.
    rows = b.pop_completed(minute0 + 90_000)
    assert len(rows) == 1
    sym, minute, buy_vol, sell_vol, buy_quote, sell_quote, trades = rows[0]
    assert sym == "BTCUSDT"
    assert buy_vol == 2.0 and sell_vol == 1.0
    assert buy_quote == 200.0 and sell_quote == 100.0
    assert trades == 2
    # Second minute still buffered.
    assert len(b.buckets) == 1

    rows = b.pop_completed(minute0 + 3 * 60_000)
    assert len(rows) == 1 and rows[0][2] == 3.0
    assert b.buckets == {}
