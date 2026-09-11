"""Shared feature library — the one code path used for training *and* live inference.

This module is the single largest source of train/serve skew in a system like
this, so it is written to make skew structurally hard rather than merely
discouraged:

- **Every feature is causal.** Value at index `i` depends only on bars `<= i`.
  No centred windows, no `shift(-1)`, no full-series normalisation (a z-score
  against the whole sample leaks the future into every row). `test_features.py`
  asserts this by mutating the tail of a series and checking earlier values are
  bit-identical.
- **Prefix-stable.** Computing on `close[:k]` gives the same answers for the
  first `k` rows as computing on the full array. That is exactly what live
  inference does — it holds a rolling window and appends — so if this property
  holds, the VPS necessarily sees the same numbers the model trained on.
- **One ordered name list.** `FEATURE_NAMES` fixes column order. A model is only
  meaningful alongside the column order it was fitted with; deriving it from a
  dict iteration order is how a silent feature-permutation bug happens.

Warm-up is expressed as NaN, never as a filled value. `MAX_LOOKBACK` says how
many bars must precede a row for every feature to be finite; rows before that
must be dropped, not imputed, because an imputed feature is a fabricated
observation in exactly the way a truncated label is.

Order book features are absent on purpose: the archive holds only ~18 days of
rollups, far short of the 4–8 week gate, so they join later. Everything here
comes from klines plus (optionally) funding rates, open interest, and BTC as a
market-regime reference.
"""

from __future__ import annotations

import numpy as np

# Longest window any feature uses. Rows with fewer preceding bars are NaN.
MAX_LOOKBACK = 336

FEATURE_NAMES: tuple[str, ...] = (
    # trend / momentum
    "ret_1", "ret_6", "ret_24", "ret_72", "ret_168", "ret_336",
    "ema_dist_12", "ema_dist_26", "ema_dist_50", "ema_dist_200",
    "ema_12_26_spread", "ema_50_200_spread",
    "rsi_14", "macd_hist", "bb_pos_20",
    # volatility
    "atr_14_norm", "vol_ewm_100", "vol_ratio_24_168",
    # candle shape
    "body_frac", "upper_wick_frac", "lower_wick_frac",
    # breakout context
    "dist_high_168", "dist_low_168", "dist_high_336", "dist_low_336",
    # volume / flow
    "vol_z_24", "vol_z_168", "quote_vol_z_168", "taker_buy_frac", "taker_buy_frac_z_24",
    # calendar
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    # cross-market regime
    "btc_ret_24", "btc_ret_168", "btc_vol_100", "beta_btc_168",
    # derivatives sentiment
    "funding_rate", "funding_ret_24", "oi_change_24",
)


# --- causal primitives -------------------------------------------------------
# Each returns an array the same length as its input, NaN until enough history
# exists. None of them peek forward.

def _ema(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1.0)
    out = np.full(x.size, np.nan)
    acc = np.nan
    for i in range(x.size):
        v = x[i]
        if not np.isfinite(v):
            continue
        acc = v if not np.isfinite(acc) else acc + a * (v - acc)
        out[i] = acc
    return out


def _wilder(x: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (RSI/ATR), seeded with the first full-period mean."""
    out = np.full(x.size, np.nan)
    if x.size < period:
        return out
    seed = np.nanmean(x[:period])
    if not np.isfinite(seed):
        return out
    out[period - 1] = seed
    for i in range(period, x.size):
        prev, v = out[i - 1], x[i]
        out[i] = prev if not np.isfinite(v) else (prev * (period - 1) + v) / period
    return out


def _rolling(x: np.ndarray, w: int, fn) -> np.ndarray:
    out = np.full(x.size, np.nan)
    if x.size < w:
        return out
    # Explicit windows: cumulative tricks lose precision over 5 years of prices
    # and mishandle NaN warm-up, and this runs once per symbol.
    for i in range(w - 1, x.size):
        out[i] = fn(x[i - w + 1:i + 1])
    return out


def _rolling_mean(x, w):
    return _rolling(x, w, np.mean)


def _rolling_std(x, w):
    return _rolling(x, w, lambda s: np.std(s, ddof=1))


def _rolling_max(x, w):
    return _rolling(x, w, np.max)


def _rolling_min(x, w):
    return _rolling(x, w, np.min)


def _pct_change(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.size, np.nan)
    if x.size > n:
        prev = x[:-n]
        out[n:] = np.where(prev != 0, x[n:] / np.where(prev == 0, np.nan, prev) - 1.0, np.nan)
    return out


def _zscore(x: np.ndarray, w: int) -> np.ndarray:
    m, s = _rolling_mean(x, w), _rolling_std(x, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(s > 0, (x - m) / s, 0.0)


def _time_parts(bar_times) -> tuple[np.ndarray, np.ndarray]:
    """(hour_of_day, day_of_week) from timestamps, vectorised.

    Accepts numpy `datetime64` or an object array of `datetime` (which is what
    polars hands back, tz-aware). Timestamps are UTC throughout the project, so
    dropping the tzinfo is a representation change, not a shift.
    """
    t = np.asarray(bar_times)
    if t.dtype == object:
        t = np.array([x.replace(tzinfo=None) if getattr(x, "tzinfo", None) else x
                      for x in t], dtype="datetime64[ns]")
    hours = t.astype("datetime64[h]").astype(np.int64) % 24
    days = t.astype("datetime64[D]").astype(np.int64)
    # +3 puts Monday at 0, matching datetime.weekday(): 1970-01-01 was a Thursday.
    return hours.astype(float), ((days + 3) % 7).astype(float)


def _safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(np.isfinite(b) & (b != 0), a / np.where(b == 0, np.nan, b), np.nan)


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    d = np.diff(close, prepend=np.nan)
    gain = _wilder(np.where(d > 0, d, 0.0), period)
    loss = _wilder(np.where(d < 0, -d, 0.0), period)
    with np.errstate(invalid="ignore", divide="ignore"):
        rs = np.where(loss > 0, gain / np.where(loss == 0, np.nan, loss), np.inf)
    return 100.0 - 100.0 / (1.0 + rs)


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    prev = np.roll(close, 1)
    prev[0] = np.nan
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return _wilder(tr, period)


def realized_vol(close: np.ndarray, span: int = 100) -> np.ndarray:
    """EW stdev of log returns. Mirrors `labels.ewm_volatility` so the feature
    the model sees is the same quantity that sized its barriers."""
    from .labels import ewm_volatility
    return ewm_volatility(close, span=span)


def rolling_beta(x: np.ndarray, y: np.ndarray, w: int) -> np.ndarray:
    """Causal beta of returns `x` against reference returns `y`."""
    out = np.full(x.size, np.nan)
    for i in range(w - 1, x.size):
        a, b = x[i - w + 1:i + 1], y[i - w + 1:i + 1]
        ok = np.isfinite(a) & np.isfinite(b)
        if ok.sum() < w // 2:
            continue
        vb = np.var(b[ok])
        if vb > 0:
            out[i] = float(np.cov(a[ok], b[ok])[0, 1] / vb)
    return out


# --- the feature set ---------------------------------------------------------

def compute_features(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    volume: np.ndarray,
    *,
    quote_volume: np.ndarray | None = None,
    taker_buy_volume: np.ndarray | None = None,
    bar_times: np.ndarray | None = None,
    btc_close: np.ndarray | None = None,
    funding: np.ndarray | None = None,
    open_interest: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Causal features for one symbol on one bar grid.

    Optional inputs are filled with NaN when absent rather than dropped, so the
    column set never changes shape — a model trained with funding must not be
    handed a narrower matrix at inference time.
    """
    o, h, l, c, v = (np.asarray(a, dtype=float) for a in (open_, high, low, close, volume))
    n = c.size
    nan = np.full(n, np.nan)
    qv = np.asarray(quote_volume, dtype=float) if quote_volume is not None else nan
    tbv = np.asarray(taker_buy_volume, dtype=float) if taker_buy_volume is not None else nan

    f: dict[str, np.ndarray] = {}
    for k in (1, 6, 24, 72, 168, 336):
        f[f"ret_{k}"] = _pct_change(c, k)

    emas = {s: _ema(c, s) for s in (12, 26, 50, 200)}
    for s, e in emas.items():
        f[f"ema_dist_{s}"] = _safe_div(c - e, e)
    f["ema_12_26_spread"] = _safe_div(emas[12] - emas[26], emas[26])
    f["ema_50_200_spread"] = _safe_div(emas[50] - emas[200], emas[200])

    f["rsi_14"] = rsi(c, 14)
    macd = emas[12] - emas[26]
    f["macd_hist"] = _safe_div(macd - _ema(macd, 9), c)
    m20, s20 = _rolling_mean(c, 20), _rolling_std(c, 20)
    f["bb_pos_20"] = np.where(s20 > 0, (c - m20) / (2.0 * np.where(s20 == 0, np.nan, s20)), 0.0)

    f["atr_14_norm"] = _safe_div(atr(h, l, c, 14), c)
    vol100 = realized_vol(c, 100)
    f["vol_ewm_100"] = vol100
    f["vol_ratio_24_168"] = _safe_div(realized_vol(c, 24), realized_vol(c, 168))

    rng = h - l
    f["body_frac"] = _safe_div(c - o, rng)
    f["upper_wick_frac"] = _safe_div(h - np.maximum(o, c), rng)
    f["lower_wick_frac"] = _safe_div(np.minimum(o, c) - l, rng)

    for w in (168, 336):
        f[f"dist_high_{w}"] = _safe_div(c - _rolling_max(h, w), _rolling_max(h, w))
        f[f"dist_low_{w}"] = _safe_div(c - _rolling_min(l, w), _rolling_min(l, w))

    f["vol_z_24"] = _zscore(v, 24)
    f["vol_z_168"] = _zscore(v, 168)
    f["quote_vol_z_168"] = _zscore(qv, 168) if np.isfinite(qv).any() else nan
    tb_frac = _safe_div(tbv, v) if np.isfinite(tbv).any() else nan
    f["taker_buy_frac"] = tb_frac
    f["taker_buy_frac_z_24"] = _zscore(tb_frac, 24) if np.isfinite(tb_frac).any() else nan

    if bar_times is not None:
        hours, dows = _time_parts(bar_times)
        f["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
        f["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
        f["dow_sin"] = np.sin(2 * np.pi * dows / 7.0)
        f["dow_cos"] = np.cos(2 * np.pi * dows / 7.0)
    else:
        for k in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
            f[k] = nan

    if btc_close is not None:
        b = np.asarray(btc_close, dtype=float)
        f["btc_ret_24"] = _pct_change(b, 24)
        f["btc_ret_168"] = _pct_change(b, 168)
        f["btc_vol_100"] = realized_vol(b, 100)
        f["beta_btc_168"] = rolling_beta(
            np.diff(np.log(c), prepend=np.nan), np.diff(np.log(b), prepend=np.nan), 168)
    else:
        for k in ("btc_ret_24", "btc_ret_168", "btc_vol_100", "beta_btc_168"):
            f[k] = nan

    f["funding_rate"] = np.asarray(funding, dtype=float) if funding is not None else nan
    f["funding_ret_24"] = (_rolling_mean(np.asarray(funding, dtype=float), 24)
                           if funding is not None else nan)
    f["oi_change_24"] = (_pct_change(np.asarray(open_interest, dtype=float), 24)
                         if open_interest is not None else nan)

    missing = set(FEATURE_NAMES) - set(f)
    extra = set(f) - set(FEATURE_NAMES)
    if missing or extra:
        raise AssertionError(f"feature set drift: missing={missing} extra={extra}")
    return f


def feature_matrix(features: dict[str, np.ndarray]) -> np.ndarray:
    """Stack into `(n_bars, n_features)` in `FEATURE_NAMES` order.

    Always go through this rather than stacking a dict's values: column order is
    part of the model's contract, and a permutation produces no error, just a
    quietly worthless model.
    """
    return np.column_stack([features[name] for name in FEATURE_NAMES])


def usable_mask(X: np.ndarray, min_bar: int = MAX_LOOKBACK) -> np.ndarray:
    """Rows past warm-up whose features are all finite.

    Columns that are entirely NaN (an optional input that was not supplied) are
    ignored, so omitting funding does not discard every row.
    """
    all_nan_cols = np.all(~np.isfinite(X), axis=0)
    usable = X[:, ~all_nan_cols]
    ok = np.all(np.isfinite(usable), axis=1) if usable.size else np.ones(X.shape[0], bool)
    ok[:min_bar] = False
    return ok
