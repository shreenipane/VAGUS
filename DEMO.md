# Faculty demo — Intelligent Resource Manager prototype

About 8 minutes. Everything below runs on this laptop. Open two terminals in the repository:
`cd ~/"Heavy Coding/Projects/intelligent-resource-manager"`

## Before you start (already done by the architect)
- `irm monitor` is running in the background (telemetry every 5 s into `data/irm.db`).
- `reports/forecast.json` and `reports/placement.json` were produced by `irm train forecast` and
  `irm evaluate placement`.

## 1. The problem (30 s, slides 2–4)
Over-provisioning wastes up to half of capacity, yet bursts and co-location blindness still break SLAs, because the OS
bills work to processes and schedulers ignore *when* workloads peak.

## 2. Monitor — kernel telemetry (1 min, slide 7)
Terminal 1: `irm dashboard` → open **http://127.0.0.1:8765**
- The Monitor card reads cgroups v2 and `/proc` passively: CPU, memory, throttling, and PSI (pressure stall) per
  **cgroup**, the resource principal, not per process.
- Point at the overhead line: the collector itself costs **under 1% of one core** (measured with `irm bench overhead`).
- Attribution: the eBPF design charges NET_RX softirq time to the cgroup that owns the traffic; the math is implemented and
  tested, and the kernel program is the next step (needs root and the BPF toolchain).

## 3. Contention, live (2 min, slides 9–10)
Terminal 2, start a latency-critical job and a noisy neighbour (together they want more than the 16 CPUs):
```sh
systemd-run --user --scope --unit=critical-demo bash -c 'for i in $(seq 6); do (while :; do :; done) & done; wait' &
systemd-run --user --scope --unit=noisy-demo    bash -c 'for i in $(seq 14); do (while :; do :; done) & done; wait' &
```
Wait ~60 s (watch both appear at the top of the Monitor card; click `noisy-demo.scope` to chart it).

**Analyse + Plan:**
```sh
C=/user.slice/user-1000.slice/user@1000.service/app.slice/critical-demo.scope
irm recommend --protect $C --min-samples 12 --hours 1
```
Say: the planner takes each cgroup's P95 demand, **reserves** capacity for the protected job, and squeezes everyone else
into what is left; the reason column explains each limit. The Plan card shows the same table.

**Execute (dry run, then for real).** ⚠️ Prototype bug: `recommend` produces limits for **every** busy cgroup in your
session (GNOME, Firefox, the terminal). Plain `irm apply --yes` would cap them all. On stage, apply only the demo scopes:
```sh
python3 -c "import json; p='data/recommendations.json'; r=json.load(open(p)); r['items']=[i for i in r['items'] if i['cgroup'].endswith(('/noisy-demo.scope','/critical-demo.scope'))]; json.dump(r, open('data/recommendations-demo.json','w'), indent=2)"
irm apply --from data/recommendations-demo.json          # prints old → new, writes nothing
irm apply --from data/recommendations-demo.json --yes    # journaled cgroup v2 writes: cpu.max, memory.high, cpu.weight
```
Watch the chart: `noisy-demo` drops to its quota and its throttled line rises; the critical job keeps its cores.
Safety: writes are limited to your own delegated systemd subtree, validated, clamped, and journaled.
```sh
irm revert           # restores the exact previous values
systemctl --user stop noisy-demo.scope critical-demo.scope
```

## 4. Analyse — predicting the tail (1.5 min, slide 8)
Analyse card → forecast table and example chart.
- The attention-LSTM encoder–decoder predicts the **95th percentile** of CPU for the next hour (pinball loss), not the
  mean. Compare coverage and pinball loss against last-window P95 and ARIMA.
- Prototype data: synthetic heavy-tailed bursty workloads. The pipeline for the real Azure 2019 trace (Resource
  Central's data, 2.6 M VMs) is designed and is the next phase.

## 5. Plan — co-location-aware consolidation (1.5 min, slide 9)
Analyse card → placement table.
- A simulated cluster (half the VMs peak at noon, half at midnight). The DQN scores every (VM, host) pair with 14
  features, including the **co-location coefficient K = (1 − ρ)/2**, and is rewarded for switching hosts off and
  penalised for overload and migrations.
- Compare energy, SLA overload, and migrations against First-Fit and Best-Fit.

## 6. What is next (30 s)
Real Azure trace; eBPF loader live; live SLO experiment (p99 latency with/without irm); one-command reproduction and
report. Engineering process: an architect agent specifies and gates; a coding agent writes and tests every module inside
a verified sandbox (see BUILD_LOG.md).

## If something fails
- Dashboard port busy: `irm dashboard --port 8800`.
- `recommend` skips a cgroup: wait another 30 s, or lower `--min-samples`.
- Stop the demo load at any time: `systemctl --user stop noisy-demo.scope critical-demo.scope`.
