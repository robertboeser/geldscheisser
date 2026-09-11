"""Cross-sectional features: relative strength across the traded universe.

Held to the same two properties as the rest of `features.py` — causality and
prefix-stability — because they are computed differently (across symbols at one
instant, rather than across time for one symbol) and so could plausibly break
them in a way the per-symbol tests would never see.
"""

import numpy as np
import pytest

from gs_training.features import (
    CROSS_SECTIONAL_NAMES,
    FEATURE_NAMES,
    compute_features,
    cross_sectional,
)


def panel(n=600, k=4, seed=0):
    rng = np.random.default_rng(seed)
    out = {}
    for j in range(k):
        close = 100 * np.exp(np.cumsum(rng.normal(0.0002 * j, 0.01, n)))
        out[f"S{j}"] = {
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": close * 1.01, "low": close * 0.99, "close": close,
            "volume": rng.lognormal(3, 0.3, n),
        }
    return out


class TestCausality:
    def test_mutating_the_future_cannot_change_the_past(self):
        p = panel()
        base = cross_sectional(p)
        cut = 400
        tampered = {s: {k: v.copy() for k, v in b.items()} for s, b in p.items()}
        for b in tampered.values():
            for k in ("open", "high", "low", "close"):
                b[k][cut:] *= 5.0
        after = cross_sectional(tampered)
        for sym in p:
            for name in CROSS_SECTIONAL_NAMES:
                assert np.allclose(base[sym][name][:cut], after[sym][name][:cut],
                                   equal_nan=True), f"{name} depends on future bars"

    def test_prefix_stability_matches_live_inference(self):
        p = panel()
        full = cross_sectional(p)
        cut = 500
        pref = {s: {k: v[:cut] for k, v in b.items()} for s, b in p.items()}
        partial = cross_sectional(pref)
        for sym in p:
            for name in CROSS_SECTIONAL_NAMES:
                assert np.allclose(full[sym][name][:cut], partial[sym][name],
                                   equal_nan=True), f"{name} is not prefix-stable"


class TestSemantics:
    def test_rank_orders_symbols_by_return(self):
        """Hand-built: three symbols whose 24h returns are strictly ordered."""
        n = 60
        p = {}
        for j, drift in enumerate((0.0, 0.002, 0.004)):
            close = 100 * np.exp(np.arange(n) * drift)
            p[f"S{j}"] = {"open": close, "high": close, "low": close, "close": close,
                          "volume": np.ones(n)}
        xs = cross_sectional(p)
        i = n - 1
        assert xs["S0"]["xs_rank_ret_24"][i] == pytest.approx(0.0)   # beats nobody
        assert xs["S1"]["xs_rank_ret_24"][i] == pytest.approx(0.5)
        assert xs["S2"]["xs_rank_ret_24"][i] == pytest.approx(1.0)   # beats both

    def test_excess_return_is_centred_on_the_universe(self):
        p = panel(seed=3)
        xs = cross_sectional(p)
        stacked = np.column_stack([xs[s]["xs_excess_ret_168"] for s in p])
        live = np.isfinite(stacked).all(axis=1)
        assert live.any()
        assert np.allclose(stacked[live].sum(axis=1), 0.0, atol=1e-9)

    def test_breadth_is_a_fraction_and_universe_wide(self):
        p = panel(seed=4)
        xs = cross_sectional(p)
        b = xs["S0"]["xs_breadth_200"]
        live = np.isfinite(b)
        assert live.any()
        assert np.all((b[live] >= 0.0) & (b[live] <= 1.0))
        for s in p:  # regime context, identical for every symbol
            assert np.allclose(xs[s]["xs_breadth_200"], b, equal_nan=True)

    def test_dispersion_is_zero_when_symbols_move_together(self):
        n = 120
        close = 100 * np.exp(np.cumsum(np.full(n, 0.001)))
        p = {f"S{j}": {"open": close, "high": close, "low": close, "close": close,
                       "volume": np.ones(n)} for j in range(3)}
        d = cross_sectional(p)["S0"]["xs_dispersion_24"]
        assert np.allclose(d[np.isfinite(d)], 0.0, atol=1e-12)

    def test_warmup_symbols_are_excluded_not_counted_as_zero(self):
        """A symbol whose own 24h return is not yet defined must not be ranked;
        treating its NaN as a value would make every early rank wrong."""
        n = 60
        p = panel(n=n, k=3, seed=5)
        xs = cross_sectional(p)
        for s in p:
            # ret_24 needs 24 prior bars, so ranks before that are undefined.
            assert np.all(np.isnan(xs[s]["xs_rank_ret_24"][:24]))

    def test_single_symbol_universe_has_no_ordering(self):
        p = panel(n=100, k=1, seed=6)
        xs = cross_sectional(p)
        assert np.all(np.isnan(xs["S0"]["xs_rank_ret_24"]))

    def test_misaligned_grid_is_rejected(self):
        p = panel(n=100, k=2, seed=7)
        p["S1"] = {k: v[:-5] for k, v in p["S1"].items()}
        with pytest.raises(ValueError, match="aligned grid"):
            cross_sectional(p)


class TestIntegration:
    def test_cross_features_reach_the_matrix(self):
        p = panel(seed=8)
        xs = cross_sectional(p)
        b = p["S0"]
        f = compute_features(b["open"], b["high"], b["low"], b["close"], b["volume"],
                             cross=xs["S0"])
        for name in CROSS_SECTIONAL_NAMES:
            assert np.isfinite(f[name]).any(), f"{name} is all-NaN despite being supplied"

    def test_omitting_cross_keeps_the_column_set_stable(self):
        p = panel(seed=9)
        b = p["S0"]
        f = compute_features(b["open"], b["high"], b["low"], b["close"], b["volume"])
        assert set(f) == set(FEATURE_NAMES)
        for name in CROSS_SECTIONAL_NAMES:
            assert not np.isfinite(f[name]).any()
