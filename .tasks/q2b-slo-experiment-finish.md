# Task Q2b — Finish the SLO experiment v2 (continuation)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md` (especially §5: the **only** command you may run is
`/home/shreenipane/.local/bin/irm-test …` — any other command ends your run with no output, which is what happened to the
previous attempt), then `.tasks/q2-slo-experiment-v2.md` (the full specification), `irm/experiment.py` and
`tests/test_experiment.py`.

Touch **only** `irm/experiment.py` and `tests/test_experiment.py`.

## State
A previous attempt implemented most of Q2 in `irm/experiment.py` (`threshold_sensitivity`, `arm_plan`, `save_raw`, warm-up,
`--iters`, `nethog-rx`/`nethog-tx`, arms) but stopped before updating the tests. Current failures:
- `test_rotation…` still expects the old 3-arm rotation.
- `test_run_slo_revert_on_apply_failure` and `test_cleanup_resets_failed_units` monkeypatch collaborators with lambdas
  that do not accept the new keyword arguments (e.g. `arms`), giving `TypeError`.

## Do
1. Review `irm/experiment.py` against every numbered requirement in `.tasks/q2-slo-experiment-v2.md` and complete anything
   missing (in particular: rotation left by `r` over `arms`; `ValueError` without A; load generator weight plan applied
   and reverted in every arm; `apply_late` tracking; `rep_violation_rates`; `applied_plans` + legacy `applied_plan`;
   `metric` string; `reset-failed` on cleanup; revert of every batch in `finally`).
2. Update the three failing tests to the new design (keep what they check: rotation, revert on apply failure, cleanup
   resets failed units) — do not delete them.
3. Add the tests listed in the Q2 specification that do not exist yet.

Run `/home/shreenipane/.local/bin/irm-test tests/test_experiment.py -q` until green. Print the `AGENTS.md` §6 summary.
