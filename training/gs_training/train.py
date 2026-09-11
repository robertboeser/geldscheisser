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

**Timeouts count as not-win.** The classifier is binary: did the profit barrier
get hit first. Expected value then assumes a non-win pays the full stop, which
is pessimistic — a timeout usually exits nearer to flat than to the stop. That
bias is deliberate and in the safe direction.
"""

from __future__ import annotations

import argparse
import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .backtest import CostModel, Portfolio, Signal, run_backtest
from .baselines import random_entries
from .datasets import DEFAULT_CACHE, align_funding, load_panel
from .features import (
    FEATURE_NAMES,
    MAX_LOOKBACK,
    compute_features,
    cross_sectional,
    feature_matrix,
    usable_mask,
)
from .labels import WIN, BarrierSpec, cusum_events, ewm_volatility, label_events
from .metrics import performance
from .splits import assert_no_leakage, average_uniqueness, walk_forward

log = logging.getLogger(__name__)


@dataclass
class Dataset:
    """Model matrix plus everything needed to split and evaluate it honestly."""

    X: np.ndarray                 # (n, n_features)
    y: np.ndarray                 # 1 = profit barrier hit first
    event_bar: np.ndarray         # decision bar on the shared grid
    label_end_bar: np.ndarray     # last bar the label depends on (for purging)
    weight: np.ndarray            # average uniqueness, per symbol
    symbol: np.ndarray            # symbol per row
    up_ret: np.ndarray            # barrier distances at the decision bar,
    dn_ret: np.ndarray            # used for the expected-value rule
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def __len__(self) -> int:
        return self.X.shape[0]


def build_dataset(
    symbols: list[str],
    interval: str = "1h",
    cache_dir: Path = DEFAULT_CACHE,
    spec: BarrierSpec = BarrierSpec(),
    vol_span: int = 100,
    cusum_mult: float = 2.0,
) -> tuple[Dataset, np.ndarray, dict[str, dict[str, np.ndarray]], dict[str, np.ndarray]]:
    """Build the training matrix. Returns `(dataset, bar_times, panel, sigma)`."""
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
    return ds, bar_times, panel, sigma


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
) -> list[FoldResult]:
    """Fit, calibrate, threshold and backtest each walk-forward fold."""
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

        sigs = [Signal(bar=int(b) - lo, symbol=str(s))
                for b, s in zip(ds.event_bar[te][take], ds.symbol[te][take])]
        strat = performance(run_backtest(sub, sigs, spec, sub_sigma, costs, portfolio))
        base = performance(run_backtest(
            sub, random_entries(len(sigs), hi - lo, sorted(panel), seed=seed + k),
            spec, sub_sigma, costs, portfolio))

        results.append(FoldResult(
            fold=k, n_train=int(fit_idx.size), n_test=int(te.size),
            auc=auc, brier=brier, n_signals=int(take.sum()),
            strategy=strat, baseline=base,
        ))
        log.info("fold %d: auc=%.3f signals=%d ret=%+.1f%% (baseline %+.1f%%)",
                 k, auc, take.sum(), strat["total_return"] * 100,
                 base["total_return"] * 100)
    return results


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
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--mlflow-uri", default="http://localhost:5001")
    p.add_argument("--run-name", default="lgbm-baseline")
    args = p.parse_args()

    setup_logging(settings.log_level)
    symbols = [s.strip() for s in args.symbols.split(",")]
    spec = BarrierSpec(args.upper, args.lower, args.max_bars)

    ds, _, panel, sigma = build_dataset(symbols, args.interval, Path(args.cache_dir), spec)
    log.info("dataset: %d events, %d features, effective n=%.0f",
             len(ds), ds.X.shape[1], ds.weight.sum())

    results = train_walk_forward(ds, panel, sigma, spec, n_splits=args.splits,
                                 ev_threshold=args.ev_threshold, seed=args.seed)
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
        "n_events": len(ds), "effective_n": round(float(ds.weight.sum())),
    }, results, summary)


if __name__ == "__main__":
    main()
