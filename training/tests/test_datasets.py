"""Loader contracts: what reaches the model, and when it was knowable.

Both failures these guard against are silent. A panel that quietly stops
carrying a column does not raise — the feature comes out all-NaN and is
dropped, so the model simply trains on less than you think it does. And a
funding series aligned one step too generously does not raise either; it
improves the backtest, because funding peaks exactly when the crowd is most
extended.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from gs_training.datasets import (
    FUNDING_COLUMNS,
    KLINE_COLUMNS,
    PANEL_COLUMNS,
    align_funding,
    funding_settlements,
    load_panel,
)


def _write_klines(cache_dir, symbol, interval, n=40, seed=0):
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    volume = rng.lognormal(3, 0.3, n)
    df = pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "interval": [interval] * n,
            "open_time": [t0 + timedelta(hours=i) for i in range(n)],
            "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": volume,
            # Deliberately not a multiple of volume, so a mix-up is detectable.
            "quote_volume": volume * close,
            "trades": np.arange(1, n + 1),
            "taker_buy_volume": volume * 0.42,
            "taker_buy_quote_volume": volume * close * 0.42,
        },
        schema_overrides={"open_time": pl.Datetime(time_zone="UTC")},
    )
    assert set(df.columns) == set(KLINE_COLUMNS)
    df.write_parquet(cache_dir / f"klines_{symbol}_{interval}.parquet")
    return df


def _write_funding(cache_dir, symbol, times, rates):
    pl.DataFrame(
        {"symbol": [symbol] * len(times), "funding_time": list(times), "rate": list(rates)},
        schema_overrides={"funding_time": pl.Datetime(time_zone="UTC")},
    ).select(FUNDING_COLUMNS).write_parquet(cache_dir / f"funding_{symbol}.parquet")


class TestPanel:
    def test_panel_carries_every_declared_column(self, tmp_path):
        """The regression that motivated this file: the panel used to stop after
        'volume', so taker flow and real quote volume never reached the model."""
        _write_klines(tmp_path, "AAAUSDT", "1h")
        _, panel = load_panel(["AAAUSDT"], "1h", tmp_path)
        assert set(panel["AAAUSDT"]) == set(PANEL_COLUMNS)

    def test_quote_volume_is_not_base_volume(self, tmp_path):
        """`quote_vol_z_168` was a bit-identical copy of `vol_z_168` for a while
        because the caller passed base volume into the quote-volume slot."""
        _write_klines(tmp_path, "AAAUSDT", "1h")
        _, panel = load_panel(["AAAUSDT"], "1h", tmp_path)
        b = panel["AAAUSDT"]
        assert not np.allclose(b["quote_volume"], b["volume"])

    def test_taker_buy_volume_survives_the_inner_join(self, tmp_path):
        _write_klines(tmp_path, "AAAUSDT", "1h", n=40, seed=1)
        _write_klines(tmp_path, "BBBUSDT", "1h", n=30, seed=2)
        _, panel = load_panel(["AAAUSDT", "BBBUSDT"], "1h", tmp_path)
        for sym in ("AAAUSDT", "BBBUSDT"):
            tb, v = panel[sym]["taker_buy_volume"], panel[sym]["volume"]
            assert np.all(np.isfinite(tb))
            assert np.allclose(tb / v, 0.42)


class TestFundingAlignment:
    def _grid(self, n=24):
        t0 = np.datetime64("2024-01-01T00:00:00")
        return t0 + np.arange(n) * np.timedelta64(1, "h")

    def test_uses_the_last_rate_published_at_or_before_the_bar(self, tmp_path):
        _write_klines(tmp_path, "AAAUSDT", "1h")
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        _write_funding(tmp_path, "AAAUSDT",
                       [t0, t0 + timedelta(hours=8), t0 + timedelta(hours=16)],
                       [0.001, 0.002, 0.003])
        got = align_funding(self._grid(), ["AAAUSDT"], tmp_path)["AAAUSDT"]
        assert got[0] == pytest.approx(0.001)     # stamped exactly at the bar: known
        assert got[7] == pytest.approx(0.001)     # still the 00:00 print
        assert got[8] == pytest.approx(0.002)     # new print lands
        assert got[15] == pytest.approx(0.002)
        assert got[16] == pytest.approx(0.003)

    def test_never_reaches_forward_for_a_rate(self, tmp_path):
        """The whole point. Bars before the first print have no funding, and
        must be NaN rather than borrowing the first future value."""
        _write_klines(tmp_path, "AAAUSDT", "1h")
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        _write_funding(tmp_path, "AAAUSDT", [t0 + timedelta(hours=10)], [0.005])
        got = align_funding(self._grid(), ["AAAUSDT"], tmp_path)["AAAUSDT"]
        assert np.all(np.isnan(got[:10]))
        assert np.allclose(got[10:], 0.005)

    def test_future_rates_cannot_change_earlier_bars(self, tmp_path):
        _write_klines(tmp_path, "AAAUSDT", "1h")
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        times = [t0, t0 + timedelta(hours=8), t0 + timedelta(hours=16)]
        _write_funding(tmp_path, "AAAUSDT", times, [0.001, 0.002, 0.003])
        base = align_funding(self._grid(), ["AAAUSDT"], tmp_path)["AAAUSDT"].copy()
        _write_funding(tmp_path, "AAAUSDT", times, [0.001, 0.002, 99.0])
        after = align_funding(self._grid(), ["AAAUSDT"], tmp_path)["AAAUSDT"]
        assert np.allclose(base[:16], after[:16], equal_nan=True)

    def test_missing_snapshot_degrades_to_none_not_an_exception(self, tmp_path):
        _write_klines(tmp_path, "AAAUSDT", "1h")
        assert align_funding(self._grid(), ["AAAUSDT"], tmp_path) is None

    def test_one_missing_symbol_disables_funding_for_all(self, tmp_path):
        """Returning a short dict would leave some symbols with funding and
        others without — the same model fitted on two different feature sets."""
        _write_klines(tmp_path, "AAAUSDT", "1h")
        _write_klines(tmp_path, "BBBUSDT", "1h")
        _write_funding(tmp_path, "AAAUSDT", [datetime(2024, 1, 1, tzinfo=timezone.utc)], [0.001])
        assert align_funding(self._grid(), ["AAAUSDT", "BBBUSDT"], tmp_path) is None


class TestFundingSettlements:
    """Cash flows, not features: each print lands on exactly one bar."""

    def _grid(self, n=24):
        t0 = np.datetime64("2024-01-01T00:00:00")
        return t0 + np.arange(n) * np.timedelta64(1, "h")

    def test_rate_lands_only_on_its_settlement_bar(self, tmp_path):
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        _write_funding(tmp_path, "AAAUSDT",
                       [t0, t0 + timedelta(hours=8), t0 + timedelta(hours=16)],
                       [0.001, -0.002, 0.003])
        got = funding_settlements(self._grid(), ["AAAUSDT"], tmp_path)["AAAUSDT"]
        assert np.flatnonzero(got).tolist() == [0, 8, 16]
        assert got[[0, 8, 16]] == pytest.approx([0.001, -0.002, 0.003])
        assert got.sum() == pytest.approx(0.002), "summing must not multiply-count"

    def test_millisecond_offsets_still_land_on_the_hour_bar(self, tmp_path):
        t = datetime(2024, 1, 1, 8, 0, 0, 3000, tzinfo=timezone.utc)
        _write_funding(tmp_path, "AAAUSDT", [t], [0.001])
        got = funding_settlements(self._grid(), ["AAAUSDT"], tmp_path)["AAAUSDT"]
        assert np.flatnonzero(got).tolist() == [8]

    def test_settlement_in_a_missing_bar_is_dropped_not_moved(self, tmp_path):
        grid = np.delete(self._grid(), 8)          # outage: the 08:00 bar is gone
        t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        _write_funding(tmp_path, "AAAUSDT", [t0 + timedelta(hours=8)], [0.001])
        got = funding_settlements(grid, ["AAAUSDT"], tmp_path)["AAAUSDT"]
        assert not got.any()

    def test_missing_snapshot_returns_none(self, tmp_path):
        assert funding_settlements(self._grid(), ["AAAUSDT"], tmp_path) is None
