"""Causality and prefix-stability of the feature library.

These are the tests that make the "one code path for training and live"
guarantee real. A lookahead here would not crash; it would produce a model that
backtests superbly and loses money on the VPS."""

import numpy as np
import pytest

from gs_training.features import (
    FEATURE_NAMES,
    MAX_LOOKBACK,
    atr,
    compute_features,
    feature_matrix,
    rsi,
    usable_mask,
)


def synth(n=1200, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    spread = close * rng.uniform(0.001, 0.01, n)
    high = close + spread
    low = close - spread
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = rng.lognormal(5, 0.5, n)
    times = np.arange("2024-01-01", n, dtype="datetime64[h]")
    return dict(open_=open_, high=high, low=low, close=close, volume=volume,
                quote_volume=volume * close,
                taker_buy_volume=volume * rng.uniform(0.3, 0.7, n),
                bar_times=times, btc_close=close * rng.uniform(0.99, 1.01, n),
                funding=rng.normal(0, 1e-4, n), open_interest=rng.lognormal(10, 0.1, n))


class TestCausality:
    def test_mutating_the_future_cannot_change_the_past(self):
        """The core no-lookahead property, feature by feature so a failure names
        the culprit instead of just saying 'something leaks'."""
        d = synth()
        base = compute_features(**d)
        cut = 800
        tampered = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in d.items()}
        for k in ("open_", "high", "low", "close", "volume", "quote_volume",
                  "taker_buy_volume", "btc_close", "funding", "open_interest"):
            tampered[k][cut:] *= 5.0
        after = compute_features(**tampered)
        for name in FEATURE_NAMES:
            assert np.allclose(base[name][:cut], after[name][:cut], equal_nan=True), (
                f"{name} depends on future bars")

    def test_prefix_stability_matches_live_inference(self):
        """Live holds a rolling window and appends. Computing on a prefix must
        therefore reproduce the full-series values exactly."""
        d = synth()
        full = compute_features(**d)
        cut = 900
        pref = {k: (v[:cut] if isinstance(v, np.ndarray) else v) for k, v in d.items()}
        partial = compute_features(**pref)
        for name in FEATURE_NAMES:
            assert np.allclose(full[name][:cut], partial[name][:cut], equal_nan=True), (
                f"{name} is not prefix-stable")


class TestShapeAndContract:
    def test_matrix_column_order_follows_the_declared_names(self):
        d = synth(600)
        f = compute_features(**d)
        X = feature_matrix(f)
        assert X.shape == (600, len(FEATURE_NAMES))
        for j, name in enumerate(FEATURE_NAMES):
            assert np.allclose(X[:, j], f[name], equal_nan=True)

    def test_optional_inputs_keep_the_column_set_stable(self):
        """A model trained with funding must never be handed a narrower matrix."""
        d = synth(600)
        minimal = compute_features(**{k: d[k] for k in
                                      ("open_", "high", "low", "close", "volume")})
        assert set(minimal) == set(FEATURE_NAMES)
        assert feature_matrix(minimal).shape == (600, len(FEATURE_NAMES))
        assert np.all(np.isnan(minimal["funding_rate"]))

    def test_feature_set_drift_is_caught(self):
        d = synth(400)
        f = compute_features(**d)
        del f["rsi_14"]
        with pytest.raises(KeyError):
            feature_matrix(f)

    def test_warmup_rows_are_nan_not_imputed(self):
        d = synth(600)
        X = feature_matrix(compute_features(**d))
        assert not np.all(np.isfinite(X[0])), "row 0 cannot have a 336-bar return"

    def test_usable_mask_drops_warmup_and_keeps_the_rest(self):
        d = synth(1200)
        X = feature_matrix(compute_features(**d))
        ok = usable_mask(X)
        assert not ok[:MAX_LOOKBACK].any()
        assert ok.sum() > 700
        assert np.all(np.isfinite(X[ok][:, ~np.all(~np.isfinite(X), axis=0)]))

    def test_usable_mask_survives_missing_optional_inputs(self):
        """Omitting funding must not empty the dataset."""
        d = synth(1200)
        X = feature_matrix(compute_features(**{k: d[k] for k in
                                               ("open_", "high", "low", "close", "volume")}))
        assert usable_mask(X).sum() > 700


class TestIndicators:
    def test_rsi_is_bounded_and_extreme_on_a_monotonic_series(self):
        up = np.linspace(100, 200, 100)
        r = rsi(up, 14)
        assert np.nanmax(r) <= 100.0 and np.nanmin(r[np.isfinite(r)]) >= 0.0
        assert r[-1] == pytest.approx(100.0)

    def test_atr_is_positive_and_tracks_range(self):
        d = synth(300)
        a = atr(d["high"], d["low"], d["close"], 14)
        assert np.all(a[np.isfinite(a)] > 0)
        assert np.isnan(a[0])


class TestTimestampForms:
    """Regression: the synthetic fixtures above use datetime64, but `load_panel`
    hands back tz-aware `datetime` objects. That mismatch let a crash reach real
    data while the suite stayed green, so both forms are covered here."""

    def _both_forms(self, n=500):
        import datetime as dt
        base = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        objs = np.array([base + dt.timedelta(hours=i) for i in range(n)], dtype=object)
        dt64 = np.arange("2024-01-01", n, dtype="datetime64[h]").astype("datetime64[ns]")
        return objs, dt64

    def test_tz_aware_datetime_objects_are_accepted(self):
        objs, _ = self._both_forms()
        d = synth(500)
        d["bar_times"] = objs
        f = compute_features(**d)
        assert np.all(np.isfinite(f["hour_sin"]))

    def test_both_timestamp_forms_agree(self):
        objs, dt64 = self._both_forms()
        d = synth(500)
        a = compute_features(**{**d, "bar_times": objs})
        b = compute_features(**{**d, "bar_times": dt64})
        for k in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
            assert np.allclose(a[k], b[k]), f"{k} differs between timestamp forms"

    def test_day_of_week_is_correct(self):
        import datetime as dt
        from gs_training.features import _time_parts
        # 2024-01-01 was a Monday.
        t = np.array([dt.datetime(2024, 1, d, tzinfo=dt.timezone.utc) for d in range(1, 8)],
                     dtype=object)
        _, dow = _time_parts(t)
        assert list(dow) == [0, 1, 2, 3, 4, 5, 6]
