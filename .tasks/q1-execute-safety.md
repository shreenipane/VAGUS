# Task Q1 — Execute and recommend safety fixes, CLI wiring (council action plan)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md`, `council meeting.md` §4.2, §4.6, then `irm/execute.py`, `irm/recommend.py`, `irm/cli.py` and
their tests.

**Five other agents are editing other files right now.** Touch **only** `irm/execute.py`, `irm/recommend.py`,
`irm/cli.py`, `tests/test_execute.py`, `tests/test_recommend.py`, `tests/test_cli.py`. Run **only** those test files.

## 1. `apply` refuses stale plans (`irm/execute.py`)
- New parameters: `apply(..., max_age_minutes: float | None = 15.0, now: float | None = None)`.
- Read `recs["generated_at"]`, accepting epoch seconds (int/float) or an ISO-8601 string (use `datetime.fromisoformat`).
  If `max_age_minutes` is not `None` and the timestamp is missing, unparseable, or older than `max_age_minutes`, print
  `refusing stale plan: generated <N> min ago (limit <M>); regenerate with irm recommend` (or `…: no generated_at`),
  write **nothing** (no files, no journal rows), and return 1. This check runs before anything else, dry run included.
- `max_age_minutes=None` disables the check.

## 2. `revert` never overwrites a value someone else changed
- New parameter: `revert(..., force: bool = False)`.
- Before writing `old` back, read the file's current value. Compare it with the journaled `new` after normalizing:
  strip whitespace; for `cpu.max`, `"max"` equals `"max 100000"` (the kernel appends the default period).
- If they differ and `force` is `False`: do not write; set the row's status to `revert_skipped:changed`; print
  `skipping <cgroup> <file>: current value <cur> differs from applied <new> (use --force)`; the function returns 1 at the end.
- An unreadable current value counts as differing.

## 3. Protected cgroups keep existing limits (`irm/recommend.py`)
Protected items must no longer erase operator limits: `cpu_max` and `memory_high` become `None`; `cpu_weight` stays 1000;
reason: `protected; peak X.XX cores (empirical); CPU weight raised, existing limits kept`. The budget math (`reserve`) is
unchanged. Also make sure every plan carries `generated_at` (keep the existing format if present).

## 4. CLI wiring (`irm/cli.py`) — lazy imports inside each branch
- `apply`: `--max-age-minutes` (float, default 15; `0` means no check → pass `None`).
- `revert`: `--force`.
- New `control` subcommand: `--db`, `--root /sys/fs/cgroup`, `--proc /proc`, `--protect` (nargs `+`, required),
  `--only` (nargs `+`, required), `--interval 60`, `--settle 30`, `--psi-margin 5.0`, `--iterations` (int, default None),
  `--min-samples 12`, `--hours 1.0` → `from irm.control import run; run(db, root, proc, protect, only, interval=…,
  settle=…, psi_margin=…, max_iterations=…, min_samples=…, hours=…)` (another agent writes `irm/control.py`).
- `experiment slo`: add `--arms` (default `"A,B,C,W,K"`, comma-separated) and `--raw-dir` (default
  `HOME/reports/slo_raw`) → `run_slo(out, minutes=…, reps=…, rate=…, arms=[…], raw_dir=…)` (another agent adds these
  parameters).

## Tests (add; keep existing ones green, update only assertions that encoded the old protected-item behaviour, and say which)
- A plan with `generated_at` 20 min old → return 1, no file written, no journal rows; 1 min old → applies; missing →
  refused; `max_age_minutes=None` → applies.
- Apply then change the file by hand then `revert` → value kept, status `revert_skipped:changed`, returns 1; with
  `force=True` → restored.
- Apply `cpu.max = max` on a file that then reads `max 100000` → `revert` restores it (normalization).
- Protected item has `cpu_max is None`, `memory_high is None`, `cpu_weight == 1000`.
- CLI parses `control` and `experiment slo --arms A,C`.

Run `/home/shreenipane/.local/bin/irm-test tests/test_execute.py tests/test_recommend.py tests/test_cli.py -q` until green.
Print the `AGENTS.md` §6 summary.
