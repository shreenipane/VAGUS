# Prototype task P1 — Recommend, Execute, CLI wiring

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, then `TRD.md` §2, §3, §11, §12. Read `irm/cli.py` and `irm/monitor.py` first.

**Deadline: a faculty demo in 25 minutes.** Keep code minimal and working. Three other agents are writing
`irm/dashboard.py`, `irm/forecast.py`, `irm/sim.py`, `irm/dqn.py` at the same time: never touch those files.

## Files to touch
- `irm/recommend.py` (create), `irm/execute.py` (create), `irm/cli.py`, `tests/test_recommend.py`, `tests/test_execute.py`

## `irm/recommend.py` — TRD §11 with one prototype change
- `recommend(db_path, root, protect, headroom=1.25, min_samples=360, hours=24, now=None) -> dict`.
- **Source is always `empirical`** (no forecaster yet): for each leaf cgroup (not `host`) with at least `min_samples`
  samples in the last `hours`, `d = cpu_cores + coalesce(netrx_attrib_cores, 0)` per sample; peak = 95th percentile of
  `d` (`numpy.percentile`, ignoring NULL `cpu_cores`). Fewer samples → `skipped` with a reason.
- Steps 4–8 of TRD §11 exactly (protected: `max`/1000/`max`; reserve, `avail`, proportional squeeze, 0.1-core floor,
  `"max"` at ≥ 0.9 × ncpu, `memory.high` from the maximum `mem_bytes` × headroom rounded up to MiB, ≥ 64 MiB; `reason`
  strings; `old` values read from `root + cgroup` files, `null` if missing).
- Pairs: 30-second buckets (max of `d`), ≥ 10 common buckets, non-zero variance, ρ ≥ 0.7, top 10.
- Open the DB read-only (TRD §3).

## `irm/execute.py` — TRD §12 exactly
- `validate(root, cgroup, file, value, allow) -> str | None` (returns the rejection reason).
- `apply(db_path, recs, root, allow, yes) -> int` and `revert(db_path, root, batch=None) -> int`, printing
  `cgroup  file  old → new` lines. Journal table in the same DB (`sqlite3`, parameters only).

## `irm/cli.py` — add subcommands (lazy imports inside each branch)
- `recommend` (`--db`, `--protect` nargs `*`, `--headroom 1.25`, `--min-samples 360`, `--hours 24`, `--out`
  default `HOME/data/recommendations.json`, `--root`): writes JSON (indent 2), prints one line per item
  `cgroup  cpu.max  memory.high  reason`.
- `apply` (`--from`, `--yes`, `--allow` nargs `*`, `--root`, `--db`), `revert` (`--batch`, `--root`, `--db`).
- `dashboard` (`--port 8765`, `--db`): `from irm.dashboard import serve; serve(port, db, HOME / "reports",
  HOME / "data" / "recommendations.json")`.
- `train forecast` (`--epochs 5`, `--seed 0`): `from irm.forecast import demo; demo(HOME / "reports" /
  "forecast.json", seed, epochs)`.
- `evaluate placement` (`--episodes 20`, `--seed 0`): `from irm.dqn import demo; demo(HOME / "reports" /
  "placement.json", seed, episodes)`.

## Tests (small, fast)
- recommend: a temp DB with hand rows for 3 cgroups over 2 minutes → protected gets `max`/1000; others squeezed so Σ
  quota ≤ avail when demand exceeds `ncpu` (monkeypatch `os.cpu_count` to 4); `memory.high` from max, not P95; too few
  samples → skipped.
- execute (fake tree in `tmp_path`: directories with a `cgroup.controllers` file and value files): `..`, a symlink out
  of the prefix, and sibling prefix `user@1000.service-x` rejected; disallowed file rejected; dry run writes nothing and
  creates no journal rows; `apply --yes` then `revert` restores the exact original bytes; memory clamp.
- Existing tests stay green.

Run `/home/shreenipane/.local/bin/irm-test -q` until green. Print the `AGENTS.md` §6 summary.
