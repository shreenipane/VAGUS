# Task Q5 — Closed control loop with pressure-based rollback; IRQ pressure in the monitor

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md`, `council meeting.md` §4.2 (the "closed loop" claim), then `irm/monitor.py`, `irm/recommend.py`,
`irm/execute.py`, `tests/test_monitor.py`.

**Five other agents are editing other files right now.** Touch **only** `irm/control.py` (create), `irm/monitor.py`,
`tests/test_control.py` (create), `tests/test_monitor.py`. Run **only** those two test files. Another agent is adding
`max_age_minutes` to `execute.apply` (default 15) and `force` to `execute.revert` — the plans you apply are fresh, so call
`apply` and `revert` without those parameters. It is also wiring the `irm control` CLI to your `run()` below.

## Why
VAGUS is described as a closed-loop autonomic system, but recommend/apply/revert are one-shot commands with no feedback
or rollback. This adds a minimal real loop.

## 1. `irm/control.py`
```
run(db_path, root, proc, protect, only, interval=60.0, settle=30.0, psi_margin=5.0, max_iterations=None,
    min_samples=12, hours=1.0, headroom=1.25, min_cores=0.0,
    recommend_fn=None, apply_fn=None, revert_fn=None, read_psi=None, sleep=time.sleep, log=print) -> int
```
Defaults: `recommend_fn=irm.recommend.recommend`, `apply_fn=irm.execute.apply`, `revert_fn=irm.execute.revert`,
`read_psi(root, cgroup) -> float | None` reads `some avg10` from `<root><cgroup>/cpu.pressure`.

Each iteration:
1. `recs = recommend_fn(db_path, root, protect=protect, only=only, min_samples=…, hours=…, headroom=…, min_cores=…)`.
2. If there are no items, or every item's non-null `cpu_max`/`memory_high`/`cpu_weight` already equals the current file
   value (normalize `cpu.max` `"max"` = `"max 100000"`), log `iteration N: no change` and sleep `interval`.
3. Otherwise: `before` = mean `read_psi` over protected cgroups (ignore `None`); `apply_fn(db_path, recs, root, yes=True)`;
   if it returns non-zero, log and continue. Sleep `settle`. `after` = the same mean.
4. If `after > before + psi_margin`: `revert_fn(db_path, root)` and log
   `iteration N: rollback (protected PSI before X → after Y)`; else log `iteration N: kept (PSI X → Y)`.
5. Sleep `interval − settle` (at least 0). Stop after `max_iterations` if given.

On exit (normal, exception, or `KeyboardInterrupt`), revert every batch this run applied and did not already roll back
(call `revert_fn` once per such batch, most recent first). Return 0, or 1 if any apply/revert failed.

## 2. IRQ pressure in `irm/monitor.py`
- New metric column `irq_psi` appended **at the end** of `COLUMNS` and the schema: host from `<proc>/pressure/irq`,
  leaves from `irq.pressure`; both files have only a `full` line, so parse `full avg10`. Missing file → `NULL`.
- `open_db` must upgrade existing databases: if `samples` lacks `irq_psi`, run
  `alter table samples add column irq_psi real` (check with `pragma table_info`).
- Do not change any other behaviour or signature.

## Tests
- `tests/test_control.py` with fakes (no real cgroups): PSI rising beyond the margin → exactly one revert and a rollback
  log; stable PSI → no revert; an unchanged plan → `apply_fn` not called; exiting after `max_iterations=2` with one kept
  batch → that batch reverted on exit; `KeyboardInterrupt` from `sleep` → applied batches reverted.
- `tests/test_monitor.py`: `irq_psi` parsed from a `full avg10=…` line for host and leaf; `open_db` on a database created
  with the old schema adds the column and existing rows survive.

Run `/home/shreenipane/.local/bin/irm-test tests/test_control.py tests/test_monitor.py -q` until green. Print the
`AGENTS.md` §6 summary.
