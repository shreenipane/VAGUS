# Task P9 — Dashboard: SLO experiment and placement study results

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `TRD.md` §13, `irm/dashboard.py`, `irm/static/*`, `tests/test_dashboard.py`.

Other agents are editing `irm/cli.py`, `irm/recommend.py`, `irm/experiment.py`, `irm/sim.py`, `irm/dqn.py` right now.
Touch **only** `irm/dashboard.py`, `irm/static/index.html`, `irm/static/app.js`, `irm/static/style.css`,
`tests/test_dashboard.py`. Keep every TRD §13 security rule (textContent only, no inline script/style, headers, Host
check, GET only).

## Change
1. `/api/reports` also returns `"slo"` (`reports/slo.json`) and `"placement_study"` (`reports/placement_study.json`),
   null when missing.
2. New card **"Proof: SLO experiment"** (placeholder "Run: irm experiment slo" when null). Table rows A "alone", B
   "co-located", C "co-located + irm"; columns p50 ms, p95 ms, p99 ms, p99 range (min–max over reps), SLO violations %,
   cpuhog iterations/s (— for A). Above it: "SLO target: X ms (2 × p99 alone) · N reps × M min at R rps". Below it: the
   `applied_plan` items (cgroup short name, cpu.max, cpu.weight). Highlight the lowest p99 among B and C.
   JSON schema: `{"slo_target_ms", "minutes", "reps", "rate", "conditions": {"A"|"B"|"C": {"p50_ms", "p95_ms", "p99_ms",
   "rep_p99_ms": {"min", "mean", "max"}, "violation_rate", "rps", "errors", "cpuhog_ips"}}, "applied_plan": [items]}`.
3. New card **"Plan: placement study"** (placeholder "Run: irm evaluate study" when null). Rows FirstFit, BestFit, DQN,
   DQN_noK; columns energy kWh, SLA overload %, overloaded host-steps %, migrations, active hosts — each "mean ± ci95".
   Caption: "N seeds · util ×U · host slack ×S". Schema: `{"seeds", "episodes", "util_scale", "host_slack", "summary":
   {policy: {metric: {"mean", "ci95"}}}}` for metrics `energy_kwh`, `sla_overload_frac`, `overloaded_host_step_frac`,
   `migrations`, `mean_active_hosts`.
4. Numbers use `toFixed` with sensible precision; percentages ×100.

## Tests
`/api/reports` includes `slo` and `placement_study` keys (null when missing, the object when a fixture file exists).
Existing tests stay green.

Run `/home/shreenipane/.local/bin/irm-test tests/test_dashboard.py -q` until green. Print the `AGENTS.md` §6 summary.
