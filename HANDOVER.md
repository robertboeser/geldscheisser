# Handover — 2026-09-04

Session question: *is there enough data to start training, or should we wait?*
Answer: **train now** — waiting buys almost nothing — **and shorten the holding
horizon**, which buys a lot. Details and the numbers behind them below.

Nothing in `gs_training/` was modified. One new file:
[training/scripts/horizon_sweep.py](training/scripts/horizon_sweep.py), which
reproduces every table here. Read this alongside [CHECKLIST.md](CHECKLIST.md);
the items marked **→ CHECKLIST** at the end should be folded into it.

---

## 1. Data inventory (archive, queried live 2026-09-04)

| table | span | rows | usable at a multi-day horizon? |
|---|---|---|---|
| `klines` 1h | 2021-01-01 → now, 10 symbols | 497,230 | **yes** |
| `klines` 15m | 2021-01-01 → now | 1,988,810 | yes |
| `klines` 1m / 5m | 2025-09-01 → now | 6,366,110 | yes (1 year) |
| `funding_rates` | **2021-01-01 → now, all 10 symbols** | 62,255 | **yes — currently unused** |
| `book_rollups` | 2026-08-05 → now (30d) | 2,593,129 | no |
| `book_snapshots` | 2026-08-05 → now (30d) | 87,069 | no |
| `agg_trades_1m` | 2026-08-05 → now (30d) | 435,000 | no |
| `open_interest` | 2026-08-05 → now (30d) | 87,270 | no |

`book_rollups` daily completeness for BTCUSDT is ~8,550–8,600 of an expected
8,640 at 10s cadence — i.e. ~99.3%, no material holes.

### Why waiting does not help

The binding constraint is not rows, it is **non-overlapping label windows**, and
calendar time buys almost none of them at a long horizon. At 336 bars:
49,440 / 336 ≈ **147 windows per symbol**, and the ten symbols are heavily
BTC-correlated so the honest figure is lower still. Each additional month of
collection adds **~2 windows per symbol**. Six more months of waiting grows the
effective sample by ~8%. This sample size is structural — the response is to
design the evaluation around it, not to wait it out.

Regime coverage, which is what the phase-2 gate actually asks for, is already
there: 2021 bull, 2022 bear, 2023 chop, 2024–26 bull.

### What genuinely is not there yet

**Order book / OI / aggTrades: 30 days.** The phase-0 gate says "4–8 weeks of
rollups before book features join the model." That gate was written before the
horizon was settled and is **calibrated for a much shorter one**. At a 14-day
horizon, 30 days of rollups is *2 non-overlapping windows per symbol*; eight
weeks gives 4. Book features are not weeks away from being usable at this
horizon — they are a year-plus away, or they belong to a shorter-horizon model.
Do not let that gate hold anything up; restate it in terms of independent
windows rather than weeks.

**Funding is a free feature currently going unused.**
[features.py:276-278](training/gs_training/features.py#L276-L278) has
`funding_rate` and `funding_ret_24` NaN-ing out because
[datasets.py](training/gs_training/datasets.py) only snapshots klines
(`snapshot_klines` / `load_panel` are the only loaders). The archive has funding
at 8h cadence back to 2021-01-01 for **all ten symbols, no gaps** — exactly the
same span as the klines. For a long-only swing model this is real signal
(persistent positive funding = crowded longs). `oi_change_24` stays NaN; only 30
days of open interest exists.

---

## 2. The horizon finding

The 14-day horizon was a judgement call, not a derived number, and it is the
single most expensive parameter in the setup. Sweeping it produced a clear
result.

### The label problem is nearly scale-invariant

Because `BarrierSpec.horizon_sigma` scales barriers as `sigma·sqrt(max_bars)`,
shrinking the horizon shrinks the barriers proportionally. The *shape* of the
problem barely moves:

| horizon | eff. n | median hold | target % | cost/target | win % | break-even p |
|---|---|---|---|---|---|---|
| 12h | 30,854 | 0.2d | 2.7 | 11.3% | 23.9 | 40.9% |
| 1d | 17,609 | 0.3d | 3.7 | 8.0% | 24.8 | 38.7% |
| 2d | 9,518 | 0.7d | 5.3 | 5.7% | 25.3 | 37.1% |
| **3.5d** | **5,619** | 1.2d | 7.0 | 4.3% | 26.2 | 36.2% |
| **7d** | **2,866** | 2.4d | 9.9 | 3.0% | 27.0 | 35.3% |
| **14d (current)** | **1,448** | 4.6d | 14.1 | 2.1% | 27.6 | 34.8% |
| 21d | 968 | 6.9d | 17.2 | 1.7% | 28.1 | 34.5% |

(eff. n here is as `train.py` weights today — see §3, the honest numbers are
~2.5× higher across the board. Break-even p is the probability at which the EV
rule `p·up − (1−p)·dn − cost` turns positive.)

Win rate (~25–28%), timeout rate (~16%), and the break-even probability the
model must clear (~35–39%) are **essentially constant from 12h to 21d**. Only
two things actually move with the horizon:

- **effective sample size** — roughly `1/horizon`, so it rises fast as the
  horizon shrinks
- **cost drag** — rises slowly, 2.1% → 3.0% → 4.3% of target

Going 14d → 3.5d **quadruples the evidence for 2.2 percentage points of cost
drag**. Shortening the horizon does not make the trade structurally harder to
win; it only costs a little more friction. That is the whole argument.

### Two weeks was already a ceiling, not a holding time

At the current spec the **median hold is 4.6 days** and the vertical barrier only
binds on ~16% of trades. "About two weeks" already means "usually under a week."
A 7-day ceiling gives a 2.4-day median and doubles the sample — still
unambiguously swing trading, nowhere near the intraday regime this project
deliberately avoids.

### Barrier width is a second, independent lever

Tightening the barriers shortens typical holds while preserving the two-week
tolerance exactly (`max_bars=336` throughout):

| geometry | eff. n (honest) | median hold | p90 hold | target % | cost/target | win % | timeout % |
|---|---|---|---|---|---|---|---|
| 1.0 / 0.5 (current) | 3,566 | 4.6d | 14.0d | 14.1 | 2.1% | 27.6 | 16.5 |
| 0.75 / 0.375 | 5,431 | 2.7d | 11.0d | 10.5 | 2.8% | 31.7 | 6.5 |
| 0.5 / 0.25 | 10,611 | 1.2d | 4.7d | 7.0 | 4.3% | 33.5 | 1.0 |
| 0.5 / 0.5 | 5,670 | 2.6d | 9.8d | 7.0 | 4.3% | 48.4 | 5.1 |

Note the symmetric `0.5/0.5` row: a 48.4% win rate against a 52.1% break-even —
a much higher hit rate that is still not profitable. Kept here as a reminder
that **win rate is not the objective**; the payoff ratio is half the trade.

### Floor: do not go below ~2 days

Cost drag passes 5%, and the 5bps slippage assumption in
[backtest.py:39](training/gs_training/backtest.py#L39) becomes progressively
less defensible as trades speed up. Below that you are in the microstructure
game this project was scoped to avoid, and you would need the book data that
does not exist yet.

---

## 3. Bug-shaped finding: sample weights are ~2.5× too pessimistic

[labels.py:193](training/gs_training/labels.py#L193) sets
`label_end_idx = entry_i + spec.max_bars - 1` unconditionally, with the comment
that purging must assume the full horizon. **That is correct for purging.** But
the same array is then passed to `average_uniqueness` as the `sample_weight`
source in [train.py:105](training/gs_training/train.py#L105), and uniqueness is
defined over the *actual* barrier-touch time — a trade that resolved in six
hours does not occupy fourteen days of the concurrency grid.

| spec | eff. n as weighted today | eff. n by actual exits | gain |
|---|---|---|---|
| 14d 1.0/0.5 | 1,448 | 3,566 | 2.5× |
| 14d 0.75/0.375 | 1,448 | 5,431 | 3.8× |
| 14d 0.5/0.25 | 1,448 | 10,611 | 7.3× |
| 7d 1.0/0.5 | 2,866 | 6,851 | 2.4× |
| 3.5d 1.0/0.5 | 5,619 | 12,599 | 2.2× |

The flat 1,448 across every barrier width in the table above is this artifact,
not a real result — the current weighting is *blind to barrier geometry
entirely*.

The absolute 2.5× understates the damage. The real cost is that a 6-hour trade
and a 14-day trade receive **identical** downweighting, so the weights carry no
relative information at all — which is the entire point of uniqueness weighting.

**Fix:** feed `exit_idx` to `average_uniqueness`, keep `label_end_idx` for
purging. Two different questions currently answered with one array. This is not
leakage: uniqueness is a label-derived quantity by construction, and the labels
are known for training samples. It costs nothing and changes no horizon.

Both call sites are worth a comment when fixed, since the next reader will
otherwise "simplify" them back together.

---

## 4. Decisions and open questions

**Settled this session:**
- Start training now; do not wait for more data.
- The 14-day horizon is negotiable and probably too long. It was a feeling, and
  the sweep says a shorter one is cheap.
- Book/OI/aggTrade features are out of scope for this model generation.

**Open — needs a decision or an experiment:**
- Final horizon and geometry. Prior from the sweep: **7d @ 1.0/0.5** or
  **14d @ 0.75/0.375**, both landing at a ~2.5-day median hold with 2–4× the
  evidence and under 3% cost drag. Decide by sweeping, not by argument.
- Where to cut the untouched holdout. Suggested: all of 2026, or the last ~9
  months.
- Whether to wire funding into `load_panel` before or after the horizon sweep.
  (Before is tempting but it confounds the sweep — see the ordering below.)

---

## 5. Next steps, in order

The ordering matters: step 1 is now a prerequisite rather than a nicety, because
the horizon has become a hyperparameter to be searched, and every search burns
folds.

1. **Reserve the untouched final holdout.** Already flagged as not-done in
   CHECKLIST.md; it has been promoted from "should" to "must, first." With ~147
   windows per symbol and a 63-point spread between identical random-entry seeds,
   this is the only defence against fitting the evaluation.
2. **Fix the uniqueness weighting** (§3). Free, independent of every other
   decision, and it changes the weights that every subsequent experiment uses —
   so do it before generating results worth keeping.
3. **Refresh the Parquet cache.** `data/parquet/` ends 2026-08-23; the archive is
   current. All numbers in this document are from the stale cache — they will
   shift slightly, not qualitatively.
4. **Sweep the horizon** on training folds only:
   `max_bars ∈ {84, 168, 336}` × `upper_mult ∈ {0.5, 0.75, 1.0}` with
   `lower_mult = upper_mult/2`. Judge on backtest performance against the
   same-count random-entry band, not on AUC.
5. **Wire funding into the loader** (§1) and re-run the winning config to measure
   what it adds. Deliberately *after* the sweep so the horizon choice is not
   confounded by a simultaneous feature change.
6. **Then** the phase-2 gate: walk-forward beats baselines net of fees across
   ≥2 regimes, and you can explain why.

---

## 6. Reproduction

```sh
# Tables in §2 and §3 (uses the local Parquet cache):
.venv/bin/python training/scripts/horizon_sweep.py

# Refresh the cache first (step 3 above):
GS_DATABASE_URL=postgresql://gs:gs@gs-archive:5433/geldscheisser \
  .venv/bin/python -m gs_training.datasets --interval 1h --cache-dir data/parquet
```

Caveats on the numbers:
- Computed against the **stale cache** (2021-01-01 → 2026-08-23, 49,440 bars ×
  10 symbols, 5.64 years). Directionally robust; the exact figures will move.
- CUSUM threshold `2.0·sigma`, `vol_span=100`, warm-up cut at bar 250 — matching
  `build_dataset`'s defaults so the sweep describes the real training sample.
- Costs are `2×0.1%` fees + `2×5bps` slippage = 0.30% round trip, matching
  `backtest.CostModel`.

---

## 7. → CHECKLIST

Worth folding into [CHECKLIST.md](CHECKLIST.md) when convenient:

- Phase 0 gate: restate the book-feature gate in **independent windows**, not
  weeks. "4–8 weeks" is wrong for any multi-day horizon and will otherwise be
  read as "nearly ready."
- Phase 1: the untouched-holdout item is now blocking, not optional.
- Phase 1: add the uniqueness weighting bug (§3) — it belongs with the other
  "plausible output while being wrong" entries, since nothing about the current
  behaviour looks broken.
- CLAUDE.md's *"Row counts are not sample size"* invariant cites ~1,456 effective
  observations. That figure is the over-conservative one; the honest number at
  the current spec is ~3,566, and it moves with barrier geometry. Update it
  alongside the fix so the two do not drift.
