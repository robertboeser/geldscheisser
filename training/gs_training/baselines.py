"""Signal generators that any model has to beat.

These are not throwaway comparisons. PLAN.md makes beating them a gate, and two
of them are really tests of the harness rather than of a strategy:

- **`random_entries`** uses the same exit rule and the same trade count as the
  strategy under test. If a model cannot beat coin-flip entries through the same
  barriers, its apparent edge belongs to the *exit* rule, not to the model. This
  is the single most useful control here.
- **`buy_and_hold`** is the real opportunity cost. In a bull sample almost any
  long-biased rule looks profitable; the question is whether it beat doing
  nothing.
- **`sma_cross`** is the cheapest plausible rule a human would write. A model
  that cannot beat two moving averages is not earning its complexity.

All of them emit `Signal`s, so they run through the identical backtester with
identical costs — comparing a strategy measured one way against a baseline
measured another is the classic way to fool yourself.
"""

from __future__ import annotations

import numpy as np

from .backtest import Signal


def sma_cross(close: np.ndarray, symbol: str, fast: int = 50, slow: int = 200) -> list[Signal]:
    """Signal on the bar where the fast SMA crosses above the slow SMA."""
    close = np.asarray(close, dtype=float)
    if close.size < slow + 1:
        return []
    f = _sma(close, fast)
    s = _sma(close, slow)
    valid = np.isfinite(f) & np.isfinite(s)
    above = np.zeros(close.size, dtype=bool)
    above[valid] = f[valid] > s[valid]
    # Cross = first bar of an above-run; index i compares against i-1.
    cross = np.zeros(close.size, dtype=bool)
    cross[1:] = above[1:] & ~above[:-1]
    return [Signal(bar=int(i), symbol=symbol) for i in np.flatnonzero(cross)]


def _sma(x: np.ndarray, window: int) -> np.ndarray:
    out = np.full(x.size, np.nan)
    if x.size < window:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[window - 1:] = (c[window:] - c[:-window]) / window
    return out


def buy_and_hold(symbol: str, first_bar: int = 0) -> list[Signal]:
    """A single entry at the start. The exit is still the barrier rule, so this
    is 'hold until a barrier resolves', not 'hold forever' — the honest
    comparison against a strategy that must also exit."""
    return [Signal(bar=first_bar, symbol=symbol)]


def random_entries(
    n_signals: int, n_bars: int, symbols: list[str], seed: int = 0,
    first_bar: int = 0, last_bar: int | None = None, side: int | None = None,
) -> list[Signal]:
    """`n_signals` uniformly random entries — the same-exit-rule control.

    Seeded, because a baseline that changes between runs cannot be a baseline.
    Compare against it across several seeds: a single random draw has enough
    variance to flatter or damn a model by luck.

    `side` must match the strategy under test (`None` follows the backtest's
    spec). A short model scored against random *longs* measures the market's
    drift, not the model.
    """
    hi = n_bars if last_bar is None else min(last_bar, n_bars)
    if hi <= first_bar or not symbols or n_signals <= 0:
        return []
    rng = np.random.default_rng(seed)
    bars = rng.integers(first_bar, hi, size=n_signals)
    picks = rng.integers(0, len(symbols), size=n_signals)
    return [Signal(bar=int(b), symbol=symbols[int(p)], side=side) for b, p in zip(bars, picks)]
