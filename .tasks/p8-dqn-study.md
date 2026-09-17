# Task P8 — DQN study: stressed cluster, 5 seeds, no-K ablation

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `TRD.md` §9–§10, `irm/sim.py`, `irm/dqn.py`, `tests/test_sim.py`, `tests/test_dqn.py`.

Other agents are editing `irm/cli.py`, `irm/recommend.py`, `irm/experiment.py`, `irm/dashboard.py`, `irm/static/*`
right now. Touch **only** `irm/sim.py`, `irm/dqn.py`, `tests/test_sim.py`, `tests/test_dqn.py`. Do not change existing
behaviour of `demo()` or any existing default.

## Why
`reports/placement.json` shows FirstFit and BestFit with identical metrics and `mean_active_hosts` equal to `n_hosts`:
every host is always on, so placement barely matters and the DQN result (overload 2.01% → 1.75%, one seed) proves
little. Nothing shows the co-location coefficient K matters.

## Changes
1. `synthetic_cluster(n_vms, steps, seed, util_scale=1.0)`: multiply utilization by `util_scale` before clipping to
   0–100. Default keeps current output identical.
2. `Simulator(..., use_k=True)`: when `False`, the K feature is the constant 0.5 (everything else unchanged).
3. Extract from `demo()` without changing its results: `train_dqn(data, n_hosts, episodes, seed, use_k=True,
   weights=(10, 1, 0.1)) -> nn.Module` and `evaluate(data, n_hosts, t0, t1, policies: dict[str, Policy], use_k=True)
   -> dict[str, dict]`. `demo()` calls them.
4. `study(out_json, seeds=(0, 1, 2, 3, 4), episodes=20, n_vms=400, util_scale=1.4, host_slack=2.0) -> dict`:
   for each seed: `data = synthetic_cluster(n_vms, 864, seed, util_scale)`; `n_hosts = max(4, ceil(host_slack × peak Σ
   cores / (HOST_CORES × OVERCOMMIT)))` (slack so hosts can be switched off and placement choices differ); train
   `DQN` (use_k=True) and `DQN_noK` (use_k=False) on the first 2 days; evaluate FirstFit, BestFit, DQN, DQN_noK on day 3,
   each evaluated with the `use_k` it was trained with. Print one line per seed with elapsed seconds.
   Write JSON (indent 2, parent created):
   `{"seeds", "episodes", "n_vms", "util_scale", "host_slack", "n_hosts": [per seed],
     "summary": {policy: {metric: {"mean", "ci95"}}}, "per_seed": [{policy: metrics}], "total_seconds"}` for metrics
   `energy_kwh`, `sla_overload_frac`, `overloaded_host_step_frac`, `migrations`, `mean_active_hosts`, with
   `ci95 = 1.96 × sample std / sqrt(n)` (0.0 when n = 1).

## Tests (fast)
- `util_scale=1.0` reproduces the default output exactly; `util_scale=2.0` never exceeds 100.
- `use_k=False` makes feature index 12 equal 0.5 for every candidate.
- `demo()` output keys unchanged (existing test).
- `study` on a tiny setting (`seeds=(0, 1)`, `episodes=1`, `n_vms=30`) writes every key, with 4 policies in `summary`
  and a `ci95` for each metric.

Run `/home/shreenipane/.local/bin/irm-test tests/test_sim.py tests/test_dqn.py -q` until green. Print the `AGENTS.md`
§6 summary.
