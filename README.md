# Intelligent Resource Manager (`irm`)

An autonomic **Monitor → Analyse → Plan → Execute** loop for Linux servers, built from the proposal
*"Intelligent Resource Recommendation System for Linux Server"* (13 slides, 2026).

- **Monitor**: passive telemetry from cgroups v2 and `/proc`, plus an eBPF program that charges network softirq CPU
  time to the cgroup that owns the traffic, not to the task that happened to be interrupted.
- **Analyse**: offline gradient-boosted trees predict each VM's P95-CPU and lifetime bucket at creation (Resource
  Central style); an attention LSTM forecasts the P95 of CPU demand for the next hour.
- **Plan**: a Deep Q-Network places and consolidates VMs in a simulated cluster driven by the Azure 2019 VM trace,
  using temporal co-location coefficients; on the live host, a recommender sizes `cpu.max` / `memory.high`.
- **Execute**: validated, journaled, revertible cgroup v2 writes (dry run by default).
- **Proof**: a live SLO experiment on this machine measures p99 latency of a latency-sensitive service next to noisy
  neighbours, with and without `irm`. `irm reproduce` regenerates every result; `REPORT.md` presents them.

Status: see [BUILD_LOG.md](BUILD_LOG.md).

## Documents

| File | What it holds |
|---|---|
| [HOWTO.md](HOWTO.md) | **How to install and run everything** |
| [DEMO.md](DEMO.md) | Step-by-step faculty demo with talking points |
| [PRD.md](PRD.md) | Problem, goals, scope, success metrics |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, design decisions, deviations from the proposal |
| [TRD.md](TRD.md) | Exact technical specification every task refers to |
| [SECURITY.md](SECURITY.md) | Threat model: coding-agent containment and the privileged runtime |
| [PHASES.md](PHASES.md) | Build sequence, acceptance criteria, gates |
| [AGENTS.md](AGENTS.md) | Standing rules for the coding agent |
| [BUILD_LOG.md](BUILD_LOG.md) | What was specified, built, found wrong, and proven |

## Quick start (after Phase 1)

```sh
irm monitor &                 # telemetry into data/irm.db
irm recommend                 # after 30 min (empirical) or 4 h (forecast)
irm apply                     # dry run: shows old → new
irm apply --yes               # writes, journaled
irm revert                    # undoes the last batch
irm dashboard                 # http://127.0.0.1:8765
```

`irm` is a wrapper in `~/.local/bin` that runs the project's virtual environment (kept outside the repository; see
SECURITY.md).
