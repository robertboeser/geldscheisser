"""Event-driven backtest over walk-forward test segments.

Deliberately not vectorised. A vectorised "sum the labelled returns" backtest
silently assumes infinite capital, no concurrency limit, and simultaneous entry
into every signal — which is how a strategy that cannot be traded produces a
beautiful equity curve. This walks bars in order and can only open a position
when capital and a slot are actually free.

What is modelled, all pessimistically:

- **Entry on the bar after the signal, at its open**, plus slippage. Deciding at
  a bar's close and filling at that same close is not achievable.
- **Exits via `labels.scan_barriers`** — the identical function that defined the
  training label, so the backtest scores the strategy the model was trained for.
- **Taker fees both ways.** Spot is 0.1% per side by default; PLAN.md's target
  of paying maker on entry is an optimisation to prove later, not to assume now.
- **Position and concurrency caps**, so a signal arriving with no free slot is
  skipped rather than silently taken.

Skipped signals are counted, not discarded: a strategy that only works when it
can hold thirty simultaneous positions is a different strategy from the one the
labels describe, and the skip count is how that shows up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .labels import LOSS, TIMEOUT, UNRESOLVED, WIN, BarrierSpec, scan_barriers


@dataclass(frozen=True)
class CostModel:
    """Trading frictions. Defaults are Binance spot taker, deliberately plain."""

    fee_rate: float = 0.001        # per side, 0.1% taker
    slippage_bps: float = 5.0      # each way, on top of the fill price

    def buy_price(self, px: float) -> float:
        return px * (1.0 + self.slippage_bps / 10_000.0)

    def sell_price(self, px: float) -> float:
        return px * (1.0 - self.slippage_bps / 10_000.0)


@dataclass(frozen=True)
class Portfolio:
    starting_cash: float = 10_000.0
    position_frac: float = 0.10    # of *starting* equity per position
    max_positions: int = 5


@dataclass
class Trade:
    symbol: str
    entry_i: int
    exit_i: int
    entry_px: float
    exit_px: float
    qty: float
    outcome: int
    fees: float

    @property
    def pnl(self) -> float:
        return (self.exit_px - self.entry_px) * self.qty - self.fees

    @property
    def gross_return(self) -> float:
        return self.exit_px / self.entry_px - 1.0


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity: np.ndarray = field(default_factory=lambda: np.zeros(0))
    skipped_no_slot: int = 0
    skipped_no_cash: int = 0
    starting_cash: float = 0.0

    @property
    def final_equity(self) -> float:
        return float(self.equity[-1]) if self.equity.size else self.starting_cash


@dataclass(frozen=True)
class Signal:
    """A decision to enter, taken at the close of bar `bar`."""

    bar: int
    symbol: str


def run_backtest(
    bars: dict[str, dict[str, np.ndarray]],
    signals: list[Signal],
    spec: BarrierSpec,
    sigma: dict[str, np.ndarray],
    costs: CostModel = CostModel(),
    portfolio: Portfolio = Portfolio(),
) -> BacktestResult:
    """Simulate `signals` over aligned per-symbol OHLC series.

    `bars[symbol]` holds equal-length `open/high/low/close` arrays on a shared
    bar grid (index `i` is the same instant for every symbol). `sigma[symbol]`
    is the volatility estimate used to size barriers, read at the *signal* bar.
    """
    if not bars:
        return BacktestResult(starting_cash=portfolio.starting_cash)
    n_bars = len(next(iter(bars.values()))["close"])
    for sym, b in bars.items():
        if any(len(b[c]) != n_bars for c in ("open", "high", "low", "close")):
            raise ValueError(f"{sym}: OHLC arrays must share one bar grid")

    by_bar: dict[int, list[Signal]] = {}
    for s in signals:
        by_bar.setdefault(s.bar, []).append(s)

    cash = portfolio.starting_cash
    notional = portfolio.starting_cash * portfolio.position_frac
    open_trades: list[Trade] = []
    result = BacktestResult(starting_cash=portfolio.starting_cash)
    equity = np.full(n_bars, np.nan)

    for i in range(n_bars):
        # 1. Close anything whose barrier resolved on this bar, freeing capital
        #    before new entries are considered.
        still_open = []
        for t in open_trades:
            if t.exit_i == i:
                cash += t.exit_px * t.qty * (1.0 - costs.fee_rate)
                result.trades.append(t)
            else:
                still_open.append(t)
        open_trades = still_open

        # 2. Act on signals from the *previous* bar's close.
        for sig in by_bar.get(i - 1, []):
            if len(open_trades) >= portfolio.max_positions:
                result.skipped_no_slot += 1
                continue
            if cash < notional:
                result.skipped_no_cash += 1
                continue
            b = bars[sig.symbol]
            s = sigma[sig.symbol][sig.bar]
            if not np.isfinite(s) or s <= 0:
                continue
            raw_entry = float(b["open"][i])
            entry_px = costs.buy_price(raw_entry)
            # Same geometry the labeller used — one definition, no drift.
            up_px, dn_px = spec.barrier_prices(raw_entry, s)
            outcome, exit_i, exit_raw = scan_barriers(
                b["open"], b["high"], b["low"], b["close"],
                i, raw_entry, up_px, dn_px, spec.max_bars,
            )
            if outcome == UNRESOLVED:
                continue  # not enough forward data left in this segment
            qty = notional / entry_px
            entry_fee = entry_px * qty * costs.fee_rate
            exit_px = costs.sell_price(float(exit_raw))
            exit_fee = exit_px * qty * costs.fee_rate
            cash -= entry_px * qty * (1.0 + costs.fee_rate)
            trade = Trade(
                symbol=sig.symbol, entry_i=i, exit_i=exit_i,
                entry_px=entry_px, exit_px=exit_px, qty=qty,
                outcome=outcome, fees=entry_fee + exit_fee,
            )
            if exit_i == i:
                # Resolved within its own entry bar. Step 1 already ran for this
                # bar, so leaving it to the next iteration would never close it:
                # it would hold capital to the end of the run and leak into
                # equity as an unrealised mark that never becomes P&L.
                cash += exit_px * qty * (1.0 - costs.fee_rate)
                result.trades.append(trade)
            else:
                open_trades.append(trade)

        # 3. Mark to market.
        held = sum(float(bars[t.symbol]["close"][i]) * t.qty for t in open_trades)
        equity[i] = cash + held

    # `scan_barriers` only resolves trades whose exit lands inside the series,
    # so every position must have closed. If one has not, equity and realised
    # P&L have silently diverged — and the equity curve is what gets reported.
    if open_trades:
        raise AssertionError(
            f"{len(open_trades)} position(s) still open at the final bar; "
            "equity and realised P&L have diverged"
        )
    result.equity = equity
    return result


def summarize(result: BacktestResult) -> dict[str, float]:
    """Headline counts. Risk/return metrics live in `metrics.py`."""
    trades = result.trades
    wins = [t for t in trades if t.outcome == WIN]
    losses = [t for t in trades if t.outcome == LOSS]
    timeouts = [t for t in trades if t.outcome == TIMEOUT]
    pnl = np.array([t.pnl for t in trades]) if trades else np.zeros(0)
    return {
        "trades": float(len(trades)),
        "wins": float(len(wins)),
        "losses": float(len(losses)),
        "timeouts": float(len(timeouts)),
        "hit_rate": float(len(wins) / len(trades)) if trades else 0.0,
        "total_pnl": float(pnl.sum()),
        "total_fees": float(sum(t.fees for t in trades)),
        "avg_pnl": float(pnl.mean()) if trades else 0.0,
        "final_equity": result.final_equity,
        "total_return": (result.final_equity / result.starting_cash - 1.0)
                        if result.starting_cash else 0.0,
        "skipped_no_slot": float(result.skipped_no_slot),
        "skipped_no_cash": float(result.skipped_no_cash),
    }
