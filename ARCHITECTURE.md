# Intelligent Resource Manager — Architecture

## Contents
1. [Overview](#1-overview)
2. [Components](#2-components)
3. [Data flow and files](#3-data-flow-and-files)
4. [Design decisions](#4-design-decisions)
5. [Deviations from the proposal](#5-deviations-from-the-proposal)
6. [Known limitations](#6-known-limitations)

## 1. Overview

The system has two planes that share one forecaster and one co-location function.

```
                          LIVE HOST PLANE (this server)
  kernel ─────────────────────────────────────────────────────────────────────────────
   cgroupfs  /proc  PSI            softirq tracepoints + cgroup_skb/ingress (eBPF)
      │                                       │  raw per-CPU counters, JSON lines
      ▼                                       ▼
  MONITOR  irm/monitor.py ◄──── stdin ──── bpf/attrib (root)       irm/attrib.py
      │  wide rows every 5 s
      ▼
  KNOWLEDGE  data/irm.db  (samples, journal)                 ───► DASHBOARD irm/dashboard.py
      │                                                               (127.0.0.1, read-only)
      ▼
  ANALYSE  irm/forecast.py (attention LSTM, q95)  ◄── models/forecast.pt
      ▼
  PLAN     irm/recommend.py → data/recommendations.json
      ▼
  EXECUTE  irm/execute.py → cpu.max / memory.high / cpu.weight (journaled, revertible)

                          RESEARCH PLANE (Azure 2019 VM trace)
  irm/data.py  fetch → data/raw ; prepare → data/processed/series.npz
      ├─► irm/lifetime.py   GBDT: P95 bucket + lifetime bucket at creation → priors.npz, reports/lifetime.json
      ├─► irm/forecast.py   train LSTM; evaluate vs ARIMA, last-window P95 → reports/forecast.json
      └─► irm/sim.py + irm/dqn.py   cluster simulator; First-Fit, Best-Fit, DQN → reports/placement.json

  PROOF  irm/experiment.py  live SLO experiment → reports/slo.json
         irm/reproduce.py   runs the research plane end to end → reports/RESULTS.md, reports/figures/
```

## 2. Components

| MAPE-K stage (slide) | Module | Responsibility |
|---|---|---|
| Monitor (7) | `irm/monitor.py` | Reads leaf cgroups and `/proc`; computes interval gauges; writes `samples`; enforces retention |
| Monitor (3, 10) | `bpf/attrib.bpf.c`, `bpf/attrib.c`, `irm/attrib.py` | Measures softirq time per CPU and vector; counts packets delivered to each cgroup's sockets; userspace splits NET_RX time by packet share |
| Analyse (8) | `irm/lifetime.py` | Offline models from creation-time features, leakage-safe subscription history |
| Analyse (8) | `irm/forecast.py` | Attention LSTM encoder–decoder forecasting per-step q95 of CPU; baselines; evaluation |
| Plan (9) | `irm/sim.py` | Cluster simulator: arrivals, departures, hourly evacuation, linear power model, overload accounting, co-location coefficient K, First-Fit/Best-Fit |
| Plan (9) | `irm/dqn.py` | Double DQN with one per-(VM, host) scoring network |
| Plan (live) | `irm/recommend.py` | Limits from forecasts, capacity reservation for protected cgroups, correlated-peak pairs |
| Execute (10) | `irm/execute.py` | Validation, allowlist, clamps, journal, apply, revert |
| — | `irm/dashboard.py`, `irm/static/` | Local read-only dashboard |
| — | `irm/experiment.py` | Live SLO experiment |
| — | `irm/reproduce.py` | Research pipeline, figures, `reports/RESULTS.md` |
| — | `irm/cli.py` | `argparse` entry point for all subcommands |

## 3. Data flow and files

| Path | Written by | Read by | In git |
|---|---|---|---|
| `data/irm.db` (`samples`, `journal`) | monitor, execute | recommend, dashboard, execute | no |
| `data/raw/` (trace downloads, `SHA256SUMS`) | `irm data fetch` | `irm data prepare`, lifetime | no |
| `data/processed/*.npz` | prepare, lifetime, forecast | forecast, sim, dqn | no |
| `data/recommendations.json` | recommend | apply, dashboard | no |
| `models/forecast.pt`, `models/dqn.pt` | training | recommend, sim | no |
| `reports/*.json`, `reports/figures/*.svg`, `reports/RESULTS.md` | evaluations, reproduce, experiment | dashboard, REPORT.md | **yes** |

## 4. Design decisions

| # | Decision | Reason |
|---|---|---|
| D1 | eBPF measurement + cgroup v2 enforcement; no kernel patch | User decision. A patched kernel is months of work and can leave the machine unbootable |
| D2 | Two planes: live single host, simulated cluster | One laptop has no fleet of hosts or VMs to consolidate. The Azure 2019 trace is the Resource Central paper's own data |
| D3 | The resource principal is a **leaf cgroup**; no per-PID metrics | The proposal's point is that the process is the wrong principal. Leaf cgroups are services and scopes |
| D4 | One wide `samples` row per (cgroup, ts); 48 h retention | Long format at 5 s × ~180 cgroups × 12 metrics is ~40 M rows per day |
| D5 | NET_RX softirq time is split **per CPU by packet share** in userspace, from raw BPF counters | Keeps the BPF program tiny and makes the attribution math unit-testable without root. Per-CPU splitting is correct when different cgroups' traffic lands on differently loaded CPUs |
| D6 | eBPF programs are attached through `bpf_link`; the ingress program always returns 1 | Loader exit detaches everything; a bug can never drop packets |
| D7 | Loader writes to stdout, piped into `irm monitor --attrib -` | Root never writes into a user-controlled directory (no symlink attack), and no unbounded log file |
| D8 | scikit-learn `HistGradientBoostingClassifier` instead of XGBoost | Same model family, one dependency fewer. Models are never pickled to disk; predictions are saved as `.npz` |
| D9 | The forecaster predicts per-step q95 with pinball loss; the decoder is not autoregressive | "We discard mean predictions" (slide 8). No teacher forcing, no exposure bias |
| D10 | Time-ordered splits everywhere: GBDT trains on VMs created before the simulation window; LSTM and DQN train on the window's first two days and are tested on the last | No label or future leakage into the held-out day |
| D11 | One MDP decision = "place one VM" (arrival or evacuation); one network scores each (VM, host) pair | Works for any host count. Baselines use the same triggers, so only the host choice differs between policies |
| D12 | VM forecasts are precomputed per VM per hour | VM demand does not depend on placement in the simulator, so forecasting inside the loop would be wasted work |
| D13 | Reward = realized overload on the chosen host over the next hour + power-on/power-off + migration terms | Ground truth from the environment, delivered with a one-hour delay; the agent never observes the future |
| D14 | Live CPU limits come from q95 forecasts; memory limits from the observed maximum | CPU is elastic (throttling is recoverable); memory is not (reclaim below the working set hurts) |
| D15 | Protected cgroups get `cpu.weight` 1000 and reserved capacity; others share the rest | This is the lever that protects latency under contention (Heracles-style reservation) |
| D16 | Execute is dry-run by default, journaled, limited to the user's delegated systemd subtree | Writes that can hurt the host must be explicit and undoable |
| D17 | Dashboard: stdlib `http.server`, `127.0.0.1`, GET only, no apply button | No web framework; a privileged action never sits behind a browser request |
| D18 | CPU-only PyTorch wheels | Models are small (tens of thousands of parameters). Switch to CUDA wheels if training exceeds ~30 min |
| D19 | Figures use matplotlib, written as SVG | Axis and tick layout is not worth hand-rolling |
| D20 | Coding-agent containment: agy writes only `irm/`, `tests/`, `bpf/`; git directory and virtualenv live outside the repo; agy runs tests only through a bubblewrap jail | See SECURITY.md T1–T4 |

## 5. Deviations from the proposal

| Proposal | Built | Why |
|---|---|---|
| Kernel modification bridging Resource Containers with cgroups v2 (slides 10, 12) | eBPF measures kernel work per cgroup; cgroup v2 files enforce limits | D1. Softirq CPU is **measured and counted as the cgroup's demand**, but the stock kernel does not bill it to `cpu.stat`, and `cpu.max` does not throttle it |
| VM migration and placement on real hosts (9) | Simulated cluster on a real trace | D2 |
| XGBoost (8) | scikit-learn histogram GBDT | D8 |
| "Near-zero impact on application throughput" (7) | Collector CPU time is measured; application throughput impact is measured only in the SLO experiment | A measurable definition |
| Hardware-level isolation wall (10) | `cpu.weight` + capacity reservation; no `cpuset` | `cpuset` is not delegated to the user's systemd subtree |
| "Handles mixed batch workloads perfectly" (8) | Coverage and pinball loss reported against baselines | Claims are measured, not asserted |

## 6. Known limitations

- On this kernel `CONFIG_IRQ_TIME_ACCOUNTING=y`: softirq time is charged to **no** cgroup when it runs in interrupt
  context, and to whichever task was running when it runs in task context (`ksoftirqd`, `local_bh_enable`). "Blamed"
  in the dashboard means the cgroup of the task running at softirq entry.
- Only NET_RX is attributed. Other vectors (TIMER, RCU, BLOCK, …) are reported as unattributed totals.
- Packet share approximates cost share: a large packet and a small one count the same.
- The live forecaster is trained on 5-minute Azure VM utilization and applied to local cgroups as a fraction of host
  CPUs. That is a distribution shift; the forecast coverage on local data is reported separately.
- The simulator models neither interference beyond CPU overload nor migration duration.
