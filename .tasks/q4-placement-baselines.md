# Task Q4 — Placement: forecast-aware baselines, t-based CIs, training-only cluster sizing

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md`, `council meeting.md` §4.4, then `irm/sim.py`, `irm/dqn.py`, `tests/test_sim.py`, `tests/test_dqn.py`.

**Five other agents are editing other files right now.** Touch **only** `irm/sim.py`, `irm/dqn.py`, `tests/test_sim.py`,
`tests/test_dqn.py`. Run **only** those two test files.

## Why
The study compared the DQN only with First-Fit/Best-Fit, which ignore utilisation. Forecast-aware heuristics recovered
~60% of the DQN's gain. CIs used z=1.96 at n=5, and the cluster size was computed from a peak that included the test day.

## Changes
1. **Policies (`irm/sim.py`).** Using the existing 14 candidate features (index 1 = `host_alloc_frac`, index 13 =
   `post_q95_frac`):
   - `ForecastBestFit(cap=1.0)`: among candidates with `post_q95_frac ≤ cap`, choose the highest `host_alloc_frac` after
     placement (ties → lowest host id); if none qualifies, choose the lowest `post_q95_frac`.
   - `ForecastFirstFit(cap=0.8)`: the lowest host id with `post_q95_frac ≤ cap`; if none, the lowest `post_q95_frac`.
2. **Study (`irm/dqn.py`, `study`).**
   - Evaluate policies `FirstFit`, `BestFit`, `ForecastFirstFit_0.8`, `ForecastBestFit_1.0`, `DQN`, `DQN_noK`.
   - Size the cluster from the **training days only** (`t < 172800`), not the whole trace.
   - Confidence intervals: `ci95 = t_{0.975, n−1} × sample std / √n` using a small hard-coded table of two-sided 95%
     critical values for df 1–30 (df 4 = 2.776), 1.96 beyond; `ci95 = 0.0` when n = 1. Add `"ci_method": "t"`.
   - Add `"sizing": "training days only"` to the output. Keep every existing key.
   - `demo()` behaviour unchanged.

## Tests
- `ForecastBestFit` respects the cap and falls back to the lowest `post_q95_frac`; `ForecastFirstFit` likewise.
- The t critical value for df=4 is 2.776; for df=100 it is 1.96; n=1 gives 0.
- Cluster sizing ignores a VM that exists only after `t = 172800` (construct a tiny dataset where that VM would change the
  host count).
- A tiny `study` (`seeds=(0,1)`, `episodes=1`, `n_vms=30`) contains all six policies and `ci_method == "t"`.

Run `/home/shreenipane/.local/bin/irm-test tests/test_sim.py tests/test_dqn.py -q` until green. Print the `AGENTS.md` §6
summary.
