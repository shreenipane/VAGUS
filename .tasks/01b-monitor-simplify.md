# Task 1b — Monitor: remove duplication (fragment)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Paths below are relative
to it; pass absolute paths to your file tools.

Read and follow `AGENTS.md` (especially §2), then `irm/monitor.py` and `tests/test_monitor.py`.

## Why
Task 1 passed its tests, but `irm/monitor.py` is 555 lines and the architect's benchmark measured 0.97% of one core,
right at the 1% target. The file repeats the same missing-file / `ENODEV` / vanished-directory handling in six places,
checks `is_dir()` after every failed read, and computes rates with three copies of the same code.

## Change (only `irm/monitor.py`)
1. One helper reads a cgroup file: returns the text, or `None` when the file is missing or reading raises `ENODEV`. Any
   other `OSError` propagates. Use `os.path.join` strings, not `Path`, in `discover` and `read_cgroup` (they run ~135
   times every sweep).
2. `read_cgroup`: read each file once through the helper. Decide "the cgroup vanished" with a **single**
   `os.path.isdir` check, made only if at least one read returned `None`. Delete `_CgroupVanished`.
3. Small parsers: `cpu.stat` key/value lines, `io.stat` byte sums, pressure `some avg10`. Reuse the pressure parser
   for the host.
4. `leaf_gauges` and `host_gauges`: one rate helper that returns `None` when either value is missing or the counter
   decreased.
5. `except (FileNotFoundError, OSError)` → `except OSError`.
6. Keep every public name, signature, and behaviour of task 1 exactly. Keep `COLUMNS`, `SCHEMA`, `run`'s `on_sweep`.

Target: the file under 300 lines. Comments explain why, not what.

## Tests
Do **not** change any test. `/home/shreenipane/.local/bin/irm-test -q` must stay green (15 passed).

## Do not touch
Every file except `irm/monitor.py`.

## The architect does this after your run (do not do it)
Re-runs `irm-test run bench overhead`.

## Final summary
Print the block from `AGENTS.md` §6, plus the new line count of `irm/monitor.py`.
