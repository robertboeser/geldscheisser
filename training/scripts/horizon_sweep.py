"""Horizon / barrier-geometry sweep — the analysis behind the HANDOVER decision.

Answers one question: what does the holding horizon actually buy or cost?
Because barriers scale as `sigma·sqrt(max_bars)`, shrinking the horizon shrinks
the barriers with it, so the *shape* of the label problem (win rate, timeout
rate, break-even probability) is nearly scale-invariant. Only two things really
move with the horizon — the effective sample size and the cost drag — and this
script measures both so the choice is made on numbers rather than on feel.

Also reports effective sample size two ways: as `train.py` currently weights it
(full horizon, via `label_end_idx`) and by the actual barrier-touch time. The
gap is the free sample size described in the HANDOVER.

Run from the repo root:
    .venv/bin/python training/scripts/horizon_sweep.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from gs_training.datasets import DEFAULT_CACHE, load_panel
from gs_training.labels import WIN, TIMEOUT, BarrierSpec, cusum_events, ewm_volatility, label_events
from gs_training.splits import average_uniqueness

# Binance spot taker, both sides, plus slippage each way — matches backtest.CostModel.
COST = 2 * 0.001 + 2 * 5.0 / 10_000.0

WARMUP = 250          # skip feature warm-up bars, as build_dataset does via usable_mask
CUSUM_MULT = 2.0
VOL_SPAN = 100


def measure(pre, spec: BarrierSpec) -> dict:
    """Label every symbol under `spec` and summarise the resulting sample."""
    n = 0
    eff_horizon = eff_actual = 0.0
    durs, outs, tgts = [], [], []
    for b, sg in pre.values():
        ev = cusum_events(b["close"], np.nan_to_num(sg, nan=0.0) * CUSUM_MULT)
        ev = ev[np.isfinite(sg[ev]) & (sg[ev] > 0)]
        ev = ev[ev > WARMUP]
        if ev.size == 0:
            continue
        lab = label_events(b["open"], b["high"], b["low"], b["close"], sg, ev, spec)
        ok = lab["outcome"] > -2                      # drop UNRESOLVED
        ev, lab = ev[ok], {k: v[ok] for k, v in lab.items()}
        if ev.size == 0:
            continue
        n += ev.size
        # Two uniqueness readings: what train.py uses today, and the honest one.
        eff_horizon += float(average_uniqueness(lab["label_end_idx"].astype(float), ev.astype(float)).sum())
        eff_actual += float(average_uniqueness(lab["exit_idx"].astype(float), ev.astype(float)).sum())
        durs.append(lab["exit_idx"] - lab["entry_idx"])
        outs.append(lab["outcome"])
        tgts.append(np.array([spec.horizon_sigma(sg[i]) for i in ev]))

    durs, outs, tgts = np.concatenate(durs), np.concatenate(outs), np.concatenate(tgts)
    sig = float(np.nanmedian(tgts))
    up, dn = sig * spec.upper_mult, sig * spec.lower_mult
    return {
        "n": n, "eff_horizon": eff_horizon, "eff_actual": eff_actual,
        "med_hold_d": float(np.median(durs)) / 24.0,
        "p90_hold_d": float(np.percentile(durs, 90)) / 24.0,
        "target_pct": up * 100.0, "stop_pct": dn * 100.0,
        "cost_over_target": COST / up * 100.0,
        "win_pct": float((outs == WIN).mean()) * 100.0,
        "timeout_pct": float((outs == TIMEOUT).mean()) * 100.0,
        # p(win) at which the EV rule breaks even: p·up − (1−p)·dn − cost = 0
        "breakeven_p": (dn + COST) / (up + dn) * 100.0,
    }


HEAD = (f"{'spec':>14} {'events':>7} {'eff(hz)':>8} {'eff(act)':>9} {'medhld':>7} {'p90hld':>7} "
        f"{'tgt%':>6} {'stop%':>6} {'cost/tgt':>9} {'win%':>6} {'t-o%':>6} {'EVbe%':>6}")


def row(name: str, r: dict) -> str:
    return (f"{name:>14} {r['n']:>7} {r['eff_horizon']:>8.0f} {r['eff_actual']:>9.0f} "
            f"{r['med_hold_d']:>6.1f}d {r['p90_hold_d']:>6.1f}d {r['target_pct']:>6.1f} "
            f"{r['stop_pct']:>6.1f} {r['cost_over_target']:>8.1f}% {r['win_pct']:>6.1f} "
            f"{r['timeout_pct']:>6.1f} {r['breakeven_p']:>6.1f}")


def main() -> None:
    from gs_common.config import load_settings

    settings = load_settings()
    p = argparse.ArgumentParser(description="Sweep holding horizon and barrier width")
    p.add_argument("--symbols", default=",".join(settings.symbols))
    p.add_argument("--interval", default="1h")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bar_times, panel = load_panel(symbols, args.interval, Path(args.cache_dir))
    print(f"grid: {len(bar_times)} {args.interval} bars x {len(symbols)} symbols "
          f"({len(bar_times) / 24 / 365.25:.2f} years)\n")
    pre = {s: (panel[s], ewm_volatility(panel[s]["close"], span=VOL_SPAN)) for s in symbols}

    print("A. Vertical barrier swept, geometry fixed at 1.0/0.5\n")
    print(HEAD); print("-" * len(HEAD))
    for mb, nm in [(12, "12h"), (24, "1d"), (48, "2d"), (84, "3.5d"), (120, "5d"),
                   (168, "7d"), (240, "10d"), (336, "14d"), (504, "21d")]:
        print(row(nm, measure(pre, BarrierSpec(1.0, 0.5, mb))))

    print("\nB. 14-day ceiling held fixed, barriers tightened\n")
    print(HEAD); print("-" * len(HEAD))
    for up, dn in [(1.0, 0.5), (0.75, 0.375), (0.5, 0.25), (0.5, 0.5), (0.35, 0.175)]:
        print(row(f"14d {up}/{dn}", measure(pre, BarrierSpec(up, dn, 336))))

    print("\nC. Leading candidates\n")
    print(HEAD); print("-" * len(HEAD))
    for mb, up, dn, nm in [(336, 1.0, 0.5, "14d 1.0/.5"), (336, 0.75, 0.375, "14d .75/.38"),
                           (168, 1.0, 0.5, "7d 1.0/.5"), (168, 0.75, 0.375, "7d .75/.38"),
                           (84, 1.0, 0.5, "3.5d 1.0/.5")]:
        print(row(nm, measure(pre, BarrierSpec(up, dn, mb))))


if __name__ == "__main__":
    main()
