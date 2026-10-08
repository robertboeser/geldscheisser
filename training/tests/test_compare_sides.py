"""Comparison helpers: regime tagging must be causal, and trades must land in
the regime of their decision bar, not their entry or exit."""

import numpy as np

from gs_training.backtest import Trade
from gs_training.compare_sides import REGIME_SMA_BARS, _funding_flow, btc_regime, regime_split
from gs_training.labels import LONG, SHORT, WIN


def _panel(close):
    return {"BTCUSDT": {"close": np.asarray(close, dtype=float)}}


class TestBtcRegime:
    def test_warmup_is_zero_then_tracks_the_sma(self):
        n = REGIME_SMA_BARS + 100
        close = np.r_[np.full(REGIME_SMA_BARS, 100.0), np.full(50, 120.0), np.full(50, 80.0)]
        r = btc_regime(_panel(close))
        assert (r[:REGIME_SMA_BARS - 1] == 0).all()
        assert (r[REGIME_SMA_BARS:REGIME_SMA_BARS + 50] == 1).all()
        assert (r[-10:] == -1).all() and r.size == n

    def test_is_causal(self):
        rng = np.random.default_rng(0)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, REGIME_SMA_BARS + 500)))
        base = btc_regime(_panel(close))
        bumped = close.copy(); bumped[-100:] *= 5
        assert np.array_equal(base[:-100], btc_regime(_panel(bumped))[:-100])


class TestRegimeSplit:
    def test_trade_is_tagged_by_its_decision_bar(self):
        regime = np.array([1, -1, -1, 1, 1])
        t = Trade("BTCUSDT", entry_i=2, exit_i=4, entry_px=1, exit_px=2, qty=1,
                  outcome=WIN, fees=0.0)
        out = regime_split([t], regime)
        assert out["btc_below_sma200"]["trades"] == 1, "decision bar is entry_i - 1"
        assert out["btc_above_sma200"]["trades"] == 0


class TestFundingFlow:
    def test_sign_and_window_match_the_backtester(self):
        fund = np.zeros(10); fund[[2, 5, 8]] = 0.001
        # held over bars 3..8: settlements at 5 and 8, not 2 (before entry+1)
        assert _funding_flow(fund, 2, 8, SHORT) == np.float64(0.002)
        assert _funding_flow(fund, 2, 8, LONG) == -0.002


class TestBaselineBand:
    def test_runs_on_the_perp_venue_with_funding(self):
        """Regression: funding was passed positionally into the portfolio slot."""
        from gs_training.compare_sides import baseline_band
        from gs_training.labels import BarrierSpec
        from gs_training.train import FoldResult, costs_for
        n = 400
        c = 100 + np.sin(np.arange(n) / 10) * 5
        panel = {"BTCUSDT": {"open": c, "high": c * 1.01, "low": c * 0.99, "close": c}}
        sigma = {"BTCUSDT": np.full(n, 0.01)}
        fund = {"BTCUSDT": np.where(np.arange(n) % 8 == 0, 0.0001, 0.0)}
        r = FoldResult(fold=0, n_train=1, n_test=1, auc=0.5, brier=0.25, n_signals=5,
                       window=(0, n))
        band = baseline_band([r], panel, sigma, BarrierSpec(1, 0.5, 40, side=SHORT),
                             costs_for("perp"), fund, seeds=3)
        assert np.isfinite(band[0]["mean"]) and band[0]["min"] <= band[0]["max"]
