"""Purged, embargoed walk-forward splits.

A random split is not merely suboptimal here, it is wrong: labels look up to 14
days ahead, so a training sample drawn from just before a test bar contains that
bar's future. The model then scores brilliantly and means nothing. Everything in
this module exists to make that impossible.

Two separate leaks, two separate defences:

    train ─────────────────────────┤              ├────── train resumes ──
                          ├─purge──┤├─── test ───┤├─embargo─┤
                          ↑                                 ↑
        labels reaching into the test window      features looking back
                                                  into it

**Purge** drops training samples whose *label window* overlaps the test period —
a sample at `T-5d` with a 14-day horizon resolves at `T+9d`, inside the test set.
**Embargo** drops training samples starting shortly after the test period,
because their features have lookback that reaches back into it.

Purge length is not a parameter: it is derived per-sample from `label_end`, so a
variable horizon (an early barrier touch, a shorter series) is handled exactly
rather than approximated by a fixed window.

Walk-forward alone is not enough. Its folds are reusable, so every experiment
scored against them spends a little of their credibility — run twenty variants
and the best one is partly a fit to the folds themselves. `HOLDOUT_START` and
`holdout_split` reserve a final block that nothing but the very last check may
touch; see the constant for which block and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np


@dataclass(frozen=True)
class Fold:
    """One walk-forward step. Indices refer to the event arrays passed in."""

    train_idx: np.ndarray
    test_idx: np.ndarray
    test_start: np.int64
    test_end: np.int64
    n_purged: int
    n_embargoed: int

    def __repr__(self) -> str:  # keeps fold listings readable in notebooks
        return (f"Fold(train={self.train_idx.size}, test={self.test_idx.size}, "
                f"purged={self.n_purged}, embargoed={self.n_embargoed})")


def walk_forward(
    event_time: np.ndarray,
    label_end: np.ndarray,
    n_splits: int = 5,
    embargo: float = 0.0,
    min_train: int = 1,
) -> list[Fold]:
    """Chronological folds with purge + embargo.

    `event_time` is when each sample is *decided* and `label_end` when its label
    is fully resolved; both in the same unit (bar index or epoch — anything
    orderable and subtractable). `embargo` is in those same units.

    Test blocks are contiguous, equal-count, and strictly after their training
    data. Only samples *before* a test block are eligible for training, so this
    is walk-forward, not cross-validation: the model is never fitted on the
    future of its own test set.
    """
    event_time = np.asarray(event_time)
    label_end = np.asarray(label_end)
    if event_time.shape != label_end.shape:
        raise ValueError("event_time and label_end must be the same length")
    if np.any(label_end < event_time):
        raise ValueError("label_end must not precede event_time")
    if n_splits < 1:
        raise ValueError("n_splits must be at least 1")

    order = np.argsort(event_time, kind="stable")
    n = order.size
    if n < n_splits + 1:
        return []

    folds: list[Fold] = []
    # Reserve the first block as training-only, so every test block has history.
    block = n // (n_splits + 1)
    if block == 0:
        return []

    for s in range(n_splits):
        lo = block * (s + 1)
        hi = n if s == n_splits - 1 else block * (s + 2)
        test_idx = order[lo:hi]
        if test_idx.size == 0:
            continue
        t_start = event_time[test_idx].min()
        t_end = event_time[test_idx].max()

        candidates = order[:lo]
        if candidates.size == 0:
            continue
        # Purge: a training label that resolves at or after the test period
        # begins has seen test data.
        overlaps = label_end[candidates] >= t_start
        # Embargo: training samples decided inside the shadow just after the
        # test block. (Only reachable when a later fold reuses earlier data;
        # kept explicit so the guarantee does not depend on fold ordering.)
        embargoed = (event_time[candidates] > t_end) & (
            event_time[candidates] <= t_end + embargo
        )
        keep = ~(overlaps | embargoed)
        train_idx = candidates[keep]
        if train_idx.size < min_train:
            continue
        folds.append(Fold(
            train_idx=train_idx,
            test_idx=test_idx,
            test_start=t_start,
            test_end=t_end,
            n_purged=int(overlaps.sum()),
            n_embargoed=int((embargoed & ~overlaps).sum()),
        ))
    return folds


def assert_no_leakage(
    fold: Fold, event_time: np.ndarray, label_end: np.ndarray, embargo: float = 0.0
) -> None:
    """Raise if any training label reaches into the fold's test window.

    Cheap enough to call on every fold in every experiment, and worth doing:
    leakage does not announce itself, it just makes the metrics look good.
    """
    event_time = np.asarray(event_time)
    label_end = np.asarray(label_end)
    if fold.train_idx.size == 0:
        return
    bad = label_end[fold.train_idx] >= fold.test_start
    if bad.any():
        raise AssertionError(
            f"{int(bad.sum())} training labels resolve at or after test_start="
            f"{fold.test_start} (worst: {label_end[fold.train_idx].max()})"
        )
    shadow = (event_time[fold.train_idx] > fold.test_end) & (
        event_time[fold.train_idx] <= fold.test_end + embargo
    )
    if shadow.any():
        raise AssertionError(f"{int(shadow.sum())} training samples inside the embargo")


def average_uniqueness(label_end: np.ndarray, event_time: np.ndarray) -> np.ndarray:
    """Per-sample weight in (0, 1]: how much of its label window it has to itself.

    Overlapping labels are not independent observations — with a 14-day horizon
    on 1h bars, consecutive samples share 335/336 of their outcome. Training
    unweighted treats that as 336 pieces of evidence when it is closer to one.
    These weights are what `sample_weight` should be set to.
    """
    event_time = np.asarray(event_time, dtype=float)
    label_end = np.asarray(label_end, dtype=float)
    n = event_time.size
    if n == 0:
        return np.zeros(0)
    # Concurrency over a shared grid of all interval endpoints.
    edges = np.unique(np.concatenate([event_time, label_end]))
    counts = np.zeros(edges.size)
    starts = np.searchsorted(edges, event_time, side="left")
    ends = np.searchsorted(edges, label_end, side="right")
    for a, b in zip(starts, ends):
        counts[a:b] += 1.0
    out = np.zeros(n)
    for j, (a, b) in enumerate(zip(starts, ends)):
        c = counts[a:b]
        c = c[c > 0]
        out[j] = float(np.mean(1.0 / c)) if c.size else 0.0
    return out


# The final holdout: everything from this instant onward is reserved and must
# not inform model selection. It is a constant rather than a CLI default because
# a holdout you can move is not a holdout — the whole value is that it was fixed
# before the experiments that would otherwise be tempted to move it.
#
# Chosen 2026-09-22, when the archive ran 2021-01-01 .. 2026-09-22 on 1h bars:
# it reserves the most recent ~12 months (~26 non-overlapping 14-day windows per
# symbol) while leaving ~4.75 years of development data. The tail is the right
# place for it — it is the only split that answers the question live trading
# asks, "does this work on data that did not exist when the model was designed".
#
# Known limitation, stated here so it is not rediscovered as a surprise: a
# chronological holdout can only ever contain the most recent regime. This one
# is a drawdown (2025 closed −31% median across the basket, 2026 is −8% so far
# with BTC −40% peak-to-trough), so it tests whether a long-only model correctly
# *abstains*, not whether it captures upside. Passing it is necessary, not
# sufficient.
HOLDOUT_START = datetime(2025, 10, 1, tzinfo=timezone.utc)


def holdout_split(
    event_time: np.ndarray,
    label_end: np.ndarray,
    holdout_start: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Partition events into (development, holdout) masks at `holdout_start`.

    Units follow `walk_forward`: anything orderable, normally a bar index.

    Development keeps only events whose label has **fully resolved** before the
    holdout begins. That is the same purge rule `walk_forward` applies between
    folds, for the same reason — an event decided five days before the boundary
    with a 14-day horizon resolves *inside* the holdout, so fitting on it leaks
    the very period being reserved, and the holdout stops being untouched.

    The two masks deliberately do **not** cover every event: those in the purged
    band belong to neither. Returning them as a gap rather than assigning them
    to a side is the point, and `n_purged = len(event_time) - dev.sum() -
    hold.sum()` is worth logging so the cost stays visible.
    """
    event_time = np.asarray(event_time)
    label_end = np.asarray(label_end)
    if event_time.shape != label_end.shape:
        raise ValueError("event_time and label_end must be the same length")

    hold = event_time >= holdout_start
    # Not `~hold`: an event before the boundary whose label reaches past it is
    # in neither set.
    dev = label_end < holdout_start
    return dev, hold
