# Intelligent Resource Manager — Security

Two attack surfaces: **build time** (a coding agent writing and running code on the user's machine) and **run time**
(root eBPF, cgroup writes, a local web server, downloaded data).

## Contents
1. [Build time: coding-agent containment](#1-build-time-coding-agent-containment)
2. [Run time](#2-run-time)
3. [Standing checks](#3-standing-checks)
4. [Open items](#4-open-items)

## 1. Build time: coding-agent containment

Lesson carried over from ai-memory-reflection BUILD_LOG 0.12: agy's command allow-rules match text, not a filesystem
jail, so `command(uv)` gave the agent arbitrary file read and write. Here the agent **must** run tests (user
decision), so the containment is an OS jail the agent cannot modify.

| # | Threat | Control | Verified by |
|---|---|---|---|
| T1 | agy's file tools write outside the code directories (docs, `.tasks`, `pyproject.toml`, `.git`, other projects, `~`) | agy may write **anywhere inside its working directory** (BUILD_LOG D.6), so it is started from the empty `~/.local/share/irm/agy-ws`, never from the repo. The only repo `write_file` rules are `irm/`, `tests/`, `bpf/` (directory-matched: `irmx/` is refused). `read_file` covers the repository | Probes q6–q9 (R3) |
| T2 | agy's commands read secrets, reach the network, or write outside the repository | The **only** command rule is `command(/home/shreenipane/.local/bin/irm-test)`; bare names, backticks, `$(…)`, `;`, newlines, and redirects are refused. agy **does** accept environment prefixes (`LD_PRELOAD=…`, `PATH=…`), so the launcher is a static, libc-free binary that reads no environment and execs `~/.local/lib/irm/irm-test.sh` with `PATH=/usr/bin` only. That script runs everything in bubblewrap: read-only `/`; tmpfs over `/home`, `/root`, `/tmp`, `/var/tmp`, `/run`, `/mnt`, `/media` (hides SSH keys, tokens, D-Bus, Wayland, X11); read-write binds only `irm/`, `tests/`, `bpf/` (plus `data/processed`, `models/`, `reports/` in `run` mode); `--unshare-all` (no network, no host PIDs); `--clearenv`; `--new-session`; `--die-with-parent`; `--cap-drop ALL`; absolute binary paths; `bash -p`. agy's own `--sandbox` is not used: it hides `~/.local/bin` | Probes p2–p7, q1–q5 (R1, R2); `.tasks/test_jail_probe.py` (18 checks); hostile-environment test (D.6) |
| T3 | Jail-written files later run **outside** the jail without review: a poisoned virtualenv, git hooks, forged `__pycache__`, pickles | The virtualenv (`~/.local/share/irm/venv`) and git directory (`~/.local/share/irm/git`) live outside the repo; the jail sees the venv read-only. The `irm` wrapper sets `PYTHONPYCACHEPREFIX`, so jail-written bytecode is never loaded outside. Models load with `weights_only=True`, arrays with `allow_pickle=False`; nothing is pickled | Standing checks §3 |
| T4 | Agent-written code runs outside the jail (`data fetch`, `monitor`, `apply`, `experiment`) | Only after the architect's diff review and the standing checks | Every step's gate |
| T5 | Prompt injection through data | The agent never reads datasets or the database; trace fields are parsed as numbers | Spec wording |

## 2. Run time

| # | Threat | Control | Verified by |
|---|---|---|---|
| T6 | The root eBPF loader is abused or corrupts the kernel's view | ~200 lines of C, reviewed before its first `sudo` run; fixed arguments; opens no file for writing; programs attached via `bpf_link` so exit detaches them; verifier-checked BPF | Phase 2 gate; `bpftool cgroup show` after exit |
| T7 | The ingress program drops traffic | Every path returns 1; a standing check fails on any other return | Standing check; live network test |
| T8 | Root writes into a user-controlled path (symlink attack) | The loader writes only to stdout; the user's shell and `irm monitor` handle files | Code review |
| T9 | `apply` damages the host (starves a service, triggers OOM, touches system cgroups) | Dry run by default; allowlist = the user's delegated systemd subtree, checked on `realpath` strictly inside the prefix; file allowlist; value validation; `memory.high` clamped above current usage × 1.1; journal before every write; `revert` | Phase 9 tests (traversal, symlink, sibling prefix `user@1000.service-x`, invalid values) and live test |
| T10 | Dashboard reached from a web page (DNS rebinding, CSRF) or injected | `127.0.0.1` only; `Host` check → 421; GET only; no actions; CSP `default-src 'self'`; `textContent` only; metric whitelist before SQL; parameters for values; read-only DB | Phase 10 tests |
| T11 | Tampered or truncated dataset | HTTPS from GitHub Releases; byte count equals `Content-Length`; SHA-256 recorded on first download; CSV parsed as numbers | Phase 3 |
| T12 | The SLO experiment leaves load or limits behind, or sends traffic off-host | Loopback only; every scope stopped and the applied batch reverted in `finally` | Phase 11 live run |
| T13 | Secrets | The project needs none. The jail clears the environment. No `.env` files | — |

## 3. Standing checks

The architect runs these at every gate (`irm/`, `tests/`, `bpf/`):

```sh
irm-test .tasks/test_jail_probe.py -q                        # 18 passed: the jail still holds
grep -rnE 'pickle|joblib|allow_pickle=True|weights_only=False|\beval\(|\bexec\(|shell=True|os\.system' irm tests
grep -rnE 'urllib|http\.client|requests|socket' irm        # only data.py, dashboard.py, experiment.py
grep -rn  'subprocess' irm                                   # only experiment.py
grep -rnE 'innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(' irm/static
grep -nE  'return [^1]' bpf/attrib.bpf.c                     # reviewed: none inside the cgroup_skb program
grep -nE  'fopen|O_WRONLY|O_RDWR|O_CREAT' bpf/attrib.c       # none
git status --porcelain                                       # only files the spec lists
```

## 4. Open items

- None. **Closed 2026-09-17:** the legacy global agy rules for external commands and `command(cp)`, which predated these projects
  (external commands could start an agent with the user's full permissions outside any jail, bypassing T2), were removed at the
  user's request. The only command rule is now the `irm-test` launcher (BUILD_LOG D.8).
