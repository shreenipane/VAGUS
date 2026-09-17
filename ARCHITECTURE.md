# VAGUS — System Architecture

## Contents
1. [Overview](#1-overview)
2. [Components](#2-components)
3. [Data Flow and Artifacts](#3-data-flow-and-artifacts)
4. [Design Decisions](#4-design-decisions)
5. [Deviations from the Proposal](#5-deviations-from-the-proposal)
6. [Known Limitations & Kernel Semantics](#6-known-limitations--kernel-semantics)
7. [Prior Art & References](#7-prior-art--references)

---

## 1. Overview

VAGUS separates live host monitoring and actuation from offline deep-learning research:

```
                          LIVE HOST PLANE (Operator-Run)
  kernel ─────────────────────────────────────────────────────────────────────────────
   cgroupfs  /proc  PSI            softirq tracepoints + cgroup_skb/ingress (eBPF)
      │                                       │  raw per-CPU counters, JSON lines
      ▼                                       ▼
  MONITOR  irm/monitor.py ◄──── stdin ──── bpf/attrib (root)       irm/attrib.py
      │  telemetry samples every 5 s
      ▼
  KNOWLEDGE  data/irm.db  (samples, journal)                 ───► DASHBOARD irm/dashboard.py
      │                                                               (127.0.0.1, read-only)
      ▼
  PLAN     irm/recommend.py (empirical P95, headroom & background budget)
      ▼
  EXECUTE  irm/execute.py → cpu.max / memory.high / cpu.weight (journaled, revertible)

                          OFFLINE RESEARCH PLANE (Trace Simulation)
  Synthetic Diurnal Trace (with burst injections)
      ├─► irm/forecast.py   Attention-LSTM vs seasonal-naive and ARIMA → reports/forecast.json
      └─► irm/sim.py + irm/dqn.py   Cluster simulator; First-Fit, Best-Fit, DQN → reports/placement_study.json

  BENCHMARK & VALIDATION
      └─► irm/experiment.py  Live SLO rotation experiment (Conditions A, B, C, W, K) → reports/slo.json
```

---

## 2. Components

| Stage | Module | Implementation Responsibility |
|---|---|---|
| **Monitor** | `irm/monitor.py` | Traverses leaf cgroups v2 (`cpu.stat`, `memory.current`, `io.stat`) and `/proc/stat`; records 5s interval telemetry into SQLite; manages table retention. |
| **Attribution** | `bpf/attrib.bpf.c`, `bpf/attrib.c`, `irm/attrib.py` | Ingress eBPF filter (`cgroup_skb`) tracking packet counts by cgroup ID; userspace math proportionally attributes `NET_RX` softirq CPU time. |
| **Host Plan** | `irm/recommend.py` | Sizes cgroup limits from empirical P95 demand; computes reserved headroom for protected services; subtracts background workload usage; enforces `--min-cores`. |
| **Host Execute** | `irm/execute.py` | Validates target cgroups within delegated user systemd subtrees; clamps memory above live usage; writes transaction journal; handles rollback (`revert`). |
| **Dashboard** | `irm/dashboard.py`, `irm/static/` | Stdlib `http.server` bound to `127.0.0.1`; read-only metrics visualization with strict CSP and `Host` header validation. |
| **Forecasting** | `irm/forecast.py` | Encoder–decoder Attention-LSTM estimating future P95 demand across multi-step windows; evaluated against seasonal-naive and ARIMA baselines. |
| **Consolidation** | `irm/sim.py`, `irm/dqn.py` | Trace-driven cluster simulator (arrivals, departures, evacuation) and Double-DQN scheduler minimizing SLA overload and energy tradeoffs. |
| **Experiment** | `irm/experiment.py` | Open-loop HTTP load generation and latency measurement across rotation arms (A, B, C, W, K) with automatic cleanup. |
| **CLI** | `irm/cli.py` | Unified command-line interface entry point for both `irm` and `vagus`. |

---

## 3. Data Flow and Artifacts

| Path | Written by | Read by | In Git | Description |
|---|---|---|:---:|---|
| `data/irm.db` | `monitor`, `execute` | `recommend`, `dashboard`, `execute` | No | Telemetry database (`samples` table) and transaction journal (`journal` table). |
| `data/demo-plan.json` | `recommend` | `apply`, `dashboard` | No | Generated recommendation plans with target quotas and reasoning. |
| `models/forecast.pt` | `irm train forecast` | Evaluation | No | Trained PyTorch weights for Attention-LSTM forecaster. |
| `models/dqn.pt` | `irm evaluate placement` | Simulation | No | Trained PyTorch weights for Double-DQN scheduler policy. |
| `reports/*.json` | Evaluations, experiment | Dashboard, documentation | **Yes** | Benchmark outputs (`slo.json`, `forecast.json`, `placement_study.json`, `overhead.json`). |

---

## 4. Design Decisions

| # | Decision | Technical Rationale |
|---|---|---|
| **D1** | eBPF measurement + cgroup v2 enforcement; no in-tree kernel patches | User decision. Developing custom kernel patches presents high maintenance risks and instability. eBPF provides non-intrusive observability on stock kernels. |
| **D2** | Split planes: single-host empirical limits, cluster trace simulation | A single development workstation cannot physically consolidate a cluster of physical machines. Simulation evaluates multi-tenant placement policies at scale. |
| **D3** | Leaf cgroup as resource principal (no per-PID metrics) | Process boundaries fail in containerized/systemd environments. Leaf cgroups map directly to services, scopes, and tenants. |
| **D4** | Normalized wide telemetry rows; 48h retention | Telemetry samples record wide rows per cgroup per 5 s, preventing excessive table explosion while supporting fast range queries. |
| **D5** | Userspace per-CPU proportional softirq attribution | Keeps the eBPF kernel program minimal and non-blocking, while userspace math remains unit-testable without requiring root. |
| **D6** | Ingress filter attached via `bpf_link` returning 1 | On process termination, the kernel automatically detaches the BPF program; returning 1 ensures traffic is never dropped. |
| **D7** | Recommender derives live caps from empirical P95 | Live actuation uses verified historical P95 demand; deep sequence models are validated in the offline analytics plane. |
| **D8** | Protected cgroups receive `cpu.weight=1000` without erasing user caps | CFS weight prioritization guarantees CPU scheduling precedence under contention without discarding existing operator limits. |
| **D9** | Defensive execution: dry run by default, journaled, delegated subtree only | Operations that alter kernel cgroup settings require operator consent, allowlist confinement, and reversible rollback journals. |
| **D10** | Localhost dashboard with zero remote attack surface | Binds strictly to `127.0.0.1`, enforces `Host` header matching, serves GET-only endpoints, and exposes no remote execution interfaces. |

---

## 5. Deviations from the Proposal

| Proposal Claim | Implemented Reality | Design Rationale |
|---|---|---|
| In-kernel accounting patch bridging Resource Containers and cgroups v2 | Ingress eBPF filter + userspace attribution; cgroups v2 enforcement | Kernel patches carry high maintenance overhead and risk machine instability. |
| Continuous autonomous re-planning daemon | Operator-initiated `Monitor → Plan → Execute` pipeline with dry run | Production systems favor explicit operator review over autonomous actuator loops. |
| Real VM migration on physical hosts | Simulated cluster consolidation driven by multi-tenant traces | Cluster-scale physical VM migration requires multi-server infrastructure. |
| Hardware-level isolation wall | Cgroup weights (`cpu.weight=1000`) and quota clamps (`cpu.max`) | `cpuset` pinning is not delegated within unprivileged systemd user sessions. |

---

## 6. Known Limitations & Kernel Semantics

1. **Kernel SoftIRQ Invisibility**:
   - On Linux with `CONFIG_IRQ_TIME_ACCOUNTING=y`, softirq time is booked under `CPUTIME_SOFTIRQ`. Consequently, softirq time is **invisible** to cgroups rather than misattributed to whichever thread was interrupted.
   - eBPF packet attribution quantifies this invisible overhead, but stock kernels cannot throttle softirq execution via `cpu.max`.
2. **Attribution Coverage**:
   - Current eBPF probes target `NET_RX` softirqs on ingress. Vectors such as `TIMER`, `RCU`, and `BLOCK` remain unallocated.
3. **Approximation of Packet Work**:
   - The attribution model weights packets equally, approximating processing cost by packet count rather than byte length.

---

## 7. Prior Art & References

- **Iron (Khalid et al., NSDI 2018)**: *Iron: Mitigating the Performance Impact of Software Interrupts in Container Environments*. Per-container softirq CPU accounting enforced against cgroup quotas.
- **EVMC (Zhang et al., Electronics 2025)**: *Energy-Efficient Virtual Machine Consolidation in Cloud Data Centers*, doi:10.3390/electronics14193813. Co-location correlation coefficient $K = (1 - \rho)/2$.
- **Resource Central (Cortez et al., SOSP 2017)**: *Resource Central: Understanding and Predicting Workloads for Improved Resource Management in Large Cloud Platforms*.
- **Resource Containers (Banga, Druschel, Mogul, OSDI 1999)**: *Resource Containers: A New Facility for Resource Management in Operating Systems*.
