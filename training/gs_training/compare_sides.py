"""Long vs short, on development data only.

The question is whether shorting would be the better swing trade. Answering it
honestly means separating three things that all move the headline number:

- **Direction** — what the market did. The development sample (2021 → 2025-09)
  is a strong bull overall but contains the 2022 bear, so each side gets a
  regime in which it should shine.
- **Venue** — a short is a USDⓈ-M perp (spot cannot short), which charges half
  spot's taker fee and pays or charges funding. Long-perp is run alongside
  long-spot so that a better short result can be split into "the venue is
  cheaper" and "the direction is better".
- **The model** — every variant is scored against random entries *on the same
  side* through the same barriers, over several seeds.

It runs in four views: label-level (no model: what does trading every event
on each side earn?), walk-forward per variant, a regime split by BTC versus its
200-day SMA, and stress tests. `--combined` additionally runs both sides'
signals through one shared portfolio.

**There is no holdout flag, on purpose.** The reserved holdout (from
`HOLDOUT_START`) is a drawdown — exactly where shorts would look best, and
therefore exactly where a peek would be most tempting and most costly.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .backtest import PERP, SPOT, Portfolio, Signal, Trade, run_backtest
from .baselines import _sma, random_entries
from .datasets import DEFAULT_CACHE, funding_settlements
from .labels import LONG, LOSS, SHORT, TIMEOUT, WIN, BarrierSpec, label_events
from .metrics import performance, stress_test
from .train import (
    Dataset,
    FoldResult,
    aggregate,
    build_dataset,
    costs_for,
    log_to_mlflow,
    slice_panel,
    train_walk_forward,
)

log = logging.getLogger(__name__)

REGIME_SMA_BARS = 200 * 24          # BTC 200-day SMA on 1h bars
SIDE_NAME = {LONG: "long", SHORT: "short"}


@dataclass(frozen=True)
class Variant:
    side: int
    venue: str

    @property
    def name(self) -> str:
        return f"{SIDE_NAME[self.side]}-{self.venue}"


VARIANTS = (Variant(LONG, SPOT), Variant(LONG, PERP), Variant(SHORT, PERP))


def btc_regime(panel: dict[str, dict[str, np.ndarray]]) -> np.ndarray:
    """+1 where BTC closed above its 200-day SMA, -1 below, 0 during warm-up.

    Causal: bar `i` uses closes up to and including `i`, and it is read at the
    decision bar, so it is information a live system would have had.
    """
    close = panel["BTCUSDT"]["close"]
    sma = _sma(close, REGIME_SMA_BARS)
    out = np.zeros(close.size, dtype=int)
    ok = np.isfinite(sma)
    out[ok] = np.where(close[ok] > sma[ok], 1, -1)
    return out


def _funding_flow(fund: np.ndarray, entry_i: int, exit_i: int, side: int) -> float:
    """Funding received per unit notional over a holding, on the backtester's
    rule: settlements on bars `entry_i < k <= exit_i`. Approximates the
    notional as constant (the backtester marks it at each settlement's open)."""
    return float(-side * fund[entry_i + 1:exit_i + 1].sum())


def label_view(ds: Dataset, panel, sigma, bar_times, spec: BarrierSpec,
               funding: dict[str, np.ndarray], regime: np.ndarray) -> dict:
    """What trading *every* event on one side earns, with no model and no
    portfolio limits — the drift-plus-geometry baseline any model must explain.

    Net return per event is the side-signed barrier return, minus perp round-trip
    costs, plus funding. Rows are equal-weighted; `eff_n` is the uniqueness-
    weighted count, the honest sample size.
    """
    costs = costs_for(PERP)
    cost = 2 * costs.fee_rate + 2 * costs.slippage_bps / 10_000
    rows = []
    for sym in np.unique(ds.symbol):
        m = ds.symbol == sym
        ev = ds.event_bar[m]
        b = panel[sym]
        lab = label_events(b["open"], b["high"], b["low"], b["close"], sigma[sym], ev, spec)
        assert (lab["outcome"] > -2).all(), "dataset events must all resolve"
        fund = np.array([_funding_flow(funding[sym], e, x, spec.side)
                         for e, x in zip(lab["entry_idx"], lab["exit_idx"])])
        years = bar_times[ev].astype("datetime64[Y]").astype(int) + 1970
        w = ds.weight[m]
        for j in range(ev.size):
            rows.append((str(sym), int(years[j]), int(regime[ev[j]]), int(lab["outcome"][j]),
                         float(lab["ret"][j]), float(fund[j]), float(w[j])))

    def summary(sel) -> dict:
        if not sel:
            return {"n": 0}
        out = np.array([r[3] for r in sel]); ret = np.array([r[4] for r in sel])
        fund = np.array([r[5] for r in sel]); w = np.array([r[6] for r in sel])
        net = ret - cost + fund
        return {
            "n": len(sel), "eff_n": float(w.sum()),
            "win": float(np.mean(out == WIN)), "loss": float(np.mean(out == LOSS)),
            "timeout": float(np.mean(out == TIMEOUT)),
            "gross": float(ret.mean()), "funding": float(fund.mean()), "net": float(net.mean()),
            # Uniqueness-weighted mean, so a burst of overlapping events in one
            # week does not dominate.
            "net_w": float(np.average(net, weights=w)) if w.sum() > 0 else float("nan"),
        }

    return {
        "all": summary(rows),
        "by_year": {y: summary([r for r in rows if r[1] == y])
                    for y in sorted({r[1] for r in rows})},
        "by_symbol": {s: summary([r for r in rows if r[0] == s])
                      for s in sorted({r[0] for r in rows})},
        "by_regime": {name: summary([r for r in rows if r[2] == k])
                      for k, name in ((1, "btc_above_sma200"), (-1, "btc_below_sma200"))},
    }


def baseline_band(results: list[FoldResult], panel, sigma, spec, costs, funding,
                  seeds: int) -> list[dict]:
    """Same-side random entries over `seeds` seeds per fold, same signal count.

    A single random draw is luck (a 63-point band across five seeds is in the
    checklist); the band is what a result has to clear.
    """
    out = []
    n_all = len(next(iter(panel.values()))["close"])
    for r in results:
        lo, hi = r.window
        if r.n_signals == 0 or hi <= lo:
            out.append({"mean": float("nan"), "min": float("nan"), "max": float("nan")})
            continue
        sub, sub_sigma = slice_panel(panel, lo, hi), {s: v[lo:hi] for s, v in sigma.items()}
        sub_f = None if funding is None else {s: v[lo:hi] for s, v in funding.items()}
        rets = [performance(run_backtest(
            sub, random_entries(r.n_signals, min(hi, n_all) - lo, sorted(panel),
                                seed=1000 * (r.fold + 1) + k, side=spec.side),
            spec, sub_sigma, costs, funding=sub_f))["total_return"] for k in range(seeds)]
        out.append({"mean": float(np.mean(rets)), "min": float(np.min(rets)),
                    "max": float(np.max(rets))})
    return out


def regime_split(trades: list[Trade], regime: np.ndarray) -> dict:
    """Model trades grouped by the BTC regime at their decision bar."""
    out = {}
    for k, name in ((1, "btc_above_sma200"), (-1, "btc_below_sma200"), (0, "warmup")):
        sel = [t for t in trades if regime[t.entry_i - 1] == k]
        pnl = np.array([t.pnl for t in sel])
        out[name] = {
            "trades": len(sel),
            "pnl": float(pnl.sum()) if sel else 0.0,
            "avg_pnl": float(pnl.mean()) if sel else 0.0,
            "hit_rate": float(np.mean([t.outcome == WIN for t in sel])) if sel else 0.0,
            "funding": float(sum(t.funding for t in sel)),
        }
    return out


def fold_stress(results: list[FoldResult], panel, sigma, spec, costs, funding) -> dict:
    """`metrics.stress_test` per traded fold, reported as fold-mean returns."""
    acc: dict[str, list[float]] = {}
    for r in results:
        if r.n_signals == 0:
            continue
        lo, hi = r.window
        sigs = [Signal(bar=s.bar - lo, symbol=s.symbol, side=s.side) for s in r.signals]
        st = stress_test(slice_panel(panel, lo, hi), sigs, spec,
                         {s: v[lo:hi] for s, v in sigma.items()}, costs,
                         funding=None if funding is None else
                         {s: v[lo:hi] for s, v in funding.items()})
        # P&L over starting cash rather than `total_return`: the drop-best
        # variant keeps the base run's equity curve, so only its P&L moves.
        for k, v in st.items():
            acc.setdefault(k, []).append(v["total_pnl"] / Portfolio().starting_cash)
    return {k: float(np.mean(v)) for k, v in acc.items()}


def combined(long_res: list[FoldResult], short_res: list[FoldResult], panel, sigma,
             spec, funding) -> list[dict]:
    """Both perp models' signals through one portfolio, per fold.

    Folds line up because both datasets share the same events (CUSUM sampling
    and label resolution do not depend on the side), which is asserted.
    """
    costs = costs_for(PERP)
    out = []
    for lr, sr in zip(long_res, short_res):
        assert lr.fold == sr.fold and lr.window == sr.window, "sides must share folds"
        lo, hi = lr.window
        sigs = [Signal(bar=s.bar - lo, symbol=s.symbol, side=s.side)
                for s in sorted(lr.signals + sr.signals, key=lambda s: s.bar)]
        perf = performance(run_backtest(
            slice_panel(panel, lo, hi), sigs, spec, {s: v[lo:hi] for s, v in sigma.items()},
            costs, funding={s: v[lo:hi] for s, v in funding.items()}))
        out.append({"fold": lr.fold, "signals": len(sigs), "long_signals": len(lr.signals),
                    "short_signals": len(sr.signals), **perf})
    return out


def _pct(x: float) -> str:
    return "     n/a" if not np.isfinite(x) else f"{x:>+8.1%}"


def _print_label_view(name: str, v: dict) -> None:
    print(f"\n=== label view: {name} (every event, perp costs, no model) ===")
    print(f"{'':>18} {'n':>7} {'eff_n':>6} {'win':>6} {'loss':>6} {'t/o':>6} "
          f"{'gross':>8} {'fund':>7} {'net':>8} {'net_w':>8}")

    def row(label, s):
        if not s.get("n"):
            print(f"{label:>18}       0"); return
        print(f"{label:>18} {s['n']:>7,} {s['eff_n']:>6.0f} {s['win']:>6.1%} {s['loss']:>6.1%} "
              f"{s['timeout']:>6.1%} {s['gross']:>+8.2%} {s['funding']:>+7.2%} "
              f"{s['net']:>+8.2%} {s['net_w']:>+8.2%}")
    row("all", v["all"])
    for k, s in v["by_regime"].items():
        row(k, s)
    for y, s in v["by_year"].items():
        row(str(y), s)
    for sym, s in v["by_symbol"].items():
        row(sym, s)


def main() -> None:
    from gs_common.config import load_settings
    from gs_common.log import setup_logging

    settings = load_settings()
    p = argparse.ArgumentParser(prog="gs-compare-sides",
                                description="Compare long and short on development data")
    p.add_argument("--symbols", default=",".join(settings.symbols))
    p.add_argument("--interval", default="1h")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    p.add_argument("--splits", type=int, default=5)
    p.add_argument("--max-bars", type=int, default=336)
    p.add_argument("--upper", type=float, default=1.0)
    p.add_argument("--lower", type=float, default=0.5)
    p.add_argument("--ev-threshold", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--baseline-seeds", type=int, default=5)
    p.add_argument("--combined", action="store_true",
                   help="also run long-perp + short-perp signals through one portfolio")
    p.add_argument("--mlflow-uri", default="http://localhost:5001")
    p.add_argument("--comparison-id", default="long-vs-short")
    p.add_argument("--out", help="write the full report as JSON here")
    args = p.parse_args()

    setup_logging(settings.log_level)
    symbols = [s.strip() for s in args.symbols.split(",")]
    cache = Path(args.cache_dir)
    specs = {side: BarrierSpec(args.upper, args.lower, args.max_bars, side=side)
             for side in (LONG, SHORT)}

    # Development only — see the module docstring for why there is no flag.
    built = {side: build_dataset(symbols, args.interval, cache, specs[side], split="dev")
             for side in (LONG, SHORT)}
    ds_l, bar_times, panel, sigma = built[LONG]
    ds_s = built[SHORT][0]
    if not (np.array_equal(ds_l.event_bar, ds_s.event_bar)
            and np.array_equal(ds_l.symbol, ds_s.symbol)):
        raise AssertionError("long and short datasets must share the same events")
    funding = funding_settlements(bar_times, symbols, cache)
    if funding is None:
        p.error("perp variants need funding snapshots — run `python -m gs_training.datasets`")
    regime = btc_regime(panel)
    dev_end = bar_times[int(ds_l.label_end_bar.max())]
    print(f"development events: {len(ds_l):,} (effective n {ds_l.weight.sum():.0f}), "
          f"{bar_times[int(ds_l.event_bar.min())]} .. labels resolved by {dev_end}")
    print("perp prices are approximated by spot klines (no perp OHLC in the archive)")

    report: dict = {"label_view": {}, "variants": {}}
    for side, ds in ((LONG, ds_l), (SHORT, ds_s)):
        v = label_view(ds, panel, sigma, bar_times, specs[side], funding, regime)
        report["label_view"][SIDE_NAME[side]] = v
        _print_label_view(SIDE_NAME[side], v)

    fold_results: dict[str, list[FoldResult]] = {}
    for var in VARIANTS:
        ds = ds_l if var.side == LONG else ds_s
        spec, costs = specs[var.side], costs_for(var.venue)
        fund = funding if var.venue == PERP else None
        res = train_walk_forward(ds, panel, sigma, spec, n_splits=args.splits,
                                 ev_threshold=args.ev_threshold, costs=costs,
                                 seed=args.seed, funding=fund)
        fold_results[var.name] = res
        band = baseline_band(res, panel, sigma, spec, costs, fund, args.baseline_seeds)
        trades = [t for r in res for t in r.trades]
        summary = aggregate(res)
        report["variants"][var.name] = {
            "summary": summary,
            "folds": [{
                "fold": r.fold, "from": str(bar_times[r.window[0]]),
                "to": str(bar_times[min(r.window[1], bar_times.size) - 1]),
                "auc": r.auc, "signals": r.n_signals, "return": r.strategy["total_return"],
                "baseline_return": r.baseline["total_return"], "band": b,
                "profit_factor": r.strategy["profit_factor"],
                "max_drawdown": r.strategy["max_drawdown"],
                "funding": r.strategy["total_funding"], "fees": r.strategy["total_fees"],
            } for r, b in zip(res, band)],
            "regime": regime_split(trades, regime),
            "stress": fold_stress(res, panel, sigma, spec, costs, fund),
        }

        print(f"\n=== {var.name} (fee {costs.fee_rate:.2%}/side) ===")
        print(f"{'fold':>4} {'from':>10} {'to':>10} {'auc':>6} {'sig':>5} {'return':>8} "
              f"{'rand μ':>8} {'rand min':>8} {'rand max':>8} {'PF':>5} {'maxDD':>6} {'funding':>8}")
        for f in report["variants"][var.name]["folds"]:
            print(f"{f['fold']:>4} {f['from'][:10]:>10} {f['to'][:10]:>10} {f['auc']:>6.3f} "
                  f"{f['signals']:>5} {_pct(f['return'])} {_pct(f['band']['mean'])} "
                  f"{_pct(f['band']['min'])} {_pct(f['band']['max'])} "
                  f"{f['profit_factor']:>5.2f} {f['max_drawdown']:>6.1%} {f['funding']:>+8.1f}")
        for k in ("folds_traded", "folds_no_signal", "mean_return", "mean_baseline_return",
                  "beat_baseline_folds", "mean_profit_factor", "mean_auc"):
            if k in summary:
                print(f"  {k:22} {summary[k]:>9.4f}")
        print("  regime (model trades):")
        for k, s in report["variants"][var.name]["regime"].items():
            if s["trades"]:
                print(f"    {k:18} trades={s['trades']:>4} pnl={s['pnl']:>+9.1f} "
                      f"avg={s['avg_pnl']:>+7.2f} hit={s['hit_rate']:.1%} funding={s['funding']:>+7.1f}")
        print("  stress (mean fold return):",
              ", ".join(f"{k}={v:+.1%}" for k, v in report["variants"][var.name]["stress"].items()))

        log_to_mlflow(args.mlflow_uri, f"{args.comparison_id}:{var.name}", {
            "comparison": args.comparison_id, "side": SIDE_NAME[var.side], "venue": var.venue,
            "fee_rate": costs.fee_rate, "symbols": args.symbols, "interval": args.interval,
            "splits": args.splits, "upper_mult": args.upper, "lower_mult": args.lower,
            "max_bars": args.max_bars, "ev_threshold": args.ev_threshold, "seed": args.seed,
            "split": "dev", "n_events": len(ds), "effective_n": round(float(ds.weight.sum())),
        }, res, summary)

    if args.combined:
        comb = combined(fold_results["long-perp"], fold_results["short-perp"],
                        panel, sigma, specs[LONG], funding)
        report["combined"] = comb
        print("\n=== combined long-perp + short-perp, one portfolio ===")
        for c in comb:
            print(f"{c['fold']:>4} signals={c['signals']:>4} (L{c['long_signals']}/S{c['short_signals']}) "
                  f"return={c['total_return']:>+7.1%} PF={c['profit_factor']:.2f} "
                  f"maxDD={c['max_drawdown']:.1%} funding={c['total_funding']:+.1f} "
                  f"skipped={c['skipped_no_slot'] + c['skipped_no_cash']:.0f}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=float))
        print(f"\nreport written to {args.out}")


if __name__ == "__main__":
    main()
