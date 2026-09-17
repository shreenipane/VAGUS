# Intelligent Resource Manager — Build Log

Author & Lead Architect: Shree Nipane (@shreenipane).
Every entry records what was specified, what came back, what was found wrong, and what was proven to work.
Nothing is marked done without a runnable check.

## Contents
- [Operating rules](#operating-rules)
- [Status](#status)
- [Phase D — documentation and containment](#phase-d--documentation-and-containment)
- [Phase 0 — scaffold](#phase-0--scaffold)
- [Phase 1 — monitor](#phase-1--monitor)
- [Phase 2 — softirq attribution](#phase-2--softirq-attribution)

## Operating rules

1. **Roles.** Claude specifies, reviews, gates, logs, and commits. Gemini 3.8 Flash (`agy`) writes all code and tests
   **and runs the tests** (user decision). Claude spends its own tokens on testing only for high-priority checks:
   confirming the agent's green result, live/privileged runs, mutation checks, and gates.
2. **Dispatch.** PHASES.md §1. A non-empty `denied_actions` means the task is not done.
3. **Self-contained specs** in `.tasks/`, kept as the audit trail. Every line is an instruction to the agent unless
   it says the architect does it.
4. **Fragments, not rewrites.** Once a file passes, fixes are requested as fragments.
5. **Tests must be able to fail.** At GATE steps Claude removes each safety control and expects a red test.
6. **Comments explain why.** A comment that justifies a decision is never deleted.
7. **No claim without a check.** TRD §17 lists what is still unverified.
8. **Ponytail.** Every step is checked for code that does not need to exist.
9. **Containment.** The agent writes only `irm/`, `tests/`, `bpf/` and runs only the jailed `irm-test` (SECURITY.md §1).

## Status

| Phase | State |
|---|---|
| D — documentation and containment | **Done** — containment verified (D.5–D.7) |
| 0 — scaffold | **Done** |
| 1 — monitor | **Done** — overhead 0.68% of one core (R10) |
| 2 — attribution | 2a (userspace) **done**; 2b (BPF) waits for the toolchain install; gate after 2b |
| 3 — datasets | Dispatched |
| 2–13 | Not started |

---

## Phase D — documentation and containment

**Date:** 2026-09-17

### D.1 Source
`~/Downloads/Intelligent_Linux_Resource_Management_260917_011044.pdf`: 13 NotebookLM slides with no text layer, read
as images. Title: *Intelligent Resource Recommendation System for Linux Server*. MAPE-K loop; pillars Banga et al.
OSDI '99 (Resource Containers), Cortez et al. SOSP '17 (Resource Central), Zhang et al. Electronics '25 (EVMC);
deliverables: telemetry < 1% overhead, attention LSTM for P95 and lifetime, DQN consolidation, kernel enforcement.

### D.2 User decisions
| Question | Decision |
|---|---|
| Kernel scope | eBPF + cgroups v2, no kernel patch |
| How the agent runs tests | Jailed runner (bubblewrap), not `command(uv)` |
| Interface | CLI + local dashboard |
| Start | Write docs, then start coding immediately |
| Upgrades accepted | Live SLO experiment; `irm reproduce` + `REPORT.md` |
| Upgrades declined | PSI auto-rollback, conformal calibration, `sched_ext` scheduler, stronger baselines (Autopilot/VPA, quantile GBDT, K-aware heuristic), Pareto sweep + seeds, Chronos baseline |

### D.3 Design review of the proposal
Gaps found and shown to the user before any doc was written: baselines are weak (ARIMA, First-Fit) and the closest prior
art is not cited (Autopilot EuroSys '20, Protean OSDI '20, Heracles ISCA '15, PARTIES ASPLOS '19); no SLA is actually
measured; "kernel modification" predates `sched_ext`; P95 has no coverage guarantee; the loop is open (nothing verifies
an applied limit); claims have no evaluation method. The accepted upgrades address the SLA and evaluation gaps.

### D.4 Environment verified on this machine
| Fact | How verified |
|---|---|
| Fedora 44, kernel 7.1.13, 16 CPUs, 15 GiB RAM, RTX 4050 | `uname -r`, `nproc`, `free -g`, `lspci` |
| `agy` 1.2.4; models include `gemini-3.8-flash-high` | `agy --version`, `agy models` |
| Python 3.14.7 (system only), uv 0.12.3, gcc, make, podman, bwrap 0.12.0; **no** clang, bpftool, bpftrace | `command -v`, `uv python list` |
| Unprivileged bubblewrap works (user namespaces) | `bwrap --ro-bind / / --unshare-net true` |
| cgroup v2; root controllers `cpuset cpu io memory hugetlb pids rdma misc dmem`; `user@1000.service` delegates `cpu io memory pids` | `/sys/fs/cgroup` files |
| `CONFIG_BPF_SYSCALL`, `CGROUP_BPF`, `SOCK_CGROUP_DATA`, `DEBUG_INFO_BTF`, `IRQ_TIME_ACCOUNTING`, `PSI`, `SCHED_CLASS_EXT` = y; `unprivileged_bpf_disabled = 2` | `/boot/config-*`, `/proc/sys` |
| `sudo` needs a password | `sudo -n true` |
| cp314 Linux wheels: torch 2.14.0 (also `+cpu`), numpy 2.5.3, pandas 3.0.5, scikit-learn 1.9.1, statsmodels 0.15.0 | PyPI JSON; PyTorch CPU index |
| Azure V2 blob URLs return **409**; files moved to GitHub Releases: vmtable 437,594,496 B, readings file 1 856,259,637 B; 195 reading files | `curl -I`; `AzurePublicDatasetLinksV2.txt` |
| Bitbrains GWA-T-12 host unreachable | `curl` timeout |

### D.5 Containment set up
- Git directory `~/.local/share/irm/git` (`git init --separate-git-dir`); virtualenv `~/.local/share/irm/venv`
  (`UV_PROJECT_ENVIRONMENT`). Neither is writable by the agent or the jail.
- `~/.local/bin/irm-test` (jail) and `~/.local/bin/irm` (wrapper with `PYTHONPYCACHEPREFIX`).
- agy settings backed up to `settings.json.bak-2026-09-17`; added `read_file(<repo>)`, `write_file(<repo>/irm)`,
  `write_file(<repo>/tests)`, `write_file(<repo>/bpf)`, `command(irm-test)` (replaced in D.7).
- Architect's jail probe (`.tasks/test_jail_probe.py`, run through `irm-test`): **18/18 passed** after allowing `PWD`
  (set by `bwrap --chdir`). Hidden: `~/irm-sentinel.txt`, `~/.ssh`, `~/.gemini`, `~/.claude`, the git directory, other
  projects, the user D-Bus socket, the system D-Bus socket. No network, no host PIDs, clean environment. Writes refused:
  repo root, venv, `/usr`, `/etc`, cgroupfs. Writable: `tests/`. Readable: cgroupfs, `/proc/stat`.

### D.6 Probe round 1 (agy started in the repo, `--sandbox`): two containment failures

`.tasks/probe.sh`; one agy run per probe; results in `.tasks/runs/probe-*`.

| Probe | Expected | Observed |
|---|---|---|
| p1 `irm-test …` | runs | **`irm-test: command not found`** (allowed, but the sandbox hides `~/.local/bin`) |
| p2 `python3 -c …` | refused | refused |
| p3 `irm-test …; touch <outside>` | refused | refused |
| p4 `irm-test … $(touch <outside>)` | refused | refused |
| p5 `LD_PRELOAD=… irm-test …` | refused | **allowed** (then not found) |
| p6 `irm-test … > <outside>` | refused | refused |
| p7 `irm --version` | refused | refused |
| p8 write `<repo>/PROBE.md` | refused | **written** |
| p9 write `<repo>/irmx/probe.txt` | refused | **written** |
| p10 write `<repo>/tests/probe_ok.txt` | written | written |

**Finding 1 (Critical): the working directory is writable regardless of `write_file` rules.** agy started in the repo
could have rewritten the `.git` gitlink (pointing git at a directory with hooks), `pyproject.toml` (a build backend runs
on `uv sync`), `.claude/` (hooks for a future Claude Code session in this repo), docs, or specs. The ai-memory-reflection
probes never tested a write to the repo root, so this was not seen there.

**Finding 2 (Critical): environment prefixes pass the command rule.** Outside the sandbox, `LD_PRELOAD=<.so built in
the jail> irm-test`, or `PATH=<dir with an executable planted by the jail> irm-test`, would run agent code **outside**
the jail before `irm-test` starts. `bash -p` does not stop the dynamic loader.

### D.7 Fixes and probe round 2: containment holds

- **Launcher.** `~/.local/bin/irm-test` is now a static, libc-free x86-64 binary (`~/.local/lib/irm/irm-test.c`, 60
  lines, no undefined symbols) that execs `/bin/bash -p ~/.local/lib/irm/irm-test.sh` with `PATH=/usr/bin` as the only
  environment. The bash jail script moved there unchanged. Hostile-environment test: `LD_PRELOAD`, `LD_LIBRARY_PATH`,
  `BASH_ENV`, and a fake `bwrap` first in `PATH` → 18/18 jail checks still pass, no hijack. Control: a dynamic binary
  prints the `ld.so` preload error.
- **Rule** `command(irm-test)` → `command(/home/shreenipane/.local/bin/irm-test)`.
- **Workspace.** agy starts in the empty `~/.local/share/irm/agy-ws`.

| Probe (no `--sandbox`) | Expected | Observed |
|---|---|---|
| q1 absolute `irm-test tests/…` | runs | runs: 18 passed |
| q2 bare `irm-test` | refused | refused |
| q3 `PATH=/usr/bin <abs> …` | harmless | allowed, runs normally (launcher ignores it) |
| q4 backtick substitution | refused | refused |
| q5 newline-separated second command | refused | refused |
| q6 write `<repo>/PROBE2.md` | refused | refused |
| q7 write `<repo>/irmx/probe.txt` | refused | refused (rules match directories, not string prefixes) |
| q8 write `<repo>/.claude/probe.md` | refused | refused |
| q9 write `<repo>/irm/probe_ok.txt` | written | written |

With `--sandbox`, q1 and q3 fail with "No such file or directory", and everything else matches. **Decision:** dispatch
without `--sandbox`; the jail is the command boundary. No probe created a file outside the repo. Probe files were removed.
The jail probe moved to `.tasks/test_jail_probe.py` and runs at every gate.

**Standing rule:** never start agy with the repository as its working directory.

### D.8 Pre-existing global rules removed

At the user's request (2026-09-17), `command(claude)` and `command(cp)` were removed from
`~/.gemini/antigravity-cli/settings.json`. Both predated these projects; `claude` could start an agent with the user's
full permissions outside the jail. Remaining command rule: `command(/home/shreenipane/.local/bin/irm-test)`. The previous
project's `read_file`/`write_file` rules for ai-memory-reflection are untouched. Verification probe: `claude --version`
from agy (result recorded below when it returns).

---

## Phase 0 — scaffold

**Spec:** `.tasks/00-scaffold.md`. **Dispatch:** from `agy-ws`, no `--sandbox`; exit 0, `denied_actions` none.

| Check | Result |
|---|---|
| Files changed | `irm/__init__.py`, `irm/__main__.py`, `irm/cli.py`, `tests/conftest.py`, `tests/test_cli.py`: exactly the spec |
| Agent's `IRM-TEST` | `3 passed in 0.02s` (first task in which the agent ran its own tests) |
| Architect `irm-test -q` | `3 passed` |
| Outside the jail | `irm --version` → `irm 0.1.0`; no arguments → help, exit 2 |
| Ponytail | 4 + 5 + 15 lines; nothing unrequested |

**Accepted.**

---

## Phase 1 — monitor

**Specs:** `.tasks/01-monitor.md`, then fragment `.tasks/01b-monitor-simplify.md`. Both dispatches: `denied_actions`
none, only the listed files changed.

### 1.1 First delivery: correct but bloated
Agent: `15 passed`. Architect review of `irm/monitor.py` (555 lines): behaviour matched the spec, but the same
missing-file/`ENODEV`/vanished-directory handling was copied into six readers, `is_dir()` ran after every failed read,
and rate code was copied three times. `irm-test run bench overhead --seconds 60`: **0.97% of one core**, 135 leaf
cgroups, right at the 1% target.

Unrequested but kept: `monitor --duration` (needed by the SLO experiment and smoke runs), `--root/--proc` on the
benchmark. TRD §2 updated.

### 1.2 Simplification fragment
Tests untouched (`tests/test_monitor.py` mtime predates the fragment). Result: **284 lines**, `15 passed` (agent and
architect), benchmark **0.68% of one core** (0.043% of the host), 135 leaves. R10 verified.

### 1.3 Real run in the jail
`irm-test run monitor --interval 5` into `data/processed/smoke.db` (deleted afterwards): 136 rows per sweep (135 leaves +
host); host 0.68–0.73 cores and 6.2 GiB used, consistent with `top` (~3% busy of 16 CPUs) and `free`; first host row has
NULL rates as specified; busiest leaves are the terminal and Firefox scopes; `io_rbps` NULL for 97 leaves where the io
controller is not enabled (expected).

**Accepted.**

---

## Phase 2 — softirq attribution

### 2a Userspace (`.tasks/02a-attrib-python.md`)
Dispatch: `denied_actions` none; changed `irm/attrib.py` (186 lines), `irm/monitor.py` (+29), `irm/cli.py` (+3),
`tests/test_attrib.py`. Agent `23 passed`; architect `23 passed`.

Review of `attribute()`: per-CPU proportional split by packet deltas matches TRD §5.4; CPUs with NET_RX time but no packets
go to `unattrib`; any decreasing counter or vanished cgroup id returns `None` (loader restart; BPF hash entries are never
deleted, so a vanished id can only mean a restart); `Reader` re-baselines after a `None`. The structural length checks
are verbose (~40 lines) but validate stdin input, so they stay. Known costs, accepted: with attribution on, each sweep
walks the cgroup tree twice (`discover` + `cgroup_ids`); a dead loader shows up only as NULL attribution columns after
3 × interval. The mutation checks run at the phase gate after 2b.

**Accepted.**
