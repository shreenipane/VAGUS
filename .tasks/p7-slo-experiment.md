# Task P7 — Live SLO experiment

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `TRD.md` §14, then `irm/monitor.py` (`run`), `irm/recommend.py` (`recommend`),
`irm/execute.py` (`apply`, `revert`), `irm/cli.py`.

Other agents are editing `irm/recommend.py`, `irm/sim.py`, `irm/dqn.py`, `irm/dashboard.py`, `irm/static/*` right now.
Touch **only** `irm/experiment.py`, `irm/cli.py`, `tests/test_experiment.py`.

## Goal
Prove or disprove the proposal's SLA claim on this machine: p99 latency of a latency-sensitive service next to noisy
neighbours, (A) alone, (B) co-located, (C) co-located with `irm` limits applied. The architect runs the real experiment
(it needs systemd and cannot run in your jail); you build it and test the pure parts plus a loopback smoke test.

## `irm/experiment.py`
Roles (run as `python -m irm.experiment <role> …`; `__main__` dispatches with `argparse`):
- `service --port P`: `asyncio.start_server` on 127.0.0.1. At start, calibrate `iters` so that
  `iters × sha256(64 KiB of zero bytes)` takes about 2 ms (time 50 single iterations). Per keep-alive connection: read a
  request up to `\r\n\r\n`, do the work, reply `HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok`.
- `loadgen --port P --rate R --seconds S --out FILE --seed N`: open-loop Poisson arrivals (exponential gaps, seeded) over
  32 keep-alive connections held in an `asyncio.Queue`. Each request is launched at its **scheduled** time and its latency
  is `completion − scheduled` (queueing for a connection counts). Errors are counted and the connection re-opened. Stop
  scheduling after `S` seconds, wait ≤ 5 s for in-flight requests. Write `FILE` as JSON:
  `{"records": [[scheduled_offset_s, latency_ms], …], "errors": n}`.
- `cpuhog --procs N --seconds S --out FILE`: `N` `multiprocessing` busy-loop workers counting iterations; write
  `{"iters_per_s": total / S}`.
- `nethog --seconds S`: one process drains a UDP socket on 127.0.0.1; two processes send 1,400-byte datagrams to it as
  fast as possible. Loopback only.

Pure helpers (tested):
- `rotation(reps) -> list[list[str]]`: `["A","B","C"]`, `["B","C","A"]`, `["C","A","B"]`, then repeat.
- `percentiles(lat_ms) -> {"p50_ms", "p95_ms", "p99_ms"}` (numpy).
- `window_p99(records, window_s=1.0) -> list[float]`: p99 per window by scheduled offset (windows with no records skipped).
- `violation_rate(window_p99s, target_ms) -> float`.
- `parse_cgroup(text) -> str`: the path from a `/proc/<pid>/cgroup` line `0::/path`.
- `summarize(runs, target_ms) -> dict`: per condition, pooled percentiles, `rep_p99_ms` {min, mean, max},
  `violation_rate` over all windows, `rps` (records / seconds), `errors`, `cpuhog_ips` (mean, or null for A).

Driver `run_slo(out_json, minutes=3, reps=3, rate=200) -> dict`:
- Start each role with `subprocess.Popen(["systemd-run", "--user", "--scope", "--quiet", f"--unit=irm-exp-{role}-{hex8}",
  "--", sys.executable, "-m", "irm.experiment", role, …], cwd=HOME)`. The Popen pid is the role process; get its cgroup
  by reading `/proc/<pid>/cgroup` (retry up to 5 s until the path ends with the unit name + `.scope`).
- Pick a free port by binding a socket to 127.0.0.1:0 and closing it. Wait (≤ 10 s) until the service accepts.
- Per rep, per condition in `rotation` order:
  - A: service; then loadgen for `minutes × 60` s.
  - B: service + cpuhog (`os.cpu_count()` procs) + nethog; wait 10 s; then loadgen.
  - C: as B, then `monitor.run(tmp_db, "/sys/fs/cgroup", "/proc", interval=1, retention_hours=1, duration=60)`;
    `recs = recommend(tmp_db, "/sys/fs/cgroup", protect=[service_cg], only=[cpuhog_cg, nethog_cg], min_samples=30,
    hours=1, min_cores=0.0)`; `apply(tmp_db, recs, "/sys/fs/cgroup", allow=[], yes=True)`; record `recs["items"]` as
    `applied_plan`; then loadgen.
  - `finally` for every condition: `systemctl --user stop <unit>.scope` for every started unit; for C, `revert(tmp_db,
    "/sys/fs/cgroup")` if apply ran. Never leave a scope or a limit behind, including on exceptions and Ctrl-C.
- SLO target = 2 × p99 of the first A run. Write `out_json` (indent 2, parent created):
  `{"slo_target_ms", "minutes", "reps", "rate", "conditions": {"A": …, "B": …, "C": …}, "applied_plan": […],
  "started_at", "finished_at"}`. Print one progress line per condition.

## `irm/cli.py` (you own it in this batch; lazy imports)
- `experiment slo` (`--minutes 3`, `--reps 3`, `--rate 200`): `run_slo(HOME / "reports" / "slo.json", …)`.
- `evaluate study` (`--seeds 5`, `--episodes 20`): `from irm.dqn import study; study(HOME / "reports" /
  "placement_study.json", seeds=list(range(seeds)), episodes=episodes)` (another agent writes `study`).

## Tests (`tests/test_experiment.py`)
`rotation(4)`; `percentiles` on 1..100; `window_p99` groups by second; `violation_rate`; `parse_cgroup`; `summarize` on
hand-made runs. Loopback smoke: run the `service` coroutine in a thread on port 0, then the `loadgen` logic for 1 s at
50 rps → ≥ 30 records, 0 errors. Mark nothing that needs systemd as non-live: any test that would start `systemd-run`
must be `@pytest.mark.live`.

Run `/home/shreenipane/.local/bin/irm-test tests/test_experiment.py -q` until green. Print the `AGENTS.md` §6 summary.
