from gs_collector.orderbook import OrderBook, compute_rollup


def diff(U, u, bids=(), asks=()):
    return {"U": U, "u": u, "b": [list(x) for x in bids], "a": [list(x) for x in asks]}


def make_book():
    book = OrderBook("BTCUSDT")
    book.load_snapshot(
        100,
        bids=[["100.0", "2.0"], ["99.5", "5.0"], ["98.0", "1.0"]],
        asks=[["100.5", "3.0"], ["101.0", "4.0"], ["103.0", "2.0"]],
    )
    return book


class TestSyncRules:
    def test_event_before_snapshot_is_skipped(self):
        book = make_book()
        assert book.apply(diff(90, 100, bids=[("100.0", "9.9")])) is True
        assert book.bids["100.0"] == 2.0  # unchanged

    def test_first_event_spanning_snapshot_applies(self):
        book = make_book()
        assert book.apply(diff(95, 105, bids=[("100.0", "7.0")])) is True
        assert book.bids["100.0"] == 7.0
        assert book.last_update_id == 105

    def test_first_event_with_gap_fails(self):
        book = make_book()
        assert book.apply(diff(102, 110)) is False

    def test_continuation_must_be_contiguous(self):
        book = make_book()
        assert book.apply(diff(95, 105)) is True
        assert book.apply(diff(106, 110)) is True
        assert book.apply(diff(112, 115)) is False  # 111 missing

    def test_duplicate_event_is_skipped(self):
        book = make_book()
        assert book.apply(diff(95, 105)) is True
        assert book.apply(diff(101, 105)) is True  # replay of same range
        assert book.last_update_id == 105

    def test_zero_qty_removes_level(self):
        book = make_book()
        book.apply(diff(95, 105, bids=[("99.5", "0.0")], asks=[("101.0", "0.0")]))
        assert "99.5" not in book.bids
        assert "101.0" not in book.asks

    def test_drain_buffer_success_and_refetch(self):
        book = make_book()
        book.buffer = [diff(95, 105, bids=[("100.0", "8.0")]), diff(106, 110)]
        assert book.drain_buffer() is True
        assert book.bids["100.0"] == 8.0
        assert book.buffer == []

        stale = make_book()  # snapshot at 100, buffer starts at 150 -> gap
        stale.buffer = [diff(150, 160)]
        assert stale.drain_buffer() is False


class TestRollup:
    def test_basic_features(self):
        r = compute_rollup(
            bids={"100.0": 2.0, "99.95": 5.0, "99.0": 10.0},
            asks={"100.1": 3.0, "100.2": 4.0, "102.5": 20.0},
        )
        assert r["best_bid"] == 100.0
        assert r["best_ask"] == 100.1
        mid = 100.05
        assert abs(r["mid"] - mid) < 1e-9
        assert abs(r["spread_bps"] - (0.1 / mid * 10_000)) < 1e-6
        # 10bps band: [99.95, 100.15] around mid
        assert r["bid_qty_10bps"] == 7.0  # 100.0 + 99.95
        assert r["ask_qty_10bps"] == 3.0  # 100.1 only
        assert abs(r["imb_10bps"] - (7.0 - 3.0) / 10.0) < 1e-9
        # 200bps band: [98.05, 102.05] — excludes 102.5 ask
        assert r["bid_qty_200bps"] == 17.0
        assert r["ask_qty_200bps"] == 7.0

    def test_crossed_or_empty_book_returns_none(self):
        assert compute_rollup({"101.0": 1.0}, {"100.0": 1.0}) is None
        assert compute_rollup({}, {"100.0": 1.0}) is None
