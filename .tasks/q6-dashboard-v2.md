# Task Q6 — Dashboard: new result schemas, stale-plan warning

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md`, `TRD.md` §13, then `irm/dashboard.py`, `irm/static/*`, `tests/test_dashboard.py`.

**Five other agents are editing other files right now.** Touch **only** `irm/dashboard.py`, `irm/static/index.html`,
`irm/static/app.js`, `irm/static/style.css`, `tests/test_dashboard.py`. Run **only** `tests/test_dashboard.py`. Keep every
security rule (GET only, Host check, fixed static map, metric whitelist, read-only DB, CSP, `textContent` only).

## Report schemas the other agents are producing (render whatever keys exist; old files must still render)
- **`reports/slo.json`**: `conditions` may contain arms `A`, `B`, `C`, `W`, `K`. Labels: A "alone", B "co-located",
  C "co-located + irm (weight + caps)", W "co-located + weight only", K "co-located + caps only". Per arm: `p50_ms`,
  `p95_ms`, `p99_ms`, `rep_p99_ms` ({min, mean, max} or also a list), `violation_rate`, `rep_violation_rates` (list, may be
  absent), `threshold_sensitivity` ({"1.5x", "2x", "3x"} or similar keys — render all), `rps`, `errors`, `cpuhog_ips`.
  Top level: `slo_target_ms`, `iters`, `warmup_s`, `metric`, `applied_plans` ({C, W, K}) or legacy `applied_plan`,
  `apply_late_runs`.
- **`reports/forecast.json`**: `test_all` may include `lstm`, `lstm_noattn`, `last_window`, `last_window_cal`;
  `test_seasonal_subset` {lstm, lstm_noattn, seasonal_naive, seasonal_naive_cal, last_window, last_window_cal, n_windows};
  `test_arima_subset`; `attention_entropy` {mean, p5, p95, uniform}; `offsets`.
- **`reports/placement_study.json`**: `summary` may include policies `FirstFit`, `BestFit`, `ForecastFirstFit_0.8`,
  `ForecastBestFit_1.0`, `DQN`, `DQN_noK`; `ci_method` ("t" or absent = "z").

## Changes
1. SLO card: one row per arm present, in the order A, B, W, K, C; columns p50, p99, p99 range over reps, violations %,
   cpuhog iterations/s; a caption with the metric definition, target, `iters` and warm-up; a small table of threshold
   sensitivity per arm; applied plans per arm (cgroup short name, cpu.max, cpu.weight); a warning line if
   `apply_late_runs` is non-empty. Legacy files (A/B/C only, `applied_plan`) still render.
2. Forecast card: a table for the seasonal subset (all models present, pinball and coverage) above the existing tables;
   an "attention entropy X vs uniform Y" line.
3. Placement study card: iterate over whatever policies exist; caption shows the CI method.
4. Plan card: show the plan's age from `generated_at` (epoch or ISO); if older than 15 minutes show "stale plan —
   regenerate with irm recommend" and dim the table.
5. `/api/reports` unchanged except it must not fail when a report file is malformed (return `null` for that key).

## Tests
- `/api/reports` returns `null` for a malformed report file and the object for a valid one.
- Existing tests stay green.

Run `/home/shreenipane/.local/bin/irm-test tests/test_dashboard.py -q` until green. Print the `AGENTS.md` §6 summary.
