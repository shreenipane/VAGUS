# Task 2a — Softirq attribution: userspace

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Paths below are relative
to it; pass absolute paths to your file tools.

Read and follow `AGENTS.md`, then `TRD.md` §4.3, §4.4, §5.3, §5.4, §16. Read `irm/monitor.py` before changing it.

## Goal
Turn the eBPF loader's cumulative JSON lines (TRD §5.3) into per-cgroup NET_RX softirq cores, and store them in the
monitor's attribution columns. The BPF program itself is task 2b; this task needs no compiler and no root.

## Files to touch
- `irm/attrib.py` (create)
- `irm/monitor.py` — attribution columns in `sweep` and `run`
- `irm/cli.py` — `monitor --attrib` accepts only `-` (argparse `choices`), meaning stdin
- `tests/test_attrib.py` (create)

## Interfaces
`irm/attrib.py`:
- `NET_RX = 3`.
- `attribute(prev: dict, cur: dict) -> dict | None`: exactly TRD §5.4. JSON object keys are strings; results use `int`
  cgroup ids. Rates are ns / 1e9 / Δt.
- `cgroup_ids(root) -> dict[int, str]`: `st_ino` of every cgroup directory → name (`/` for the root itself, otherwise
  as in TRD §4.1).
- `class Reader`: `Reader(stream, clock=time.monotonic)` starts a daemon thread reading lines; keeps the previous
  parsed snapshot; publishes `(clock(), attribute(prev, cur))` when the result is not `None`; counts invalid JSON lines in
  `self.invalid`. `latest(max_age) -> dict | None`. `join(timeout)` waits for the thread (end of stream), for tests.

`irm/monitor.py`:
- `sweep(..., attrib=None)`: when `attrib` is a result dict, map ids through `cgroup_ids(root)`; each leaf row gets
  `netrx_attrib_cores` and `netrx_blamed_cores` (0.0 when its id is absent from the result); the host row gets
  `netrx_unattrib_cores` and, as `netrx_blamed_cores`, the value blamed on the root cgroup (`/`), or 0.0. When `attrib`
  is `None`, these columns stay `None`.
- `run(..., attrib_stream=None)`: when given, create a `Reader` and pass `reader.latest(3 * interval)` to each sweep.

## Tests to write (`tests/test_attrib.py`)
1. Two CPUs; cgroup 10 has 30 packets on CPU 0, cgroup 20 has 10 packets on CPU 0 and 5 on CPU 1; NET_RX ns 4e9 on
   CPU 0 and 1e9 on CPU 1 over Δt = 2 s → attrib cores exactly {10: 1.5, 20: 0.5 + 0.5}.
2. A CPU with NET_RX time but no packets → that time is `unattrib_cores`.
3. Returns `None` when a cumulative value decreased, when `ts` did not increase, and when `ncpu` differs.
4. A cgroup id absent from `prev` counts from 0.
5. `blamed_cores` uses only vector 3; `vec_cores` has 10 entries.
6. `Reader` over an `io.StringIO` of three valid lines and one invalid line: `invalid == 1`; `latest` returns the result
   of the last pair; with an injected clock, `latest(max_age)` returns `None` once the result is older than `max_age`.
7. `sweep` with a fixture tree and a hand-made result keyed by the fixture directories' `st_ino`: leaf columns filled,
   an unlisted leaf gets 0.0, host row gets unattributed and root-blamed values; with `attrib=None` all four columns are
   `None`.

Existing tests must still pass unchanged.

## Run
`/home/shreenipane/.local/bin/irm-test -q` until green.

## Do not touch
Everything not listed above. No C code in this task.

## Final summary
Print the block from `AGENTS.md` §6.
