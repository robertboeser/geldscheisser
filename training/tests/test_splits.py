"""Walk-forward leakage guarantees.

These are the tests that matter most in the whole repo: leakage does not crash
or look wrong, it just makes a worthless model look excellent."""

import numpy as np
import pytest

from gs_training.splits import (
    assert_no_leakage,
    average_uniqueness,
    holdout_split,
    walk_forward,
)

HORIZON = 336  # 14 days of 1h bars


def events(n, step=1, horizon=HORIZON):
    t = np.arange(0, n * step, step)
    return t, t + horizon


class TestWalkForward:
    def test_training_data_always_precedes_its_test_block(self):
        t, e = events(2000)
        for f in walk_forward(t, e, n_splits=4):
            assert t[f.train_idx].max() < f.test_start

    def test_labels_reaching_into_the_test_window_are_purged(self):
        t, e = events(2000)
        folds = walk_forward(t, e, n_splits=4)
        assert folds, "expected folds"
        for f in folds:
            assert f.n_purged > 0, "a 336-bar horizon must purge the train/test boundary"
            assert_no_leakage(f, t, e)   # raises if any label crosses

    def test_a_deliberately_leaky_fold_is_caught(self):
        """The guard must actually fire — a check that cannot fail is not a check."""
        t, e = events(5000)
        f = walk_forward(t, e, n_splits=2)[0]
        leaky = f.__class__(
            train_idx=np.arange(t.size), test_idx=f.test_idx,
            test_start=f.test_start, test_end=f.test_end,
            n_purged=0, n_embargoed=0)
        with pytest.raises(AssertionError, match="resolve at or after test_start"):
            assert_no_leakage(leaky, t, e)

    def test_longer_horizon_purges_more(self):
        t_short, e_short = events(2000, horizon=10)
        t_long, e_long = events(2000, horizon=500)
        short = sum(f.n_purged for f in walk_forward(t_short, e_short, n_splits=4))
        long_ = sum(f.n_purged for f in walk_forward(t_long, e_long, n_splits=4))
        assert long_ > short

    def test_embargo_excludes_the_shadow_after_the_test_block(self):
        t, e = events(1000, horizon=0)
        base = walk_forward(t, e, n_splits=3, embargo=0)
        emb = walk_forward(t, e, n_splits=3, embargo=50)
        assert sum(f.train_idx.size for f in emb) <= sum(f.train_idx.size for f in base)
        for f in emb:
            assert_no_leakage(f, t, e, embargo=50)

    def test_test_blocks_are_disjoint_and_ordered(self):
        t, e = events(1000)
        folds = walk_forward(t, e, n_splits=4)
        seen = set()
        prev_end = -np.inf
        for f in folds:
            idx = set(f.test_idx.tolist())
            assert not (idx & seen), "test blocks must not overlap"
            seen |= idx
            assert f.test_start > prev_end
            prev_end = f.test_end

    def test_rejects_label_end_before_event_time(self):
        with pytest.raises(ValueError, match="must not precede"):
            walk_forward(np.array([5, 6]), np.array([4, 7]), n_splits=1)

    def test_too_few_samples_yields_no_folds(self):
        assert walk_forward(np.arange(2), np.arange(2), n_splits=5) == []

    def test_horizon_swallowing_the_data_yields_no_folds_rather_than_a_leaky_one(self):
        """With 500 events and a 336-bar horizon, purging removes every
        training sample at each boundary. Returning nothing is correct; the
        wrong behaviour would be handing back a fold with leaked labels."""
        t, e = events(500, horizon=336)
        assert walk_forward(t, e, n_splits=2) == []


class TestAverageUniqueness:
    def test_disjoint_labels_are_fully_unique(self):
        t = np.array([0.0, 10.0, 20.0])
        e = np.array([9.0, 19.0, 29.0])
        assert np.allclose(average_uniqueness(e, t), 1.0)

    def test_heavy_overlap_drives_uniqueness_down(self):
        """The point of the whole exercise: 336x-overlapping labels are not
        336 independent observations."""
        t = np.arange(100.0)
        e = t + 50
        u = average_uniqueness(e, t)
        assert np.all(u < 0.5)
        assert u.mean() < 0.1

    def test_uniqueness_stays_in_the_unit_interval(self):
        rng = np.random.default_rng(0)
        t = np.sort(rng.uniform(0, 1000, 200))
        e = t + rng.uniform(1, 100, 200)
        u = average_uniqueness(e, t)
        assert np.all((u > 0) & (u <= 1.0 + 1e-9))


class TestHoldoutSplit:
    """The holdout only means something if nothing can quietly reach into it."""

    def test_no_development_label_resolves_inside_the_holdout(self):
        # cut well past the horizon, or there is no room for a dev sample at all
        t, end = events(1000)
        cut = 500.0
        dev, _ = holdout_split(t, end, cut)
        assert dev.any()
        assert (end[dev] < cut).all()

    def test_holdout_is_exactly_the_events_decided_after_the_cut(self):
        t, end = events(1000)
        cut = 500.0
        _, hold = holdout_split(t, end, cut)
        assert (t[hold] >= cut).all()
        assert (t[~hold] < cut).all()

    def test_the_boundary_band_belongs_to_neither_side(self):
        # The whole point: with a 336-bar horizon, events in the 336 bars before
        # the cut resolve inside the holdout and must be dropped, not assigned.
        t, end = events(1000)
        cut = 500.0
        dev, hold = holdout_split(t, end, cut)
        assert not (dev & hold).any()
        dropped = ~(dev | hold)
        assert dropped.sum() == HORIZON
        assert (t[dropped] < cut).all() and (end[dropped] >= cut).all()

    def test_a_longer_horizon_costs_a_wider_band(self):
        t, short = events(1000, horizon=24)
        _, long_ = events(1000, horizon=336)
        d_short, h_short = holdout_split(t, short, 500.0)
        d_long, h_long = holdout_split(t, long_, 500.0)
        assert (~(d_long | h_long)).sum() > (~(d_short | h_short)).sum()
        assert h_short.sum() == h_long.sum()   # only the dev side pays

    def test_cut_past_the_data_yields_an_empty_holdout_not_a_wrong_one(self):
        t, end = events(100)
        dev, hold = holdout_split(t, end, 10_000.0)
        assert hold.sum() == 0
        assert dev.all()

    def test_cut_before_the_data_reserves_everything(self):
        t, end = events(100)
        dev, hold = holdout_split(t, end, -1.0)
        assert hold.all()
        assert dev.sum() == 0

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError):
            holdout_split(np.arange(10), np.arange(9), 5.0)

    def test_walk_forward_on_the_dev_side_never_sees_the_holdout(self):
        # The composition that actually matters: fold generation is applied to
        # the development subset, so no fold's training or test block may carry
        # a label that resolves past the cut.
        t, end = events(2000)
        cut = 1500.0
        dev, _ = holdout_split(t, end, cut)
        td, ed = t[dev], end[dev]
        folds = walk_forward(td, ed, n_splits=5, embargo=HORIZON)
        assert folds
        for f in folds:
            assert_no_leakage(f, td, ed, embargo=HORIZON)
            assert ed[f.train_idx].max() < cut
            assert ed[f.test_idx].max() < cut
