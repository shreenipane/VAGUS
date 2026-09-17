# Prototype task P4 — Cluster simulator and multi-objective DQN on a synthetic cluster

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, then `TRD.md` §9 and §10.

**Deadline: a faculty demo in 25 minutes.** Other agents are writing `irm/cli.py`, `irm/recommend.py`,
`irm/execute.py`, `irm/dashboard.py`, `irm/forecast.py` right now: never touch those files.

## Files to touch
- `irm/sim.py`, `irm/dqn.py`, `tests/test_sim.py`, `tests/test_dqn.py` (all create)

## `irm/sim.py` — TRD §9 with these prototype changes
- `synthetic_cluster(n_vms, steps, seed) -> dict` (`cpu_max`, `cpu_avg` `[V,T]` percent; `cores` from {2,4,8};
  `mem_gb = 4 × cores`; `created`, `deleted` seconds; `t_start = 0`; `step = 300`). Half the VMs peak around noon, half
  around midnight (anti-correlated diurnal sinusoids), plus noise and occasional bursts. 60% of VMs exist from `t=0`
  to the end; the rest arrive uniformly and live 2–24 h. Deterministic for a seed.
- **VM forecast** = 95th percentile of the VM's last 48 finite `cpu_max/100` values before `t`; with fewer than 12,
  use 1.0 and `has_history = 0`. No priors: the four lifetime features are 0.
- Everything else in TRD §9 as written: constants, `n_hosts`, step order, feasibility, evacuation, accounting, metrics,
  K, the 14 features in order, FirstFit/BestFit, rewards with the one-hour delay, transitions.
- `Simulator(data, t0, t1, policy, weights=(10, 1, 0.1), on_transition=None).run() -> dict` (metrics).

## `irm/dqn.py` — TRD §10 with a smaller budget
- `QNet`, Double DQN, masked argmax, replay 20,000, batch 256, learn every 4 decisions after 500 transitions, target
  sync 500 steps, γ 0.9, Huber, Adam 1e-3, ε 1.0 → 0.05 over the first 60% of decisions.
- `demo(out_json, seed=0, episodes=20) -> dict`: `synthetic_cluster(400, 864, seed)` (3 days). Train on random 12 h
  episodes inside the first 2 days; evaluate FirstFit, BestFit, and greedy DQN on day 3. Write TRD §10's JSON
  (indent 2, parent directory created) plus `"train_seconds"`; save `models/dqn.pt` (`weights_only`-loadable). Print one
  line per episode. Must finish in under 3 minutes on CPU.

## Tests (fast)
- Hand-built 2-host, 2-VM scenario with constant utilization: exact `energy_kwh` and overload over 3 steps.
- K: identical series → 0; negated series → 1; constant or < 24 points → 0.5.
- Infeasible hosts never appear as candidates; masked argmax ignores masked entries even when their Q is highest.
- Double-DQN target on hand values.
- A tiny `demo` (`episodes=1`, monkeypatch to 40 VMs over 1.5 days if needed) writes every key.

Run `/home/shreenipane/.local/bin/irm-test -q` until green. Print the `AGENTS.md` §6 summary.
