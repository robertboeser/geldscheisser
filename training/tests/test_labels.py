"""Barrier semantics. Every assertion here encodes a pessimism choice —
if one of these flips, backtests start inventing money."""

import numpy as np
import pytest

from gs_training.labels import (
    LONG,
    LOSS,
    SHORT,
    TIMEOUT,
    UNRESOLVED,
    WIN,
    BarrierSpec,
    cusum_events,
    ewm_volatility,
    label_events,
    scan_barriers,
)


def series(closes, highs=None, lows=None, opens=None):
    c = np.array(closes, dtype=float)
    return (np.array(opens if opens is not None else closes, dtype=float),
            np.array(highs if highs is not None else closes, dtype=float),
            np.array(lows if lows is not None else closes, dtype=float), c)


class TestScanBarriers:
    def test_upper_barrier_first_is_a_win(self):
        o, h, l, c = series([100, 101, 110, 100])
        assert scan_barriers(o, h, l, c, 0, 100, 105, 95, 4) == (WIN, 2, 105.0)

    def test_lower_barrier_first_is_a_loss(self):
        # Bar 2 opens at 96 (above the 95 stop) and dips to 90 -> a normal stop
        # fill at the barrier, not a gap.
        o, h, l, c = series([100, 99, 96, 100], lows=[100, 99, 90, 100],
                            opens=[100, 99, 96, 100])
        outcome, i, px = scan_barriers(o, h, l, c, 0, 100, 105, 95, 4)
        assert (outcome, i, px) == (LOSS, 2, 95.0)

    def test_no_touch_times_out_at_the_last_bar_close(self):
        o, h, l, c = series([100, 101, 99, 102])
        assert scan_barriers(o, h, l, c, 0, 100, 110, 90, 4) == (TIMEOUT, 3, 102.0)

    def test_same_bar_touching_both_barriers_is_a_loss(self):
        """OHLC hides the intrabar path. Resolving it as a win is how a
        backtest quietly manufactures profit."""
        o, h, l, c = series([100], highs=[110], lows=[90], opens=[100])
        outcome, _, px = scan_barriers(o, h, l, c, 0, 100, 105, 95, 1)
        assert outcome == LOSS and px == 95.0

    def test_gap_through_the_stop_fills_at_the_open_not_the_barrier(self):
        """A stop at 95 does not fill at 95 when the bar opens at 80."""
        o, h, l, c = series([80], highs=[85], lows=[78], opens=[80])
        outcome, _, px = scan_barriers(o, h, l, c, 0, 100, 105, 95, 1)
        assert outcome == LOSS and px == 80.0, "must fill at the gapped open"

    def test_gap_through_the_target_still_fills_at_the_target(self):
        """Deliberately asymmetric with the stop case: favourable gaps are not
        assumed to pay extra."""
        o, h, l, c = series([120], highs=[125], lows=[119], opens=[120])
        outcome, _, px = scan_barriers(o, h, l, c, 0, 100, 105, 95, 1)
        assert outcome == WIN and px == 105.0

    def test_insufficient_forward_data_is_unresolved_not_timeout(self):
        o, h, l, c = series([100, 101])
        assert scan_barriers(o, h, l, c, 0, 100, 110, 90, 10)[0] == UNRESOLVED


class TestShortBarriers:
    """A short is the long scan on the negated series, so each pessimism rule
    must reappear mirrored: target below, stop above, same-bar = loss, gap up
    through the stop fills at the (higher) open."""

    def test_lower_target_first_is_a_win(self):
        o, h, l, c = series([100, 99, 90, 100])
        assert scan_barriers(o, h, l, c, 0, 100, 95, 105, 4, SHORT) == (WIN, 2, 95.0)

    def test_upper_stop_first_is_a_loss(self):
        o, h, l, c = series([100, 101, 104, 100], highs=[100, 101, 110, 100])
        assert scan_barriers(o, h, l, c, 0, 100, 95, 105, 4, SHORT) == (LOSS, 2, 105.0)

    def test_same_bar_touching_both_barriers_is_a_loss(self):
        o, h, l, c = series([100], highs=[110], lows=[90], opens=[100])
        outcome, _, px = scan_barriers(o, h, l, c, 0, 100, 95, 105, 1, SHORT)
        assert outcome == LOSS and px == 105.0

    def test_gap_up_through_the_stop_fills_at_the_open(self):
        o, h, l, c = series([120], highs=[125], lows=[118], opens=[120])
        outcome, _, px = scan_barriers(o, h, l, c, 0, 100, 95, 105, 1, SHORT)
        assert outcome == LOSS and px == 120.0, "must fill at the gapped open"

    def test_gap_down_through_the_target_still_fills_at_the_target(self):
        o, h, l, c = series([80], highs=[82], lows=[78], opens=[80])
        outcome, _, px = scan_barriers(o, h, l, c, 0, 100, 95, 105, 1, SHORT)
        assert outcome == WIN and px == 95.0

    def test_timeout_and_unresolved_match_the_long_rules(self):
        o, h, l, c = series([100, 101, 99, 102])
        assert scan_barriers(o, h, l, c, 0, 100, 90, 110, 4, SHORT) == (TIMEOUT, 3, 102.0)
        assert scan_barriers(o, h, l, c, 0, 100, 90, 110, 10, SHORT)[0] == UNRESOLVED

    def test_short_equals_long_on_the_negated_series(self):
        rng = np.random.default_rng(3)
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
        o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, 0.005, 300))
        h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, 300))
        l = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, 300))
        for e in range(0, 250, 7):
            short = scan_barriers(o, h, l, c, e, o[e], o[e] * 0.95, o[e] * 1.03, 40, SHORT)
            long_ = scan_barriers(-o, -l, -h, -c, e, -o[e], -o[e] * 0.95, -o[e] * 1.03, 40, LONG)
            assert short[:2] == long_[:2] and short[2] == pytest.approx(-long_[2])

    def test_barrier_prices_flip_for_a_short(self):
        spec = BarrierSpec(1.0, 0.5, 100, side=SHORT)
        target, stop = spec.barrier_prices(100.0, 0.01)       # horizon sigma 0.10
        assert target == pytest.approx(90.0) and stop == pytest.approx(105.0)

    def test_rejects_an_unknown_side(self):
        with pytest.raises(ValueError, match="side"):
            BarrierSpec(side=0)

    def test_short_label_return_is_positive_when_price_falls(self):
        o, h, l, c = series([100, 100, 80, 80, 80, 80], opens=[100] * 2 + [80] * 4)
        out = label_events(o, h, l, c, np.full(6, 0.10), np.array([0]),
                           BarrierSpec(1, 0.5, 4, scale_by_horizon=False, side=SHORT))
        assert out["outcome"][0] == WIN
        assert out["ret"][0] == pytest.approx(0.10)


class TestLabelEvents:
    def test_entry_is_the_next_bar_open_never_the_decision_close(self):
        o, h, l, c = series([100, 200, 201, 202], opens=[100, 200, 201, 202])
        sigma = np.full(4, 0.10)
        out = label_events(o, h, l, c, sigma, np.array([0]), BarrierSpec(2, 1, 3, scale_by_horizon=False))
        assert out["entry_idx"][0] == 1
        assert out["entry_px"][0] == 200.0, "filled at bar 1's open, not bar 0's close"

    def test_barrier_width_uses_volatility_from_the_decision_bar(self):
        o, h, l, c = series([100] * 6, opens=[100] * 6)
        sigma = np.array([0.10, 0.99, 0.99, 0.99, 0.99, 0.99])
        out = label_events(o, h, l, c, sigma, np.array([0]), BarrierSpec(2, 1, 4, scale_by_horizon=False))
        # sigma[0]=0.10 -> barriers at 120/90, untouched by a flat series.
        assert out["outcome"][0] == TIMEOUT

    def test_label_end_spans_the_full_horizon_even_when_resolved_early(self):
        """Purging must assume the whole window: at decision time you could not
        have known the barrier would resolve on bar two."""
        o, h, l, c = series([100, 100, 130, 100, 100, 100], opens=[100] * 6)
        sigma = np.full(6, 0.10)
        out = label_events(o, h, l, c, sigma, np.array([0]), BarrierSpec(2, 1, 4, scale_by_horizon=False))
        assert out["outcome"][0] == WIN
        assert out["exit_idx"][0] == 2
        assert out["label_end_idx"][0] == 1 + 4 - 1, "label_end is the horizon, not the exit"

    def test_unresolved_events_are_left_unresolved(self):
        o, h, l, c = series([100] * 3, opens=[100] * 3)
        out = label_events(o, h, l, c, np.full(3, 0.1), np.array([2]), BarrierSpec(2, 1, 10, scale_by_horizon=False))
        assert out["outcome"][0] == UNRESOLVED
        assert not np.isfinite(out["ret"][0])


class TestVolatilityAndEvents:
    def test_ewm_volatility_is_causal_and_positive(self):
        rng = np.random.default_rng(0)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 500)))
        v = ewm_volatility(close, span=50)
        assert np.isnan(v[0])
        assert np.all(v[2:] >= 0)
        # A late spike must not change an earlier estimate.
        spiked = close.copy()
        spiked[400:] *= 3
        assert np.allclose(v[:399], ewm_volatility(spiked, span=50)[:399], equal_nan=True)

    def test_cusum_emits_on_drift_not_on_every_bar(self):
        close = np.concatenate([np.full(50, 100.0), np.linspace(100, 130, 50)])
        events = cusum_events(close, 0.05)
        assert events.size > 0
        assert all(e >= 50 for e in events), "flat stretch should emit nothing"
        assert events.size < close.size / 4, "must be far sparser than every bar"


class TestHorizonScaling:
    """A per-bar sigma over a 336-bar hold must be widened, or every position
    resolves in hours and the labels describe scalping, not swing trading."""

    def test_barriers_widen_with_the_square_root_of_the_horizon(self):
        spec = BarrierSpec(upper_mult=1.0, lower_mult=1.0, max_bars=100)
        up, dn = spec.barrier_prices(100.0, 0.01)   # 0.01 * sqrt(100) = 0.10
        assert up == pytest.approx(110.0) and dn == pytest.approx(90.0)

    def test_scaling_can_be_switched_off(self):
        spec = BarrierSpec(upper_mult=1.0, lower_mult=1.0, max_bars=100,
                           scale_by_horizon=False)
        up, dn = spec.barrier_prices(100.0, 0.01)
        assert up == pytest.approx(101.0) and dn == pytest.approx(99.0)

    def test_unscaled_sigma_would_make_the_vertical_barrier_unreachable(self):
        """Regression for the bug this fixed: with per-bar sigma the barriers
        sit ~18x too close and nothing ever times out."""
        rng = np.random.default_rng(0)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 4000)))
        o = h = l = c = close
        sigma = ewm_volatility(close, span=100)
        ev = np.arange(200, 3000, 20)
        naive = label_events(o, h, l, c, sigma, ev,
                             BarrierSpec(1.0, 0.5, 336, scale_by_horizon=False))
        scaled = label_events(o, h, l, c, sigma, ev, BarrierSpec(1.0, 0.5, 336))
        naive_to = np.mean(naive["outcome"][naive["outcome"] > -2] == TIMEOUT)
        scaled_to = np.mean(scaled["outcome"][scaled["outcome"] > -2] == TIMEOUT)
        assert naive_to == 0.0, "unscaled: vertical barrier never binds"
        assert scaled_to > 0.05, "scaled: some positions should reach the horizon"
