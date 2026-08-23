"""Backtester accounting and the frictions it must not forget."""

import numpy as np
import pytest

from gs_training.backtest import (
    CostModel,
    Portfolio,
    Signal,
    run_backtest,
    summarize,
)
from gs_training.baselines import buy_and_hold, random_entries, sma_cross
from gs_training.labels import LOSS, WIN, BarrierSpec
from gs_training.metrics import max_drawdown, performance, profit_factor, sharpe, stress_test

FREE = CostModel(fee_rate=0.0, slippage_bps=0.0)


def flat_panel(closes, symbol="BTCUSDT"):
    c = np.array(closes, dtype=float)
    return {symbol: {"open": c.copy(), "high": c.copy(), "low": c.copy(),
                     "close": c.copy(), "volume": np.ones_like(c)}}


def rising(n=100, start=100.0, step=1.0):
    return start + step * np.arange(n)


class TestExecutionRealism:
    def test_entry_happens_on_the_bar_after_the_signal(self):
        bars = flat_panel(rising(60))
        sig = [Signal(bar=10, symbol="BTCUSDT")]
        r = run_backtest(bars, sig, BarrierSpec(2, 1, 20, scale_by_horizon=False),
                         {"BTCUSDT": np.full(60, 0.05)}, FREE, Portfolio())
        assert r.trades[0].entry_i == 11, "must not fill on the decision bar"

    def test_fees_and_slippage_reduce_pnl(self):
        bars = flat_panel(rising(80))
        sig = [Signal(bar=5, symbol="BTCUSDT")]
        spec, sigma = BarrierSpec(2, 1, 30, scale_by_horizon=False), {"BTCUSDT": np.full(80, 0.05)}
        free = run_backtest(bars, sig, spec, sigma, FREE, Portfolio())
        costly = run_backtest(bars, sig, spec, sigma, CostModel(0.001, 5.0), Portfolio())
        assert costly.trades[0].pnl < free.trades[0].pnl
        assert costly.trades[0].fees > 0

    def test_concurrency_cap_skips_rather_than_silently_trading(self):
        bars = flat_panel(rising(200))
        sigs = [Signal(bar=b, symbol="BTCUSDT") for b in range(10, 20)]
        r = run_backtest(bars, sigs, BarrierSpec(5, 5, 100, scale_by_horizon=False),
                         {"BTCUSDT": np.full(200, 0.05)}, FREE,
                         Portfolio(max_positions=2))
        assert len(r.trades) + r.skipped_no_slot + r.skipped_no_cash == len(sigs)
        assert r.skipped_no_slot > 0, "cap must bite and be reported"

    def test_capital_is_released_when_a_position_closes(self):
        """Sequential trades must be possible on a small account; if exits did
        not free cash the second signal would be skipped."""
        bars = flat_panel(rising(200))
        sigs = [Signal(bar=5, symbol="BTCUSDT"), Signal(bar=120, symbol="BTCUSDT")]
        r = run_backtest(bars, sigs, BarrierSpec(2, 1, 40, scale_by_horizon=False),
                         {"BTCUSDT": np.full(200, 0.05)}, FREE,
                         Portfolio(starting_cash=1000.0, position_frac=1.0, max_positions=1))
        assert len(r.trades) == 2 and r.skipped_no_cash == 0

    def test_equity_is_marked_to_market_every_bar(self):
        bars = flat_panel(rising(60))
        r = run_backtest(bars, [Signal(bar=5, symbol="BTCUSDT")], BarrierSpec(2, 1, 30, scale_by_horizon=False),
                         {"BTCUSDT": np.full(60, 0.05)}, FREE, Portfolio())
        assert np.isfinite(r.equity).all()
        assert r.equity.size == 60

    def test_signal_too_close_to_the_end_is_not_traded(self):
        """No forward data means no honest exit; inventing one is how a
        backtest books phantom trades at the edge of the sample."""
        bars = flat_panel(rising(50))
        r = run_backtest(bars, [Signal(bar=45, symbol="BTCUSDT")], BarrierSpec(2, 1, 30, scale_by_horizon=False),
                         {"BTCUSDT": np.full(50, 0.05)}, FREE, Portfolio())
        assert r.trades == []

    def test_rejects_misaligned_symbol_grids(self):
        bars = flat_panel(rising(50))
        bars["ETHUSDT"] = {k: v[:40] for k, v in bars["BTCUSDT"].items()}
        with pytest.raises(ValueError, match="one bar grid"):
            run_backtest(bars, [], BarrierSpec(), {}, FREE, Portfolio())


class TestOutcomesMatchTheLabelRule:
    def test_a_rising_market_resolves_as_a_win_at_the_target(self):
        bars = flat_panel(rising(120, step=2.0))
        r = run_backtest(bars, [Signal(bar=5, symbol="BTCUSDT")], BarrierSpec(2, 1, 50, scale_by_horizon=False),
                         {"BTCUSDT": np.full(120, 0.05)}, FREE, Portfolio())
        assert r.trades[0].outcome == WIN and r.trades[0].pnl > 0

    def test_a_falling_market_resolves_as_a_loss(self):
        bars = flat_panel(rising(120, start=300.0, step=-2.0))
        r = run_backtest(bars, [Signal(bar=5, symbol="BTCUSDT")], BarrierSpec(2, 1, 50, scale_by_horizon=False),
                         {"BTCUSDT": np.full(120, 0.05)}, FREE, Portfolio())
        assert r.trades[0].outcome == LOSS and r.trades[0].pnl < 0


class TestMetrics:
    def test_max_drawdown_of_a_known_curve(self):
        assert max_drawdown(np.array([100.0, 120, 60, 90])) == pytest.approx(0.5)

    def test_max_drawdown_of_a_monotonic_curve_is_zero(self):
        assert max_drawdown(np.array([1.0, 2, 3, 4])) == 0.0

    def test_profit_factor(self):
        assert profit_factor(np.array([10.0, -5.0, 5.0])) == pytest.approx(3.0)
        assert profit_factor(np.array([1.0, 2.0])) == float("inf")
        assert profit_factor(np.zeros(0)) == 0.0

    def test_sharpe_of_a_flat_curve_is_zero(self):
        assert sharpe(np.full(50, 100.0)) == 0.0


class TestBaselines:
    def test_sma_cross_fires_only_on_the_crossing_bar(self):
        close = np.concatenate([np.full(300, 100.0), np.linspace(100, 200, 300)])
        sigs = sma_cross(close, "BTCUSDT", fast=10, slow=50)
        assert 0 < len(sigs) < 10, "a single regime change is not many crosses"

    def test_random_entries_are_reproducible_and_bounded(self):
        a = random_entries(50, 1000, ["BTCUSDT", "ETHUSDT"], seed=7)
        b = random_entries(50, 1000, ["BTCUSDT", "ETHUSDT"], seed=7)
        assert [(s.bar, s.symbol) for s in a] == [(s.bar, s.symbol) for s in b]
        assert all(0 <= s.bar < 1000 for s in a)

    def test_buy_and_hold_is_one_signal(self):
        assert len(buy_and_hold("BTCUSDT")) == 1


class TestStressHarness:
    def test_stress_test_reports_every_variation(self):
        bars = flat_panel(rising(400))
        sigma = {"BTCUSDT": np.full(400, 0.05)}
        sigs = [Signal(bar=b, symbol="BTCUSDT") for b in range(10, 200, 20)]
        out = stress_test(bars, sigs, BarrierSpec(2, 1, 60, scale_by_horizon=False), sigma,
                          CostModel(0.001, 5.0), Portfolio(), drop_best=2)
        assert set(out) == {"base", "double_fees", "delayed_entry", "drop_best_2"}
        assert out["double_fees"]["total_fees"] > out["base"]["total_fees"]
        assert out["drop_best_2"]["trades"] == out["base"]["trades"] - 2


class TestAccountingInvariants:
    """Equity and realised P&L are two views of the same money. If they
    disagree, one of them is lying — and the equity curve is what gets
    reported, so the lie is the flattering kind."""

    def test_final_equity_equals_starting_cash_plus_realised_pnl(self):
        bars = flat_panel(rising(400))
        sigma = {"BTCUSDT": np.full(400, 0.02)}
        sigs = [Signal(bar=b, symbol="BTCUSDT") for b in range(10, 300, 7)]
        r = run_backtest(bars, sigs, BarrierSpec(1, 0.5, 60, scale_by_horizon=False),
                         sigma, CostModel(0.001, 5.0), Portfolio())
        realised = sum(t.pnl for t in r.trades)
        assert r.final_equity == pytest.approx(r.starting_cash + realised, rel=1e-9)

    def test_a_position_resolving_on_its_entry_bar_is_still_booked(self):
        """A bar wide enough to hit a barrier immediately must produce a closed
        trade, not a position that silently stays open for the whole run."""
        c = np.full(50, 100.0)
        h = c.copy(); h[6] = 200.0          # bar 6 spikes through the target
        bars = {"BTCUSDT": {"open": c.copy(), "high": h, "low": c.copy(),
                            "close": c.copy(), "volume": np.ones(50)}}
        r = run_backtest(bars, [Signal(bar=5, symbol="BTCUSDT")],
                         BarrierSpec(1, 0.5, 20, scale_by_horizon=False),
                         {"BTCUSDT": np.full(50, 0.05)}, FREE, Portfolio())
        assert len(r.trades) == 1, "same-bar resolution must be booked"
        assert r.trades[0].exit_i == r.trades[0].entry_i == 6
        assert r.final_equity == pytest.approx(r.starting_cash + r.trades[0].pnl)

    def test_no_position_is_left_open_at_the_end(self):
        bars = flat_panel(rising(300))
        sigs = [Signal(bar=b, symbol="BTCUSDT") for b in range(5, 250, 11)]
        r = run_backtest(bars, sigs, BarrierSpec(1, 0.5, 40, scale_by_horizon=False),
                         {"BTCUSDT": np.full(300, 0.03)}, FREE, Portfolio())
        # Every trade scan_barriers resolves has exit_i < n, so all must close.
        assert r.final_equity == pytest.approx(
            r.starting_cash + sum(t.pnl for t in r.trades), rel=1e-9)
