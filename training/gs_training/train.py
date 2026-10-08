"""LightGBM training over purged walk-forward folds, evaluated by the backtester.

The shape of an honest run, and why each step is here:

1. **Assemble** features + triple-barrier labels per symbol onto one bar grid.
2. **Weight** each sample by `average_uniqueness`. A 14-day horizon on 1h bars
   overlaps ~336-fold; unweighted, the model treats ~1,456 effective
   observations as ~79,000 and overfits while every metric still looks fine.
3. **Split** walk-forward with purge + embargo, asserting no leakage per fold.
4. **Fit** on the earlier part of each training block, **calibrate** on the tail
   of it. Calibration needs data the model has not fitted, and it must not come
   from the test block — carving it out of train keeps both true.
5. **Decide** with expected value, not with `p > 0.5`. A raw probability is not
   a trading rule; the barrier geometry and the fees are what turn it into one.
6. **Score** with the backtester, against a same-count random-entry baseline run
   in the same window. Accuracy and AUC are reported but are not the verdict —
   a classifier can be right most of the time and still lose money.

**Sides.** `--side short` trains a separate model whose target is the short
barrier pair (`BarrierSpec.side = -1`): price falling to the target before
rising to the stop. Shorts only exist on the perp venue, where fees are lower
and funding is a real cash flow, so `--venue perp` also books funding from the
archive. The random baseline always trades the same side as the model.

**Timeouts count as not-win.** The classifier is binary: did the profit barrier
get hit first. Expected value then assumes a non-win pays the full stop, which
is pessimistic — a timeout usually exits nearer to flat than to the stop. That
bias is deliberate and in the safe direction.
"""

from __future__ import annotations

import argparse
import logging
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from .backtest import PERP, CostModel, Portfolio, Signal, Trade, run_backtest
from .baselines import random_entries
from .datasets import DEFAULT_CACHE, align_funding, funding_settlements, load_panel
from .features import (
    FEATURE_NAMES,
    MAX_LOOKBACK,
    compute_features,
    cross_sectional,
    feature_matrix,
    usable_mask,
)
from .labels import LONG, SHORT, WIN, BarrierSpec, cusum_events, ewm_volatility, label_events
from .metrics import performance
from .splits import (
    HOLDOUT_START,
    assert_no_leakage,
    average_uniqueness,
    holdout_split,
    walk_forward,
)

log = logging.getLogger(__name__)


@dataclass
class Dataset:
    """Model matrix plus everything needed to split and evaluate it honestly."""

    X: np.ndarray                 # (n, n_features)
    y: np.ndarray                 # 1 = profit barrier (for the spec's side) hit first
    event_bar: np.ndarray         # decision bar on the shared grid
    label_end_bar: np.ndarray     # last bar the label depends on (for purging)
    weight: np.ndarray            # average uniqueness, per symbol
    symbol: np.ndarray            # symbol per row
    up_ret: np.ndarray            # target / stop distances at the decision bar,
    dn_ret: np.ndarray            # as positive fractions on either side; for the EV rule
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def __len__(self) -> int:
        return self.X.shape[0]

    def select(self, mask: np.ndarray) -> "Dataset":
        """Row subset, keeping every parallel array in step.

        Uniqueness weights are **not** recomputed. They describe how much of its
        label window each event shares with its neighbours in the full sample,
        and that is the honest figure: dropping the neighbours from the matrix
        does not make the remaining events more independent than they were.
        """
        mask = np.asarray(mask)
        return Dataset(
            X=self.X[mask], y=self.y[mask], event_bar=self.event_bar[mask],
            label_end_bar=self.label_end_bar[mask], weight=self.weight[mask],
            symbol=self.symbol[mask], up_ret=self.up_ret[mask],
            dn_ret=self.dn_ret[mask], feature_names=self.feature_names,
        )


def build_dataset(
    symbols: list[str],
    interval: str = "1h",
    cache_dir: Path = DEFAULT_CACHE,
    spec: BarrierSpec = BarrierSpec(),
    vol_span: int = 100,
    cusum_mult: float = 2.0,
    split: str = "dev",
) -> tuple[Dataset, np.ndarray, dict[str, dict[str, np.ndarray]], dict[str, np.ndarray]]:
    """Build the training matrix. Returns `(dataset, bar_times, panel, sigma)`.

    `split` selects which side of `HOLDOUT_START` is returned:

    - `"dev"` (default) — development data only. This is the default because the
      safe thing must be what happens when nobody thought about it.
    - `"holdout"` — the reserved tail, for the single final evaluation.
    - `"all"` — everything, for data-quality reporting. Not for fitting.

    The panel and sigma are always returned whole: the backtester marks to market
    across a position's full life, and a holdout trade opened near the boundary
    resolves on bars that belong to neither side of it. Only the *event* rows are
    partitioned, which is where the leak would be.
    """
    bar_times, panel = load_panel(symbols, interval, cache_dir)
    btc = panel.get("BTCUSDT", {}).get("close")
    # Both need the whole panel, so they are computed once rather than per symbol.
    cross = cross_sectional(panel)
    funding = align_funding(bar_times, symbols, cache_dir)
    if funding is None:
        log.warning("no funding snapshot in %s — funding features will be NaN; "
                    "run `python -m gs_training.datasets` to fetch it", cache_dir)

    Xs, ys, evs, ends, ws, syms, ups, dns = [], [], [], [], [], [], [], []
    sigma: dict[str, np.ndarray] = {}
    for sym in symbols:
        b = panel[sym]
        sg = ewm_volatility(b["close"], span=vol_span)
        sigma[sym] = sg

        feats = compute_features(
            b["open"], b["high"], b["low"], b["close"], b["volume"],
            # Not b["volume"]: passing base volume here made quote_vol_z_168 a
            # bit-identical copy of vol_z_168 rather than a dollar-volume feature.
            quote_volume=b["quote_volume"],
            taker_buy_volume=b["taker_buy_volume"],
            bar_times=bar_times, btc_close=btc,
            funding=None if funding is None else funding.get(sym),
            cross=cross.get(sym),
        )
        X_all = feature_matrix(feats)
        finite = usable_mask(X_all, min_bar=MAX_LOOKBACK)

        ev = cusum_events(b["close"], np.nan_to_num(sg, nan=0.0) * cusum_mult)
        ev = ev[finite[ev]]                       # drop warm-up / non-finite rows
        if ev.size == 0:
            continue
        lab = label_events(b["open"], b["high"], b["low"], b["close"], sg, ev, spec)
        ok = lab["outcome"] > -2                  # drop UNRESOLVED
        ev, lab = ev[ok], {k: v[ok] for k, v in lab.items()}
        if ev.size == 0:
            continue

        Xs.append(X_all[ev])
        ys.append((lab["outcome"] == WIN).astype(int))
        evs.append(ev)
        ends.append(lab["label_end_idx"])
        # Uniqueness is computed per symbol: concurrent BTC and ETH labels are
        # separate evidence, not competing claims on the same bar.
        ws.append(average_uniqueness(lab["label_end_idx"].astype(float), ev.astype(float)))
        syms.append(np.full(ev.size, sym))
        s_h = np.array([spec.horizon_sigma(sg[i]) for i in ev])
        ups.append(s_h * spec.upper_mult)
        dns.append(s_h * spec.lower_mult)

    if not Xs:
        raise ValueError("no usable events — check warm-up, cache, and barrier spec")
    ds = Dataset(
        X=np.vstack(Xs), y=np.concatenate(ys), event_bar=np.concatenate(evs),
        label_end_bar=np.concatenate(ends), weight=np.concatenate(ws),
        symbol=np.concatenate(syms), up_ret=np.concatenate(ups), dn_ret=np.concatenate(dns),
    )
    if split != "all":
        ds = _apply_holdout(ds, bar_times, split)
    return ds, bar_times, panel, sigma


def holdout_bar(bar_times: np.ndarray) -> float:
    """`HOLDOUT_START` as a position on the panel's bar grid.

    Returns `len(bar_times)` when the boundary is past the end of the data, so a
    cache that has not been refreshed past it yields an empty holdout rather than
    a silently mislabelled one.
    """
    cut = np.datetime64(HOLDOUT_START.replace(tzinfo=None), "ns")
    return float(np.searchsorted(np.asarray(bar_times), cut, side="left"))


def _apply_holdout(ds: Dataset, bar_times: np.ndarray, split: str) -> Dataset:
    if split not in ("dev", "holdout"):
        raise ValueError(f"split must be 'dev', 'holdout' or 'all', got {split!r}")
    cut = holdout_bar(bar_times)
    dev, hold = holdout_split(ds.event_bar, ds.label_end_bar, cut)
    n_purged = len(ds) - int(dev.sum()) - int(hold.sum())
    log.info("holdout at %s (bar %d): dev=%d, holdout=%d, purged at the boundary=%d",
             HOLDOUT_START.date(), int(cut), int(dev.sum()), int(hold.sum()), n_purged)
    if split == "holdout" and hold.sum() == 0:
        raise ValueError(
            f"holdout is empty — the cache ends before {HOLDOUT_START.date()}. "
            "Refresh it with `python -m gs_training.datasets`."
        )
    return ds.select(dev if split == "dev" else hold)


def expected_value(p_win: np.ndarray, up_ret: np.ndarray, dn_ret: np.ndarray,
                   fee_rate: float, slippage_bps: float) -> np.ndarray:
    """EV per unit stake: `p·target − (1−p)·stop − round-trip costs`.

    This is what turns a calibrated probability into a decision. Thresholding on
    the probability alone ignores that a 60% chance of +14% and a 60% chance of
    +2% are entirely different trades once 0.2% of fees are paid.
    """
    cost = 2.0 * fee_rate + 2.0 * slippage_bps / 10_000.0
    return p_win * up_ret - (1.0 - p_win) * dn_ret - cost


def _frozen(estimator):
    """Wrap an already-fitted estimator for calibration.

    sklearn dropped `CalibratedClassifierCV(cv="prefit")` in 1.9 in favour of
    `FrozenEstimator`. Both are supported so this is not pinned to one version;
    either way the point is the same — calibrate a fitted model on data it has
    not seen, without refitting it.
    """
    try:
        from sklearn.frozen import FrozenEstimator
        return FrozenEstimator(estimator)
    except ImportError:  # sklearn < 1.6
        return estimator


def slice_panel(panel: dict[str, dict[str, np.ndarray]], lo: int, hi: int):
    return {s: {k: v[lo:hi] for k, v in b.items()} for s, b in panel.items()}


@dataclass
class FoldResult:
    fold: int
    n_train: int
    n_test: int
    auc: float
    brier: float
    n_signals: int
    strategy: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    # Backtest window on the full panel, and what was traded in it, on
    # *absolute* bar indices — kept so a fold can be re-read by regime or
    # re-run in a combined portfolio without refitting.
    window: tuple[int, int] = (0, 0)
    signals: list[Signal] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)


def train_walk_forward(
    ds: Dataset,
    panel: dict[str, dict[str, np.ndarray]],
    sigma: dict[str, np.ndarray],
    spec: BarrierSpec = BarrierSpec(),
    n_splits: int = 5,
    embargo: int | None = None,
    calib_frac: float = 0.2,
    ev_threshold: float = 0.0,
    costs: CostModel = CostModel(),
    portfolio: Portfolio = Portfolio(),
    lgb_params: dict | None = None,
    seed: int = 0,
    funding: dict[str, np.ndarray] | None = None,
) -> list[FoldResult]:
    """Fit, calibrate, threshold and backtest each walk-forward fold.

    The side comes from `spec`; `funding` (settlements on the full panel grid)
    is required when `costs.venue` is perp.
    """
    import lightgbm as lgb
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import brier_score_loss, roc_auc_score

    embargo = spec.max_bars if embargo is None else embargo
    params = {
        "objective": "binary", "learning_rate": 0.03, "num_leaves": 15,
        "min_child_samples": 100, "subsample": 0.8, "subsample_freq": 1,
        "colsample_bytree": 0.7, "reg_lambda": 1.0, "n_estimators": 300,
        "verbose": -1, "random_state": seed,
    } | (lgb_params or {})

    folds = walk_forward(ds.event_bar, ds.label_end_bar, n_splits=n_splits, embargo=embargo)
    if not folds:
        raise ValueError("no viable folds — horizon may be too long for the sample")

    results: list[FoldResult] = []
    for k, f in enumerate(folds):
        assert_no_leakage(f, ds.event_bar, ds.label_end_bar, embargo=embargo)

        # Calibration data is the tail of the training block: unseen by the fit,
        # and already purged with respect to the test block.
        order = f.train_idx[np.argsort(ds.event_bar[f.train_idx], kind="stable")]
        n_cal = max(1, int(len(order) * calib_frac))
        fit_idx, cal_idx = order[:-n_cal], order[-n_cal:]
        if fit_idx.size < 50 or len(np.unique(ds.y[fit_idx])) < 2:
            log.warning("fold %d: too little training data, skipped", k)
            continue

        clf = lgb.LGBMClassifier(**params)
        clf.fit(ds.X[fit_idx], ds.y[fit_idx], sample_weight=ds.weight[fit_idx])

        if len(np.unique(ds.y[cal_idx])) < 2:
            log.warning("fold %d: calibration slice is single-class, using raw scores", k)
            model = clf
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = CalibratedClassifierCV(_frozen(clf), method="isotonic")
                model.fit(ds.X[cal_idx], ds.y[cal_idx], sample_weight=ds.weight[cal_idx])

        te = f.test_idx
        p = model.predict_proba(ds.X[te])[:, 1]
        auc = (float(roc_auc_score(ds.y[te], p, sample_weight=ds.weight[te]))
               if len(np.unique(ds.y[te])) > 1 else float("nan"))
        brier = float(brier_score_loss(ds.y[te], p, sample_weight=ds.weight[te]))

        ev = expected_value(p, ds.up_ret[te], ds.dn_ret[te], costs.fee_rate, costs.slippage_bps)
        take = ev > ev_threshold

        # Backtest inside the test window only, extended by the horizon so
        # positions opened at the end can still resolve.
        lo = int(ds.event_bar[te].min())
        hi = int(ds.event_bar[te].max()) + spec.max_bars + 2
        hi = min(hi, len(next(iter(panel.values()))["close"]))
        sub = slice_panel(panel, lo, hi)
        sub_sigma = {s: v[lo:hi] for s, v in sigma.items()}
        sub_funding = None if funding is None else {s: v[lo:hi] for s, v in funding.items()}

        sigs = [Signal(bar=int(b) - lo, symbol=str(s), side=spec.side)
                for b, s in zip(ds.event_bar[te][take], ds.symbol[te][take])]
        bt = run_backtest(sub, sigs, spec, sub_sigma, costs, portfolio, sub_funding)
        strat = performance(bt)
        base = performance(run_backtest(
            sub, random_entries(len(sigs), hi - lo, sorted(panel), seed=seed + k,
                                side=spec.side),
            spec, sub_sigma, costs, portfolio, sub_funding))

        results.append(FoldResult(
            fold=k, n_train=int(fit_idx.size), n_test=int(te.size),
            auc=auc, brier=brier, n_signals=int(take.sum()),
            strategy=strat, baseline=base, window=(lo, hi),
            signals=[Signal(bar=s.bar + lo, symbol=s.symbol, side=s.side) for s in sigs],
            trades=[_shift(t, lo) for t in bt.trades],
        ))
        log.info("fold %d: auc=%.3f signals=%d ret=%+.1f%% (baseline %+.1f%%)",
                 k, auc, take.sum(), strat["total_return"] * 100,
                 base["total_return"] * 100)
    return results


def _shift(t: Trade, offset: int) -> Trade:
    return replace(t, entry_i=t.entry_i + offset, exit_i=t.exit_i + offset)


def costs_for(venue: str) -> CostModel:
    return CostModel.perp() if venue == PERP else CostModel()


def aggregate(results: list[FoldResult]) -> dict[str, float]:
    """Fold-averaged headline numbers.

    **Folds that emitted no signal are excluded from the return averages.** A
    fold where expected value never cleared the threshold is an abstention, not
    a 0% result: averaging that zero in pulls a losing strategy toward break-even
    and reports a profit factor of 0 for a fold that never traded. Abstentions
    are reported separately as `folds_no_signal`, because a model that refuses to
    trade most of the time is telling you something worth reading, not passing.

    `beat_baseline_folds` is the one to read first: a strategy that wins on
    average but loses in most folds got lucky in one regime.
    """
    if not results:
        return {}
    traded = [r for r in results if r.n_signals > 0]
    out = {
        "folds": float(len(results)),
        "folds_traded": float(len(traded)),
        "folds_no_signal": float(len(results) - len(traded)),
        "mean_auc": float(np.nanmean([r.auc for r in results])),
        "mean_brier": float(np.mean([r.brier for r in results])),
        "total_signals": float(sum(r.n_signals for r in results)),
    }
    if not traded:
        return out
    ret = np.array([r.strategy["total_return"] for r in traded])
    bret = np.array([r.baseline["total_return"] for r in traded])
    pf = np.array([r.strategy["profit_factor"] for r in traded])
    return out | {
        "mean_return": float(ret.mean()),
        "mean_baseline_return": float(bret.mean()),
        "mean_excess_return": float((ret - bret).mean()),
        "beat_baseline_folds": float((ret > bret).sum()),
        "mean_profit_factor": float(np.mean(pf[np.isfinite(pf)])) if np.isfinite(pf).any() else 0.0,
        "mean_max_drawdown": float(np.mean([r.strategy["max_drawdown"] for r in traded])),
        "mean_sharpe": float(np.mean([r.strategy["sharpe"] for r in traded])),
        "total_funding": float(sum(r.strategy.get("total_funding", 0.0) for r in traded)),
    }


def log_to_mlflow(tracking_uri: str, run_name: str, params: dict,
                  results: list[FoldResult], summary: dict) -> None:
    """Best-effort MLflow logging — a dead tracking server must not lose a run.

    The retry budget is deliberately tiny: mlflow's default backoff spends over a
    minute failing to reach a server that is simply not running, which turns
    "best effort" into "hangs at the end of every experiment".
    """
    import os
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "5")
    try:
        import mlflow
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment("geldscheisser-phase1")
        with mlflow.start_run(run_name=run_name):
            mlflow.log_params(params)
            mlflow.log_metrics(summary)
            for r in results:
                mlflow.log_metrics({
                    f"fold{r.fold}_auc": r.auc,
                    f"fold{r.fold}_return": r.strategy["total_return"],
                    f"fold{r.fold}_baseline_return": r.baseline["total_return"],
                    f"fold{r.fold}_signals": float(r.n_signals),
                })
    except Exception as e:  # noqa: BLE001 — logging must never break training
        log.warning("MLflow logging skipped (%s: %s)", type(e).__name__, e)


def main() -> None:
    from gs_common.config import load_settings
    from gs_common.log import setup_logging

    settings = load_settings()
    p = argparse.ArgumentParser(prog="gs-train", description="Train the phase-1 model")
    p.add_argument("--symbols", default=",".join(settings.symbols))
    p.add_argument("--interval", default="1h")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    p.add_argument("--splits", type=int, default=5)
    p.add_argument("--max-bars", type=int, default=336)
    p.add_argument("--upper", type=float, default=1.0)
    p.add_argument("--lower", type=float, default=0.5)
    p.add_argument("--ev-threshold", type=float, default=0.0)
    p.add_argument("--side", choices=("long", "short"), default="long")
    p.add_argument("--venue", choices=("spot", "perp"), default="spot",
                   help="perp: USDⓈ-M taker fees and funding cash flows (needed for shorts)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--mlflow-uri", default="http://localhost:5001")
    p.add_argument("--run-name", default="lgbm-baseline")
    p.add_argument("--split", choices=("dev", "holdout", "all"), default="dev",
                   help=f"dev (default) trains on data before {HOLDOUT_START.date()}; "
                        "holdout is the final check and needs --i-am-done-tuning")
    p.add_argument("--i-am-done-tuning", action="store_true",
                   help="required to run against the holdout. The flag is the point: "
                        "the holdout is only worth anything while it is untouched, so "
                        "spending it should be a decision, not a typo")
    args = p.parse_args()

    if args.split == "holdout" and not args.i_am_done_tuning:
        p.error(
            f"--split holdout evaluates the reserved period from {HOLDOUT_START.date()} "
            "onward. Every run against it spends it — there is no second untouched "
            "tail. Pass --i-am-done-tuning if that is really what you want."
        )

    if args.side == "short" and args.venue != "perp":
        p.error("spot cannot short — use --venue perp")

    setup_logging(settings.log_level)
    symbols = [s.strip() for s in args.symbols.split(",")]
    spec = BarrierSpec(args.upper, args.lower, args.max_bars,
                       side=SHORT if args.side == "short" else LONG)
    costs = costs_for(args.venue)

    ds, bar_times, panel, sigma = build_dataset(
        symbols, args.interval, Path(args.cache_dir), spec, split=args.split)
    funding = None
    if args.venue == PERP:
        funding = funding_settlements(bar_times, symbols, Path(args.cache_dir))
        if funding is None:
            p.error("perp venue needs funding snapshots — run `python -m gs_training.datasets`")
    log.info("dataset[%s]: %d events, %d features, effective n=%.0f",
             args.split, len(ds), ds.X.shape[1], ds.weight.sum())
    if args.split == "holdout":
        log.warning("RUNNING ON THE HOLDOUT — record this run; the tail is now spent")

    results = train_walk_forward(ds, panel, sigma, spec, n_splits=args.splits,
                                 ev_threshold=args.ev_threshold, costs=costs,
                                 seed=args.seed, funding=funding)
    summary = aggregate(results)

    print(f"\n{'fold':>4} {'train':>8} {'test':>7} {'auc':>6} {'signals':>8} "
          f"{'return':>9} {'baseline':>9} {'PF':>6}")
    for r in results:
        print(f"{r.fold:>4} {r.n_train:>8,} {r.n_test:>7,} {r.auc:>6.3f} {r.n_signals:>8,} "
              f"{r.strategy['total_return']:>+8.1%} {r.baseline['total_return']:>+8.1%} "
              f"{r.strategy['profit_factor']:>6.2f}")
    print("\nsummary:")
    for k, v in summary.items():
        print(f"  {k:24} {v:>10.4f}")

    log_to_mlflow(args.mlflow_uri, args.run_name, {
        "symbols": args.symbols, "interval": args.interval, "splits": args.splits,
        "upper_mult": args.upper, "lower_mult": args.lower, "max_bars": args.max_bars,
        "ev_threshold": args.ev_threshold, "seed": args.seed,
        "side": args.side, "venue": args.venue, "fee_rate": costs.fee_rate,
        "n_events": len(ds), "effective_n": round(float(ds.weight.sum())),
        # Logged on every run so a holdout evaluation is identifiable in MLflow
        # forever, rather than resting on whoever ran it remembering.
        "split": args.split, "holdout_start": HOLDOUT_START.date().isoformat(),
    }, results, summary)


if __name__ == "__main__":
    main()
