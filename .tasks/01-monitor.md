# Task 1 — Monitor

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Paths below are relative
to it; pass absolute paths to your file tools.

Read and follow `AGENTS.md`, then `TRD.md` §2, §3, §4, §16.

## Goal
Passive telemetry: every interval, read leaf cgroups and `/proc`, compute the gauges of TRD §4.3, and store one wide
row per cgroup plus a `host` row in SQLite. Plus the overhead benchmark (§4.5).

## Files to touch
- `irm/monitor.py` (create)
- `irm/cli.py` — add subcommands `monitor` and `bench overhead` (options exactly as TRD §2)
- `tests/conftest.py` — add fixture builders
- `tests/test_monitor.py` (create)

## Interfaces (`irm/monitor.py`)
All paths are parameters so tests can use fixture trees.

- `COLUMNS`: tuple of the 13 metric column names of TRD §4.4, in schema order (everything except `ts`, `cgroup`).
- `discover(root) -> list[str]`: TRD §4.1, sorted.
- `read_cgroup(root, name) -> dict | None`: raw values `usage_usec`, `nr_periods`, `nr_throttled`, `mem`, `rbytes`,
  `wbytes`, `cpu_psi`, `mem_psi`, `io_psi`, `pids`. A missing file, or `FileNotFoundError`/`OSError` with `ENODEV`,
  gives `None` for its values. If the cgroup directory itself no longer exists, return `None`. An `io.stat` that exists
  but is empty gives 0 for both byte counts.
- `read_host(proc) -> dict`: `busy_ticks` (user+nice+system+irq+softirq+steal), `softirq_ticks`, `mem`, `cpu_psi`,
  `mem_psi`, `io_psi` (from `<proc>/pressure/*`).
- `leaf_gauges(prev, cur, dt) -> dict` and `host_gauges(prev, cur, dt, tck) -> dict`: TRD §4.3. `prev` may be `None`.
  Any counter that decreased, or whose previous value is missing, gives `None` for its rate. Every key of `COLUMNS`
  is present in the result; the Phase 2 attribution columns are `None`.
- `sweep(root, proc, state, now_wall, now_mono) -> (rows, state)`: `state` is `{"mono": float|None, "leaf": {name:
  raw}, "host": raw|None}` (start with `None` for a fresh one). `rows` are dicts with `ts=int(now_wall)`, `cgroup`, and
  every column. Cgroups that vanished are dropped from the state.
- `open_db(path) -> sqlite3.Connection`: creates parent directories, the schema of TRD §4.4 verbatim, and
  `pragma journal_mode=wal`.
- `write_rows(conn, rows)`: one transaction, `insert or replace`, SQL values as parameters.
- `prune(conn, now_wall, retention_hours) -> int`: deletes rows with `ts < now_wall − retention_hours × 3600`, returns
  the count.
- `next_deadline(deadline, interval, now_mono) -> float`: `deadline + interval`; if that is already in the past, skip to
  the first future slot `deadline + k × interval` (never burst to catch up).
- `run(db_path, root, proc, interval, retention_hours, duration=None)`: loop of sweep → write → sleep until
  `next_deadline`; prune at start and then once per hour; stop after `duration` seconds when given. Returns the number
  of sweeps.
- `bench(seconds, interval, root, proc) -> dict`: runs `run` into a database in a `tempfile.TemporaryDirectory`, measures
  `time.process_time()` and wall time, returns the TRD §4.5 dict. The CLI writes it to `HOME/reports/overhead.json`
  (indent 2) and prints it.

`os.sysconf("SC_CLK_TCK")` is read once in the CLI path and passed down.

## Fixture builders (`tests/conftest.py`)
- `make_cgroup(root, name, **files)`: creates `root/<name>` and writes each `file_name=text` (map `cpu_stat` →
  `cpu.stat`, i.e. replace the first `_` with `.`; document that in the docstring).
- `make_proc(proc, stat, meminfo, cpu_psi, mem_psi, io_psi)`: writes `stat`, `meminfo`, `pressure/{cpu,memory,io}`.
Use real file formats, e.g. `cpu.stat` lines `usage_usec 1000000`, pressure lines
`some avg10=1.50 avg60=0.00 avg300=0.00 total=10`, `io.stat` lines
`259:0 rbytes=100 wbytes=200 rios=1 wios=2 dbytes=0 dios=0`.

## Tests to write (`tests/test_monitor.py`)
1. Two sweeps 5 s apart on a fixture leaf: exact `cpu_cores`, `throttled_ratio`, `io_rbps`, `io_wbps`; `mem_bytes`,
   PSI, and `pids` from the second read.
2. `throttled_ratio` is 0.0 when `nr_periods` did not change, and `None` when those fields are absent from `cpu.stat`.
3. A counter that decreased gives `None` for that rate only.
4. First sighting: rates `None`, instantaneous gauges present.
5. Missing `memory.current` → `mem_bytes` is `None`. A cgroup directory removed after `discover` is skipped, and the
   sweep still returns the other rows.
6. `discover`: a non-leaf and a `populated 0` leaf are excluded.
7. Host row from a fixture `/proc`: `cpu_cores`, `softirq_cores`, `mem_bytes = (MemTotal − MemAvailable) × 1024`.
8. `prune` deletes only rows older than the retention window.
9. `next_deadline` skips missed slots after an overrun.
10. `open_db` enables WAL; `write_rows` twice with the same `(cgroup, ts)` leaves one row.

## Run
`/home/shreenipane/.local/bin/irm-test -q` until green.

## Do not touch
Everything not listed above.

## The architect does these after your run (do not do them)
`irm-test run bench overhead` (R10) and a 10-minute real `irm monitor` run.

## Final summary
Print the block from `AGENTS.md` §6.
