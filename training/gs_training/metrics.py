"""Performance metrics, plus the stress tests a result has to survive.

Accuracy is not here on purpose. A classifier can be right most of the time and
still lose money if the rare misses are large, so everything below is stated in
money or in risk-adjusted money, net of fees.

`stress_test` matters as much as the headline numbers. An edge that evaporates
when fees double, entry is delayed one bar, or the five best trades are removed
was never an edge — it was a handful of lucky fills. PLAN.md makes surviving
these a gate, so they are part of the harness rather than an afterthought.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .backtest import (
    BacktestResult,
    CostModel,
    Portfolio,
    Signal,
    run_backtest,
    summarize,
)
from .labels import BarrierSpec

BARS_PER_YEAR = 24 * 365  # 1h decision bars


def max_drawdown(equity: np.ndarray) -> float:
    """Worst peak-to-trough fall, as a positive fraction."""
    eq = np.asarray(equity, dtype=float)
    eq = eq[np.isfinite(eq)]
    if eq.size == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    return float(np.max((peak - eq) / peak)) if np.all(peak > 0) else 0.0


def profit_factor(pnls: np.ndarray) -> float:
    """Gross wins / gross losses. `inf` when there are no losses at all."""
    p = np.asarray(pnls, dtype=float)
    gains = p[p > 0].sum()
    losses = -p[p < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def sharpe(equity: np.ndarray, bars_per_year: int = BARS_PER_YEAR) -> float:
    """Annualised Sharpe of bar-to-bar equity returns, zero risk-free rate."""
    eq = np.asarray(equity, dtype=float)
    eq = eq[np.isfinite(eq)]
    if eq.size < 3:
        return 0.0
    r = np.diff(eq) / eq[:-1]
    sd = r.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(r.mean() / sd * np.sqrt(bars_per_year))


def sortino(equity: np.ndarray, bars_per_year: int = BARS_PER_YEAR) -> float:
    """Like Sharpe but penalising only downside deviation."""
    eq = np.asarray(equity, dtype=float)
    eq = eq[np.isfinite(eq)]
    if eq.size < 3:
        return 0.0
    r = np.diff(eq) / eq[:-1]
    downside = r[r < 0]
    if downside.size == 0:
        return float("inf") if r.mean() > 0 else 0.0
    dd = downside.std(ddof=1)
    if dd == 0:
        return 0.0
    return float(r.mean() / dd * np.sqrt(bars_per_year))


def performance(result: BacktestResult, bars_per_year: int = BARS_PER_YEAR) -> dict[str, float]:
    """Full report: counts from `summarize` plus risk-adjusted measures."""
    pnls = np.array([t.pnl for t in result.trades]) if result.trades else np.zeros(0)
    return summarize(result) | {
        "profit_factor": profit_factor(pnls),
        "max_drawdown": max_drawdown(result.equity),
        "sharpe": sharpe(result.equity, bars_per_year),
        "sortino": sortino(result.equity, bars_per_year),
    }


def stress_test(
    bars: dict[str, dict[str, np.ndarray]],
    signals: list[Signal],
    spec: BarrierSpec,
    sigma: dict[str, np.ndarray],
    costs: CostModel = CostModel(),
    portfolio: Portfolio = Portfolio(),
    drop_best: int = 5,
) -> dict[str, dict[str, float]]:
    """Re-run the backtest under adversarial variations.

    - `double_fees` — the edge must not live inside the fee assumption.
    - `delayed_entry` — one extra bar between signal and fill. Anything that
      dies here was really trading a reaction the model cannot capture live.
    - `drop_best_n` — remove the largest winners. If the result depends on a
      handful of trades, it is a sample of lucky fills, not an edge.
    """
    base = run_backtest(bars, signals, spec, sigma, costs, portfolio)
    out = {"base": performance(base)}

    out["double_fees"] = performance(run_backtest(
        bars, signals, spec, sigma, replace(costs, fee_rate=costs.fee_rate * 2), portfolio))

    delayed = [Signal(bar=s.bar + 1, symbol=s.symbol) for s in signals]
    out["delayed_entry"] = performance(run_backtest(
        bars, delayed, spec, sigma, costs, portfolio))

    trimmed = BacktestResult(
        trades=sorted(base.trades, key=lambda t: t.pnl)[:-drop_best] if drop_best else list(base.trades),
        # Equity is left as-is: removing trades retroactively would need a
        # re-simulation, and the honest question here is only whether total P&L
        # survives, so equity-derived metrics are reported from the base run.
        equity=base.equity,
        skipped_no_slot=base.skipped_no_slot,
        skipped_no_cash=base.skipped_no_cash,
        starting_cash=base.starting_cash,
    )
    out[f"drop_best_{drop_best}"] = performance(trimmed)
    return out
