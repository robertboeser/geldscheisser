"""Walk-forward leakage guarantees.

These are the tests that matter most in the whole repo: leakage does not crash
or look wrong, it just makes a worthless model look excellent."""

import numpy as np
import pytest

from gs_training.splits import assert_no_leakage, average_uniqueness, walk_forward

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
