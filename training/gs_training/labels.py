"""Triple-barrier labeling (López de Prado) and the barrier scan it shares
with the backtester.

`scan_barriers` is deliberately the *only* implementation of "what happens to a
position once it is open". `labels.py` uses it to say what the model should have
predicted; `backtest.py` uses it to exit simulated trades. If those two ever
disagreed, the backtest would be scoring a strategy the model was never trained
for — the failure would look like a mysteriously good or bad backtest rather
than like a bug, so they share one function on purpose.

Conventions that matter, all of them chosen to be pessimistic:

- **The decision bar is not the entry bar.** A signal at the close of bar `i`
  enters at the *open* of bar `i+1`. Labeling from the close of `i` would assume
  a fill nobody can get.
- **Barrier widths come from volatility known at the decision bar**, never from
  the window being labeled.
- **A bar that touches both barriers counts as a loss.** OHLC does not record
  the path within a bar, so the order is genuinely unknown; assuming the good
  one first is how a backtest quietly invents money.
- **Gaps through the stop fill at the open, not at the barrier.** Gaps through
  the target still fill at the target. The asymmetry is intentional: real
  adverse gaps cost more than the stop, favourable ones rarely pay extra.

**Sides.** `BarrierSpec.side` is +1 for a long (spot or perp) and -1 for a
short (perp only — spot cannot short). A short is resolved by running the long
scan on the price series negated, so every pessimism rule above applies to it by
construction rather than through a second copy that could drift: for a short the
stop sits *above* entry, a same-bar double touch is still a loss, and a gap up
through the stop fills at the (higher) open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

WIN = 1
LOSS = -1
TIMEOUT = 0

# Returned for events with too little forward data to resolve honestly. Such
# rows must be dropped, never coerced to TIMEOUT — a label that says "no barrier
# was hit" when the window was merely truncated is a fabricated observation.
UNRESOLVED = -2

LONG = 1
SHORT = -1


@dataclass(frozen=True)
class BarrierSpec:
    """Barrier geometry, and the single definition of where the barriers are.

    `barrier_prices` is the only place the arithmetic lives, because both the
    labeller and the backtester need it and any drift between two copies would
    mean the backtest scored a strategy the model was never trained on.

    **Volatility is scaled to the horizon.** `sigma` is a per-bar estimate, but
    the position is held for up to `max_bars`; under a random walk the expected
    move over the window grows with sqrt(time). Feeding a per-bar sigma straight
    in puts the barriers roughly 18x too close on a 336-bar horizon, so every
    position resolves almost immediately and the vertical barrier never binds —
    the labels then describe a scalping strategy, not a swing one.
    """

    upper_mult: float = 1.0   # profit target, k·sigma_horizon in the trade's favour
    lower_mult: float = 0.5   # stop, k·sigma_horizon against the trade
    max_bars: int = 336       # vertical barrier (336 1h bars = 14 days)
    scale_by_horizon: bool = True
    side: int = LONG          # +1 long, -1 short

    def __post_init__(self) -> None:
        if self.upper_mult <= 0 or self.lower_mult <= 0:
            raise ValueError("barrier multiples must be positive")
        if self.max_bars < 1:
            raise ValueError("max_bars must be at least 1")
        if self.side not in (LONG, SHORT):
            raise ValueError(f"side must be {LONG} or {SHORT}, got {self.side!r}")

    def horizon_sigma(self, sigma: float) -> float:
        """Per-bar volatility scaled to the holding period."""
        return sigma * math.sqrt(self.max_bars) if self.scale_by_horizon else sigma

    def barrier_prices(self, entry_px: float, sigma: float) -> tuple[float, float]:
        """(target, stop) absolute prices for a position entered at `entry_px`.

        For a long that is (upper, lower); for a short the target is below
        entry and the stop above it.
        """
        s = self.horizon_sigma(sigma)
        return (entry_px * (1.0 + self.side * self.upper_mult * s),
                entry_px * (1.0 - self.side * self.lower_mult * s))


def ewm_volatility(close: np.ndarray, span: int = 100) -> np.ndarray:
    """Exponentially weighted stdev of log returns, as a per-bar fraction.

    Causal by construction: index `i` sees returns up to and including `i`.
    """
    close = np.asarray(close, dtype=float)
    out = np.full(close.shape, np.nan)
    if close.size < 2:
        return out
    r = np.diff(np.log(close), prepend=np.nan)
    alpha = 2.0 / (span + 1.0)
    mean = var = np.nan
    for i in range(1, close.size):
        x = r[i]
        if not np.isfinite(x):
            continue
        if not np.isfinite(mean):
            mean, var = x, 0.0
        else:
            delta = x - mean
            mean += alpha * delta
            var = (1 - alpha) * (var + alpha * delta * delta)
        out[i] = np.sqrt(var)
    return out


def scan_barriers(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    entry_i: int, entry_px: float, up_px: float, dn_px: float, max_bars: int,
    side: int = LONG,
) -> tuple[int, int, float]:
    """Resolve one position opened at `entry_px` on bar `entry_i`.

    `up_px`/`dn_px` are the (target, stop) pair from `barrier_prices` — for a
    short the target is the lower of the two. Returns `(outcome, exit_i,
    exit_px)`. Scanning starts at `entry_i` itself: entry is at that bar's open,
    so the rest of that bar can already resolve it.

    A short is the long scan on the negated series: negation swaps high and low,
    so "high >= target" becomes "low <= target" and the gap-through-stop rule
    becomes "open above the stop fills at the open". One code path, one set of
    pessimism rules.
    """
    if side == SHORT:
        outcome, exit_i, px = scan_barriers(
            -open_, -low, -high, -close, entry_i, -entry_px, -up_px, -dn_px, max_bars)
        return outcome, exit_i, -px
    if side != LONG:
        raise ValueError(f"side must be {LONG} or {SHORT}, got {side!r}")
    n = close.size
    if entry_i >= n:
        return UNRESOLVED, -1, np.nan
    last = entry_i + max_bars - 1
    if last >= n:
        # Not enough forward data for the vertical barrier to be meaningful.
        return UNRESOLVED, -1, np.nan

    hi = high[entry_i:last + 1]
    lo = low[entry_i:last + 1]
    hit_up = hi >= up_px
    hit_dn = lo <= dn_px

    first_up = int(np.argmax(hit_up)) if hit_up.any() else -1
    first_dn = int(np.argmax(hit_dn)) if hit_dn.any() else -1

    if first_dn == -1 and first_up == -1:
        return TIMEOUT, last, float(close[last])
    # Same-bar touch of both barriers resolves as a loss (path unknown).
    if first_dn != -1 and (first_up == -1 or first_dn <= first_up):
        i = entry_i + first_dn
        # A gap straight through the stop fills at the open, which is worse.
        px = min(dn_px, float(open_[i])) if open_[i] < dn_px else dn_px
        return LOSS, i, float(px)
    i = entry_i + first_up
    return WIN, i, float(up_px)


def label_events(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    sigma: np.ndarray, event_idx: np.ndarray, spec: BarrierSpec,
) -> dict[str, np.ndarray]:
    """Label decision bars `event_idx`; entry is the *next* bar's open.

    Returns arrays aligned with `event_idx`: `outcome`, `entry_idx`, `exit_idx`,
    `entry_px`, `exit_px`, `ret` (gross and signed by `spec.side`, so a
    profitable short is positive; fees are the backtester's business) and
    `label_end_idx` — the last bar each label depends on, which is what the
    purged walk-forward split needs to prevent leakage.
    """
    open_, high, low, close, sigma = (
        np.asarray(a, dtype=float) for a in (open_, high, low, close, sigma)
    )
    event_idx = np.asarray(event_idx, dtype=int)
    k = event_idx.size
    out = {
        "outcome": np.full(k, UNRESOLVED, dtype=int),
        "entry_idx": np.full(k, -1, dtype=int),
        "exit_idx": np.full(k, -1, dtype=int),
        "entry_px": np.full(k, np.nan),
        "exit_px": np.full(k, np.nan),
        "ret": np.full(k, np.nan),
        "label_end_idx": np.full(k, -1, dtype=int),
    }
    for j, i in enumerate(event_idx):
        entry_i = i + 1                      # decide at close of i, fill at open of i+1
        s = sigma[i]                         # volatility known at the decision bar
        if entry_i >= close.size or not np.isfinite(s) or s <= 0:
            continue
        entry_px = float(open_[entry_i])
        up_px, dn_px = spec.barrier_prices(entry_px, s)
        outcome, exit_i, exit_px = scan_barriers(
            open_, high, low, close, entry_i, entry_px, up_px, dn_px, spec.max_bars,
            spec.side,
        )
        if outcome == UNRESOLVED:
            continue
        out["outcome"][j] = outcome
        out["entry_idx"][j] = entry_i
        out["exit_idx"][j] = exit_i
        out["entry_px"][j] = entry_px
        out["exit_px"][j] = exit_px
        out["ret"][j] = spec.side * (exit_px / entry_px - 1.0)
        # The label is only *known* once the whole window has passed, even when
        # a barrier resolved it early: at decision time you could not have known
        # it would. Purging must assume the full horizon.
        out["label_end_idx"][j] = entry_i + spec.max_bars - 1
    return out


def cusum_events(close: np.ndarray, threshold: np.ndarray | float) -> np.ndarray:
    """Symmetric CUSUM filter — bar indices where price moved 'enough'.

    Sampling every bar makes neighbouring labels near-identical (a 14-day
    horizon on 1h bars overlaps 336-fold), which inflates the row count without
    adding information. This emits a bar only after cumulative drift since the
    last event exceeds `threshold`, so events are spaced by activity rather than
    by the clock.
    """
    close = np.asarray(close, dtype=float)
    thr = np.broadcast_to(np.asarray(threshold, dtype=float), close.shape)
    r = np.diff(np.log(close), prepend=np.nan)
    events, s_pos, s_neg = [], 0.0, 0.0
    for i in range(1, close.size):
        if not np.isfinite(r[i]) or not np.isfinite(thr[i]):
            continue
        s_pos = max(0.0, s_pos + r[i])
        s_neg = min(0.0, s_neg + r[i])
        if s_pos > thr[i]:
            s_pos = 0.0
            events.append(i)
        elif -s_neg > thr[i]:
            s_neg = 0.0
            events.append(i)
    return np.asarray(events, dtype=int)
