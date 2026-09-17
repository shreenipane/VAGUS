# VAGUS — Product Requirements

Author & Lead Architect: Shree Nipane (@shreenipane). Source: `~/Downloads/Intelligent_Linux_Resource_Management_260917_011044.pdf`.

## 1. Problem

| Proposal claim (slide) | What it means for this project |
|---|---|
| Data centres over-provision by up to 50% for worst-case spikes (2) | Limits should follow observed/predicted **tail** demand, not static allocation |
| Bursts and co-location blindness still cause SLA violations (2, 4) | Placement must consider *when* workloads peak, not only how big they are |
| Kernel work (softirqs, protocol processing) is invisible to cgroups (3) | The resource principal is the cgroup; kernel work done for it must be measured via eBPF |
| ARIMA assumes stationary data; bursty cloud load is heavy-tailed (4) | Forecast P95 directly, with a sequence model, and compare against seasonal baselines |
| First-Fit / Best-Fit ignore temporal correlation between VMs (4) | A learned placer uses co-location coefficients (EVMC, Zhang et al. '25) |

## 2. Goal

A resource management platform for Linux servers that **recommends**, and on explicit approval **enforces**, cgroup v2
resource limits, together with a trace-driven cluster simulator that evaluates co-location-aware consolidation. Every
claim is backed by reproducible measurements.

## 3. Users

- **Operator** of a single Linux server (this Fedora 44 laptop is the test server).
- **Researcher / reviewer** checking whether the proposal's claims hold.

## 4. Deliverables (slide 12)

| # | Deliverable | Built as |
|---|---|---|
| 1 | Telemetry engineering, sub-1% observer overhead | `irm monitor` (cgroups v2 + `/proc` + PSI) and `bpf/attrib` (eBPF softirq attribution prototype) |
| 2 | ML model deployment: P95 tail demand forecasting | Sequence-to-sequence Attention-LSTM P95 forecaster on bursty workloads |
| 3 | DQN scheduler for co-location-aware consolidation | Double-DQN scheduler over a trace-driven simulated cluster |
| 4 | Kernel-level enforcement | eBPF measurement + cgroup v2 enforcement. No kernel patch (user decision, ARCHITECTURE D1) |
| + | End-to-end proof | Live SLO experiment with rotation arms (A, B, C, W, K) and raw latency telemetry |

## 5. Golden paths

**Operator**
1. `irm monitor` runs in the background. Optionally `sudo bpf/attrib | irm monitor --attrib -` adds softirq attribution.
2. `irm recommend --protect <cgroup>` prints limits and correlated-peak pairs with reasons.
3. `irm apply` shows the plan; `irm apply --yes` writes it; `irm revert` restores the previous values.
4. `irm dashboard` shows telemetry, attribution, forecasts, recommendations, and evaluation reports.

**Researcher**
1. `irm data fetch` (network, once) then `irm reproduce` rebuilds data, trains every model, runs every evaluation,
   and writes `reports/*.json`, `reports/figures/*.svg`, and `reports/RESULTS.md`.
2. `irm experiment slo` runs the live latency experiment and writes `reports/slo.json`.
3. `REPORT.md` presents method, results, and threats to validity.

## 6. Success metrics

Targets are measured and recorded as observed. A missed target is reported, never tuned away on the test data.

| Area | Metric | Target |
|---|---|---|
| Monitor | Collector CPU time ÷ wall time at 5 s interval | < 1% of one core |
| Attribution | Share of NET_RX softirq time charged to the receiving cgroup under a network-load test | ≥ 80% |
| Offline models | Accuracy, macro-F1 on a later time split | Above the majority-class baseline for both targets |
| Forecaster | Coverage of the q95 forecast on the held-out day | 0.93–0.97 |
| Forecaster | Pinball loss (τ = 0.95) | Lower than ARIMA and than last-window P95 |
| Planner | Energy (kWh), SLA overload fraction, migrations on the held-out day | DQN not Pareto-dominated by First-Fit or Best-Fit |
| Execute | Changes outside the allowlist; changes that cannot be reverted | 0; 0 |
| SLO experiment | p99 latency and SLO-violation rate of the protected service | Lower with `irm` than co-located without it |
| Reproducibility | `irm reproduce` from fetched raw data on this machine | Regenerates every report with the same seeds |

## 7. Non-goals

- Kernel patches, custom kernels, or a `sched_ext` scheduler (declined upgrade).
- Migrating real VMs across real hosts (simulated only).
- Multi-user access, authentication, or a remote dashboard (localhost only, read-only).
- cgroup v1, non-Linux systems, packaging as a system service.
- PSI-driven automatic rollback, conformal calibration, Pareto sweeps, extra baselines (declined upgrades).
- GPU training as a requirement.

## 8. Constraints

Fedora 44, kernel 7.1.13, 16 CPUs, 15 GiB RAM (a previous project's runs were killed by memory pressure), RTX 4050
(unused by default), Python 3.14.7, uv 0.12.3. `sudo` needs a password, so privileged steps are run by a human. The
coding agent has no network.
