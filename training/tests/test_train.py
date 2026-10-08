"""Training-pipeline wiring: the decision rule and the fold bookkeeping."""

import numpy as np
import pytest

from gs_training.labels import BarrierSpec
from gs_training.splits import HOLDOUT_START
from gs_training.train import (
    Dataset,
    aggregate,
    expected_value,
    holdout_bar,
    slice_panel,
)


class TestExpectedValue:
    def test_costs_are_subtracted_round_trip(self):
        ev = expected_value(np.array([1.0]), np.array([0.10]), np.array([0.05]),
                            fee_rate=0.001, slippage_bps=5.0)
        # certain win of +10%, minus 2x0.1% fees and 2x5bps slippage
        assert ev[0] == pytest.approx(0.10 - 0.002 - 0.001)

    def test_a_coin_flip_on_symmetric_barriers_is_negative_after_fees(self):
        """The core economics: 50/50 with equal barriers loses exactly the fees."""
        ev = expected_value(np.array([0.5]), np.array([0.10]), np.array([0.10]),
                            fee_rate=0.001, slippage_bps=5.0)
        assert ev[0] < 0

    def test_probability_alone_is_not_the_rule(self):
        """Same p, different barrier geometry -> different decisions. This is
        why thresholding on p > 0.5 is wrong."""
        p = np.array([0.6, 0.6])
        ev = expected_value(p, np.array([0.20, 0.005]), np.array([0.05, 0.005]),
                            fee_rate=0.001, slippage_bps=5.0)
        assert ev[0] > 0 and ev[1] < 0

    def test_ev_rises_monotonically_with_probability(self):
        p = np.linspace(0, 1, 11)
        ev = expected_value(p, np.full(11, 0.1), np.full(11, 0.05), 0.001, 5.0)
        assert np.all(np.diff(ev) > 0)


class TestSlicePanel:
    def test_slice_keeps_every_symbol_and_field_aligned(self):
        panel = {s: {k: np.arange(100, dtype=float) for k in
                     ("open", "high", "low", "close", "volume")}
                 for s in ("BTCUSDT", "ETHUSDT")}
        sub = slice_panel(panel, 10, 40)
        assert set(sub) == set(panel)
        for b in sub.values():
            assert all(v.size == 30 for v in b.values())
            assert b["close"][0] == 10.0


class TestAggregate:
    def _fold(self, i, ret, bret):
        from gs_training.train import FoldResult
        base = {"total_return": bret, "profit_factor": 1.0, "max_drawdown": 0.1, "sharpe": 0.0}
        strat = {"total_return": ret, "profit_factor": 1.2, "max_drawdown": 0.2, "sharpe": 0.5}
        return FoldResult(fold=i, n_train=100, n_test=50, auc=0.55, brier=0.2,
                          n_signals=10, strategy=strat, baseline=base)

    def test_counts_folds_that_actually_beat_the_baseline(self):
        """Mean excess return can be carried by one lucky fold; the count is
        what shows whether an edge is persistent."""
        rs = [self._fold(0, 0.5, 0.0), self._fold(1, -0.1, 0.0), self._fold(2, -0.1, 0.0)]
        agg = aggregate(rs)
        assert agg["beat_baseline_folds"] == 1.0
        assert agg["mean_excess_return"] > 0, "average is positive despite 1-of-3 folds"

    def test_empty_results_aggregate_to_nothing(self):
        assert aggregate([]) == {}


class TestDataset:
    def test_len_matches_the_matrix(self):
        n = 7
        ds = Dataset(X=np.zeros((n, 3)), y=np.zeros(n), event_bar=np.arange(n),
                     label_end_bar=np.arange(n) + 10, weight=np.ones(n),
                     symbol=np.full(n, "BTCUSDT"), up_ret=np.full(n, 0.1),
                     dn_ret=np.full(n, 0.05))
        assert len(ds) == n

    def test_select_keeps_every_parallel_array_in_step(self):
        n = 10
        ds = Dataset(X=np.arange(n * 3.0).reshape(n, 3), y=np.arange(n) % 2,
                     event_bar=np.arange(n), label_end_bar=np.arange(n) + 10,
                     weight=np.linspace(0.1, 1.0, n),
                     symbol=np.array([f"S{i}" for i in range(n)]),
                     up_ret=np.arange(n) / 100.0, dn_ret=np.arange(n) / 200.0)
        keep = np.array([True, False] * 5)
        sub = ds.select(keep)
        assert len(sub) == 5
        for field in ("y", "event_bar", "label_end_bar", "weight", "symbol",
                      "up_ret", "dn_ret"):
            assert np.array_equal(getattr(sub, field), getattr(ds, field)[keep])
        assert np.array_equal(sub.X, ds.X[keep])

    def test_select_does_not_recompute_uniqueness(self):
        """Dropping an event's neighbours from the matrix does not make it more
        independent than it was — the weight must survive the subset unchanged."""
        n = 6
        w = np.linspace(0.01, 0.06, n)
        ds = Dataset(X=np.zeros((n, 2)), y=np.zeros(n), event_bar=np.arange(n),
                     label_end_bar=np.arange(n) + 5, weight=w,
                     symbol=np.full(n, "BTCUSDT"), up_ret=np.full(n, 0.1),
                     dn_ret=np.full(n, 0.05))
        sub = ds.select(np.array([True, False, False, False, False, True]))
        assert np.array_equal(sub.weight, np.array([w[0], w[-1]]))


class TestHoldoutBar:
    """The date -> bar-index conversion the training CLI hangs off."""

    def _grid(self, start, n):
        t0 = np.datetime64(start, "h")
        return (t0 + np.arange(n) * np.timedelta64(1, "h")).astype("datetime64[ns]")

    def test_locates_the_first_bar_at_or_after_the_cut(self):
        bars = self._grid("2025-09-30T00", 96)   # 4 days of hourly bars
        i = int(holdout_bar(bars))
        assert bars[i] == np.datetime64(HOLDOUT_START.replace(tzinfo=None), "ns")
        assert bars[i - 1] < np.datetime64("2025-10-01T00:00:00", "ns")

    def test_cache_ending_before_the_cut_gives_an_empty_holdout(self):
        """A stale cache must produce no holdout rather than a mislabelled one."""
        bars = self._grid("2024-01-01T00", 100)
        assert int(holdout_bar(bars)) == len(bars)

    def test_grid_starting_after_the_cut_reserves_everything(self):
        bars = self._grid("2026-01-01T00", 100)
        assert int(holdout_bar(bars)) == 0


class TestAbstention:
    """A fold that emits no signal has abstained. Scoring that as a 0% return
    pulls a losing strategy toward break-even and reports profit factor 0 for a
    fold that never opened a position."""

    def _fold(self, i, ret, bret, n_signals, pf=1.2):
        from gs_training.train import FoldResult
        return FoldResult(
            fold=i, n_train=100, n_test=50, auc=0.55, brier=0.2, n_signals=n_signals,
            strategy={"total_return": ret, "profit_factor": pf,
                      "max_drawdown": 0.2, "sharpe": 0.5},
            baseline={"total_return": bret, "profit_factor": 1.0,
                      "max_drawdown": 0.1, "sharpe": 0.0})

    def test_zero_signal_folds_are_excluded_from_return_averages(self):
        rs = [self._fold(0, -0.40, -0.50, 100), self._fold(1, 0.0, 0.0, 0, pf=0.0)]
        agg = aggregate(rs)
        assert agg["folds_no_signal"] == 1.0 and agg["folds_traded"] == 1.0
        assert agg["mean_return"] == pytest.approx(-0.40), "must not be averaged with 0%"
        assert agg["mean_profit_factor"] == pytest.approx(1.2)

    def test_all_folds_abstaining_reports_counts_without_fake_returns(self):
        agg = aggregate([self._fold(0, 0.0, 0.0, 0), self._fold(1, 0.0, 0.0, 0)])
        assert agg["folds_no_signal"] == 2.0
        assert "mean_return" not in agg, "no trades means no return to report"

    def test_infinite_profit_factor_does_not_poison_the_mean(self):
        rs = [self._fold(0, 0.1, 0.0, 10, pf=float("inf")),
              self._fold(1, 0.2, 0.0, 10, pf=2.0)]
        assert np.isfinite(aggregate(rs)["mean_profit_factor"])


class TestSideCli:
    def test_short_on_spot_is_rejected_before_any_work(self, monkeypatch, capsys):
        import sys
        from gs_training import train
        monkeypatch.setattr(sys, "argv", ["gs-train", "--side", "short", "--venue", "spot"])
        with pytest.raises(SystemExit):
            train.main()
        assert "spot cannot short" in capsys.readouterr().err

    def test_costs_for_venue(self):
        from gs_training.train import costs_for
        assert costs_for("perp").venue == "perp" and costs_for("perp").fee_rate == 0.0005
        assert costs_for("spot").venue == "spot" and costs_for("spot").fee_rate == 0.001
