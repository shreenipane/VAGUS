# Intelligent Resource Manager — Build Sequence

## Contents
1. [How a step runs](#1-how-a-step-runs)
2. [Sequence](#2-sequence)
3. [Commands that need a human](#3-commands-that-need-a-human)
4. [Scheduling notes](#4-scheduling-notes)

## 1. How a step runs

1. The architect writes a self-contained spec, `.tasks/NN-name.md`: goal, TRD sections, files to touch, tests to
   write, do-not-touch list. **Every line is an instruction to the coding agent unless it says the architect does it.**
2. Dispatch **from the agent workspace, never from the repository** (agy may write anywhere in its working directory):
   ```
   R="/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager"
   cd ~/.local/share/irm/agy-ws && agy -p "$(cat "$R/.tasks/NN-name.md")" --model gemini-3.8-flash-high \
       --mode accept-edits --output-format json --print-timeout 20m > "$R/.tasks/runs/NN-name.json"
   ```
   No `--sandbox` (it hides the launcher; the jail is the boundary). Never `--dangerously-skip-permissions`.
3. **Gate**, in order:
   1. `denied_actions` is empty, and `git status` shows only the files the spec lists.
   2. The agent's `IRM-TEST` line is green. The architect confirms with `irm-test -q | tail -3` (a few tokens; the
      agent's claim is never taken on its own). A green run of your own **after** reading the diff, because the tests
      are code the agent wrote.
   3. Ponytail review of the diff: unrequested abstractions, dependencies, dead code, reimplemented stdlib.
   4. SECURITY.md §3 standing checks.
   5. **GATE** steps only: the architect deletes each listed safety control and expects a red test (mutation check);
      then the `security-reviewer` agent reviews. Any Critical/High → a **fragment** fix spec → back to 3.1.
   6. Live or privileged checks listed for the phase (architect or human).
4. The architect records the step in BUILD_LOG.md and commits.

Fixes are always requested as fragments, never whole-file rewrites (Verdict lesson: a rewrite silently deleted four
passing tests).

## 2. Sequence

| # | Phase | Acceptance criteria |
|---|---|---|
| D | Docs, git, virtualenv, jail runner, agy permissions | Docs committed. Probes recorded (BUILD_LOG D.6–D.7): `irm-test` runs; other commands, chaining, substitution, and redirects are refused; environment prefixes cannot hijack the launcher; writes outside `irm/ tests/ bpf/` are refused; jailed code cannot read `~`, reach the network, see D-Bus, or write outside the binds |
| 0 | Scaffold: `irm/__main__.py`, `irm/cli.py` (`--version` only), `tests/conftest.py`, `tests/test_cli.py` | Agent runs `irm-test` green; `denied_actions` empty |
| 1 | Monitor (TRD §4) | Fixture trees → exact gauges; counter decrease → NULL; vanished cgroup skipped; missing files → NULL; retention deletes only old rows; sweeps don't drift. **Architect:** `irm bench overhead` < 1% of one core (R10); 10 min real `irm monitor` run looks sane |
| 2 | eBPF attribution (TRD §5) | `irm-test bpf` compiles. `attribute()` tests: per-CPU proportional split, zero packets → unattributed, restart → None, new cgid, invalid lines skipped. **Human:** installs packages. **Architect + human:** `sudo bpf/attrib \| irm monitor --attrib -` with a loopback UDP flood inside a user scope → ≥ 80% of NET_RX time attributed to that scope (R4–R6); networking works while loaded; `bpftool cgroup show /sys/fs/cgroup` empty after exit — **GATE** (mutations: ingress returns 0; restart detection removed) |
| 3 | Datasets (TRD §6) | Tests on tiny gzip fixtures: hash sampling deterministic, resume skips finished files, `.part` never mistaken for complete, forward-fill ≤ 2 steps only, bucket and category parsing raises on unknown strings, `t_split` formula. **Architect:** `irm data fetch` (background), `irm-test run data prepare`; R7, R8 recorded |
| 4 | Offline models (TRD §7) | Leakage test: a VM deleted at or after `t_c` never changes features; censoring rule; bucket edges; report keys. **Architect:** `irm-test run train lifetime` → `reports/lifetime.json` |
| 5 | Forecaster (TRD §8) | Pinball and coverage math on hand values; attention sums to 1; train loss falls on a learnable synthetic set; split windows never cross `t_split`; ARIMA q95 covers ≈ 95% on synthetic Gaussian AR data; save/load with `weights_only=True`. **Architect:** train + evaluate → `reports/forecast.json` |
| 6 | Simulator (TRD §9) | Hand-built 2-host scenarios give exact energy, overload, migrations, rejections; K = 0 for identical, 1 for opposite, 0.5 for constant or short series; evacuation stops when a VM has no candidate; infeasible hosts are never candidates |
| 7 | DQN (TRD §10) | Double-DQN target on hand values; masked argmax never picks an infeasible host; on a toy cluster where one host always overloads, the trained policy avoids it (seeded). **Architect:** train + evaluate → `reports/placement.json` |
| 8 | Live recommender (TRD §11) | Bucketing; source selection thresholds; squeeze keeps Σ quota ≤ avail; `"max"` cutoff; memory from max, not P95; pairs threshold; output schema |
| 9 | Execute (TRD §12) | In a fake cgroup tree: `..`, symlink escape, sibling prefix, missing `cgroup.controllers`, disallowed file, bad values rejected; dry run writes nothing; journal + revert restore exact bytes; a failure continues and exits 1; clamp. **Architect live:** a user scope with a CPU burner, apply `cpu.max`, `nr_throttled` rises, revert restores (R9) — **GATE** (mutations: prefix check, dry-run default, clamp) |
| 10 | Dashboard (TRD §13) | Bad `Host` → 421; POST → 405; metric outside whitelist → 400; `hours` bounds; security headers on every response; DB opened read-only; static map only — **GATE** (mutations: Host check, whitelist) |
| 11 | SLO experiment (TRD §14) | Unit tests for percentile pooling, violation windows, rep order. **Architect live:** `irm experiment slo` → `reports/slo.json`; no `irm-exp-*` scopes and no applied batch remain afterwards |
| 12 | Reproduce (TRD §15) | `irm-test run reproduce --synthetic` completes in the jail; figures and `RESULTS.md` produced. **Architect:** full run on real data. Architect loads the dataviz skill before writing the figure spec |
| 13 | Final | Agent: full `irm-test` green. Architect: all live checks re-run; `REPORT.md` written from the reports (method, results, limitations, threats to validity); README results table; security-reviewer on the whole repo with zero Critical/High — **GATE** |

## 3. Commands that need a human

| When | Command | Why |
|---|---|---|
| Before Phase 2 | `sudo dnf install -y clang llvm bpftool libbpf-devel elfutils-libelf-devel zlib-devel` | BPF toolchain |
| Phase 2 live | `sudo bpf/attrib \| irm monitor --attrib -` (in your own terminal) | Loading BPF needs root (`unprivileged_bpf_disabled = 2`) |
| Phase 2 live | `sudo bpftool cgroup show /sys/fs/cgroup` | Confirms detachment |
| Any time | Decide on removing agy's global external command / `command(cp)` rules | SECURITY.md §4 |

## 4. Scheduling notes

- Start `irm data fetch` as soon as Phase 3 is accepted: it streams ~17 GB (20 × ~856 MB) and keeps ~2% of rows.
  Phases 4–7 are built and tested on synthetic data while it runs.
- Phases run one at a time: 15 GiB RAM, and parallel agent runs would make gating ambiguous.
- Long runs (training, evaluation, reproduce) use `irm-test run …` in the background, so agent-written code stays
  jailed even when the architect runs it.
