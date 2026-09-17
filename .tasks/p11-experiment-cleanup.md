# Task P11 — SLO experiment: hog throughput in C, failed-unit cleanup (fragment)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `irm/experiment.py` (`run_slo`), `tests/test_experiment.py`.
Touch **only** `irm/experiment.py` and `tests/test_experiment.py`. Smallest change; no restructuring.

## Defects found in the real run
1. `cpuhog_ips` is null for condition C. The hog runs `65 + seconds` but the driver waits only 5 s for it after the load
   generator finishes, so its output file does not exist yet. Fix: in B and C, run the hog for exactly as long as it
   overlaps the measurement — start its measurement window when the load generator starts. Simplest correct way: give the
   `cpuhog` role an optional `--delay D` (seconds to busy-loop before counting starts) and `--seconds S` (counting window),
   so it reports iterations/s over the measurement window only; in B use `--delay 10`, in C `--delay 60` plus the time the
   driver actually spent in monitor + recommend + apply (measure it and pass it, or start the hog's counting from a file
   flag the driver touches). Choose the simpler of the two and explain in a `why` comment. Then wait for the hog to exit
   (up to `delay + seconds + 15` s) before reading its file.
2. Failed scopes remain listed. After `systemctl --user stop <unit>.scope`, also run
   `systemctl --user reset-failed <unit>.scope` (errors ignored), in both cleanup places.

## Tests
- The `cpuhog` role with `--delay 0.2 --seconds 0.5 --procs 1` writes `iters_per_s > 0` and does not exit before
  `delay + seconds`.
- The cleanup path calls `reset-failed` for each started unit (monkeypatch `subprocess.run` and record calls).

Run `/home/shreenipane/.local/bin/irm-test tests/test_experiment.py -q` until green. Print the `AGENTS.md` §6 summary.
