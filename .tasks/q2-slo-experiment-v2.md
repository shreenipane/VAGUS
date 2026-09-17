# Task Q2 — SLO experiment v2: real reservation, ablation arms, persisted raw data

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md`, `council meeting.md` §4.3, then `irm/experiment.py` and `tests/test_experiment.py` fully.

**Five other agents are editing other files right now.** Touch **only** `irm/experiment.py` and
`tests/test_experiment.py`. Run **only** `tests/test_experiment.py`. `irm/execute.py`'s `apply` may gain a
`max_age_minutes` parameter (default 15) — plans you create are fresh, so call it without that parameter.

## Why
The council found the recorded result cannot show *why* latency improved: the protected service was idle while monitored
(reserve 0.00), there were no ablation arms, per-request work varied, raw latencies were discarded, the load generator was
unprotected, and the network hog's sender and receiver shared one cgroup.

## Changes to `run_slo(out_json, minutes=3, reps=3, rate=200, arms=("A","B","C","W","K"), raw_dir=None)`
1. **Arms.** A: service alone. B: service + cpuhog + nethog. C: B + irm plan (service weight + hog caps). W: B + weight only
   (service `cpu.weight=1000`, no caps). K: B + caps only (the recommender's hog items, no service item). Rotation: rep `r`
   uses `arms` rotated left by `r` positions. The SLO target is 2 × p99 of the first A run (A must be in `arms`; otherwise
   raise `ValueError`).
2. **Warm-up under load.** Every arm uses the same timeline: start service → start load generator immediately with
   `--warmup 70 --seconds <minutes×60>` → start hogs (B/C/W/K) → the driver waits 60 s. In C and K it runs
   `monitor.run(tmp_db, …, interval=1, duration=60)` during those 60 s, so the service is **under load** while profiled;
   in A/B/W it just sleeps 60 s. Then (C/W/K) build and apply the arm's plan. If apply finishes after the warm-up ends,
   set `apply_late: true` for that run. The load generator records only requests scheduled at or after the warm-up
   offset, and reports offsets relative to it.
3. **Plans.** A pure helper `arm_plan(arm, recs, service_cg) -> dict` returns a recs-shaped dict with a fresh
   `generated_at`: C → all `recs` items; K → recs items except the service's; W → exactly one item
   `{"cgroup": service_cg, "cpu_max": None, "memory_high": None, "cpu_weight": 1000, …}` (W does not call `recommend`).
   C and K call `recommend(tmp_db, …, protect=[service_cg], only=[cpuhog_cg, nethog_tx_cg, nethog_rx_cg],
   min_samples=30, hours=1, min_cores=0.0)`.
4. **Load generator protected in every arm.** Right after it starts, apply a one-item plan giving its scope
   `cpu.weight=1000` (journaled); revert it in cleanup. This removes the client-side bias between arms.
5. **Fixed request work.** `run_slo` calls `calibrate_iters()` five times at the start, takes the median, and passes
   `--iters N` to every `service` start (new required-if-given role argument; the role still calibrates when it is absent).
   Record `iters` in the output.
6. **Network hog split.** Replace the single `nethog` role with `nethog-rx --port P --seconds S` (drains UDP) and
   `nethog-tx --port P --seconds S --senders 2`, each in its own scope, so sender and receiver are different cgroups.
7. **Hog throughput.** Keep the measurement window aligned with the load generator (`--delay` = warm-up) and record
   `cpuhog_ips` for every arm with hogs.
8. **Raw data.** A pure helper `save_raw(path, records, errors, meta)` writes gzip-compressed JSON. Every run saves
   `raw_dir/rep<r>_<arm>.json.gz` with its records. Default `raw_dir` = `HOME / "reports" / "slo_raw"`.
9. **Summary.** Per arm, in addition to existing keys: `rep_p99_ms` as a list plus `{min, mean, max}`,
   `rep_violation_rates` (list), and `threshold_sensitivity` = violation rate at 1.5×, 2× and 3× the first A p99.
   Output JSON: `{"slo_target_ms", "iters", "minutes", "reps", "rate", "warmup_s": 70, "arms", "conditions": {arm: …},
   "applied_plans": {"C": items, "W": items, "K": items} (from the first rep), "applied_plan": <C items, for backward
   compatibility>, "raw_dir", "apply_late_runs", "started_at", "finished_at", "metric": "fraction of 1-second windows
   whose end-to-end p99 exceeds slo_target_ms"}`.
10. **Cleanup.** Every scope stopped and `reset-failed`; every applied batch (plans and load-generator weights) reverted, in
    `finally`, including on exceptions and Ctrl-C.

## Tests (fast; nothing that needs systemd unless `@pytest.mark.live`)
- Rotation of five arms over three reps.
- `arm_plan` for C, W, K (W has one item with only `cpu_weight`; K drops the service).
- Load generator in-process with `warmup=0.5, seconds=1.0` at 50 rps: records start at offset ≥ 0 and no request scheduled
  before the warm-up is recorded.
- `save_raw` round-trip.
- Threshold sensitivity on hand-made windows.
- `run_slo` raises `ValueError` without arm A.
- Existing tests stay green (update only those that encoded the old single `nethog` role or old schema, and say which).

Run `/home/shreenipane/.local/bin/irm-test tests/test_experiment.py -q` until green. Print the `AGENTS.md` §6 summary.
