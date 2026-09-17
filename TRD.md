# Intelligent Resource Manager — Technical Requirements

Every task spec names the sections it implements. "Must" is a requirement; anything not written here is not built.

## Contents
1. [Stack and layout](#1-stack-and-layout)
2. [CLI contract](#2-cli-contract)
3. [Paths](#3-paths)
4. [Monitor](#4-monitor)
5. [eBPF softirq attribution](#5-ebpf-softirq-attribution)
6. [Datasets](#6-datasets)
7. [Offline models: P95 and lifetime buckets](#7-offline-models-p95-and-lifetime-buckets)
8. [Forecaster](#8-forecaster)
9. [Cluster simulator](#9-cluster-simulator)
10. [DQN](#10-dqn)
11. [Live recommender](#11-live-recommender)
12. [Execute](#12-execute)
13. [Dashboard](#13-dashboard)
14. [SLO experiment](#14-slo-experiment)
15. [Reproduce](#15-reproduce)
16. [Tests](#16-tests)
17. [Unverified claims](#17-unverified-claims)

## 1. Stack and layout

Python 3.14 (system `/usr/bin/python3.14`), uv. Runtime dependencies: `numpy`, `pandas`, `scikit-learn`,
`statsmodels`, `torch` (CPU wheels from `https://download.pytorch.org/whl/cpu`), `matplotlib`. Dev: `pytest`.
C: clang, bpftool, libbpf (Phase 2). **No other dependency** without a TRD change.

```
pyproject.toml, uv.lock            architect-owned
irm/  __init__.py __main__.py cli.py monitor.py attrib.py data.py lifetime.py forecast.py
      sim.py dqn.py recommend.py execute.py dashboard.py experiment.py reproduce.py
      static/ index.html app.js style.css
bpf/  attrib.bpf.c attrib.c Makefile
tests/ test_<module>.py conftest.py
data/ models/                      generated, gitignored
reports/                           generated, committed
```

## 2. CLI contract

`irm.cli:main` uses `argparse` subcommands. Exit 0 on success, 1 on a runtime failure, 2 on bad arguments.

| Command | Options (default) |
|---|---|
| `irm monitor` | `--interval 5` `--db` `--root /sys/fs/cgroup` `--proc /proc` `--attrib` (unset, or `-` for stdin) `--retention-hours 48` `--duration` (unset = forever) |
| `irm bench overhead` | `--seconds 60` `--interval 5` `--root` `--proc` |
| `irm data fetch` | `--files 20` `--last-file 195` `--sample-per-mille 20` |
| `irm data prepare` | `--max-vms 5000` `--seed 0` |
| `irm train lifetime` | — |
| `irm train forecast` | `--epochs 20` `--seed 0` |
| `irm train dqn` | `--episodes 60` `--seed 0` `--w-sla 10` `--w-energy 1` `--w-mig 0.1` |
| `irm evaluate forecast` | `--source azure` (or `local`) `--arima-windows 500` |
| `irm evaluate placement` | — |
| `irm recommend` | `--db` `--protect CGROUP…` `--only CGROUP…` `--min-cores 0.5` `--headroom 1.25` `--min-samples 360` `--hours 24` `--out` |
| `irm apply` | `--from` `--yes` `--allow PREFIX…` `--root /sys/fs/cgroup` |
| `irm revert` | `--batch N` (last applied batch) `--root /sys/fs/cgroup` |
| `irm dashboard` | `--port 8765` |
| `irm experiment slo` | `--minutes 5` `--reps 3` `--rate 200` |
| `irm reproduce` | `--synthetic` |

## 3. Paths

`HOME = Path(irm.__file__).resolve().parent.parent` (the repository). Defaults: `data/irm.db`, `data/raw/`,
`data/processed/`, `data/recommendations.json`, `models/`, `reports/`, `reports/figures/`, all under `HOME`.
Directories are created on first write. SQLite read-only opens use `Path.as_uri() + "?mode=ro"` with `uri=True`
(the repository path contains a space).

## 4. Monitor

### 4.1 Discovery (every sweep)
Walk `--root` with `os.walk`. A cgroup is recorded when it is a **leaf** (no subdirectories) and its `cgroup.events`
contains `populated 1`. Its name is the path relative to root with a leading `/`. The host row uses the name `host`.

### 4.2 Reads
A missing file, `ENOENT`, or `ENODEV` makes that metric `NULL`. A cgroup that disappears mid-sweep is skipped.

| File | Fields |
|---|---|
| `cpu.stat` | `usage_usec`, `nr_periods`, `nr_throttled` |
| `memory.current` | integer |
| `io.stat` | sum of `rbytes=` and `wbytes=` over all device lines |
| `cpu.pressure`, `memory.pressure`, `io.pressure` | `some` line, `avg10` |
| `pids.current` | integer |
| `/proc/stat` (host) | first `cpu` line: user nice system idle iowait irq softirq steal, in `os.sysconf("SC_CLK_TCK")` ticks |
| `/proc/meminfo` (host) | `MemTotal`, `MemAvailable` (kB) |
| `/proc/pressure/{cpu,memory,io}` (host) | `some avg10` |

### 4.3 Gauges
`Δt` comes from `time.monotonic()`; `ts` is `int(time.time())`. Counters are differenced against the previous sweep.
If there is no previous value, or a counter decreased, the rate is `NULL` for that sweep.

| Column | Leaf cgroup | Host |
|---|---|---|
| `cpu_cores` | Δusage_usec / 1e6 / Δt | Δ(user+nice+system+irq+softirq+steal) / tck / Δt |
| `throttled_ratio` | Δnr_throttled / Δnr_periods, or 0.0 when Δnr_periods = 0 | NULL |
| `mem_bytes` | memory.current | (MemTotal − MemAvailable) × 1024 |
| `io_rbps`, `io_wbps` | Δbytes / Δt | NULL |
| `cpu_psi`, `mem_psi`, `io_psi` | avg10 | avg10 |
| `pids` | pids.current | NULL |
| `softirq_cores` | NULL | Δsoftirq / tck / Δt |
| `netrx_attrib_cores`, `netrx_blamed_cores` | §5.4 | blamed on the root cgroup (kernel threads) |
| `netrx_unattrib_cores` | NULL | §5.4 |

### 4.4 Storage
```sql
create table if not exists samples (
  ts integer not null, cgroup text not null,
  cpu_cores real, throttled_ratio real, mem_bytes real, io_rbps real, io_wbps real,
  cpu_psi real, mem_psi real, io_psi real, pids real, softirq_cores real,
  netrx_attrib_cores real, netrx_blamed_cores real, netrx_unattrib_cores real,
  primary key (cgroup, ts)) without rowid;
create index if not exists samples_ts on samples(ts);
```
`pragma journal_mode=wal`. One transaction per sweep, `insert or replace`. Once per hour, delete rows older than
`--retention-hours`. Sweeps are scheduled on the monotonic clock (`next += interval`), so they don't drift. Readers take
`root` and `proc` paths and the clocks as arguments, so tests can use fixture trees.

### 4.5 Overhead benchmark
`irm bench overhead` runs the monitor loop into a temporary database for `--seconds`, measuring `time.process_time()`.
Writes `reports/overhead.json`: `interval`, `seconds`, `cgroups` (mean leaf count), `cpu_seconds`,
`pct_of_one_core = 100 × cpu_seconds / wall`, `pct_of_host = pct_of_one_core / os.cpu_count()`.

## 5. eBPF softirq attribution

### 5.1 Build
`bpf/Makefile`: `vmlinux.h` from `bpftool btf dump file /sys/kernel/btf/vmlinux format c`;
`clang -g -O2 -target bpf -D__TARGET_ARCH_x86 -c attrib.bpf.c -o attrib.bpf.o`;
`bpftool gen skeleton attrib.bpf.o > attrib.skel.h`; `cc -O2 -Wall -Wextra attrib.c -lbpf -lelf -lz -o attrib`;
`clean`. Generated files are gitignored. Host packages (human): `clang llvm bpftool libbpf-devel elfutils-libelf-devel zlib-devel`.

### 5.2 BPF program (`attrib.bpf.c`, license `GPL`)
| Map | Type | Key → value | Max |
|---|---|---|---|
| `entry` | PERCPU_ARRAY | 0 → `{u64 ts; u64 cgid; u32 vec; u32 pad}` | 1 |
| `vec_ns` | PERCPU_ARRAY | vector → u64 ns | 10 |
| `in_netrx` | PERCPU_ARRAY | 0 → u32 flag | 1 |
| `rx_pkts` | PERCPU_HASH | u64 cgid → u64 packets | 4096 |
| `blamed_ns` | PERCPU_HASH | `{u64 cgid; u32 vec; u32 pad}` → u64 ns | 16384 |

- `tp_btf/softirq_entry` (`BPF_PROG`, `unsigned int vec_nr`): store `bpf_ktime_get_ns()`,
  `bpf_get_current_cgroup_id()`, and the vector in `entry`; if `vec_nr == NET_RX_SOFTIRQ`, set `in_netrx = 1`.
- `tp_btf/softirq_exit`: if `entry.ts == 0`, return (the loader started mid-softirq). Add `now − ts` to `vec_ns[vec]` and
  to `blamed_ns[{cgid, vec}]` (insert with `BPF_NOEXIST`, then look up and add). Clear `in_netrx` for NET_RX; set
  `entry.ts = 0`.
- `cgroup_skb/ingress`: if `in_netrx` is set on this CPU and `bpf_skb_cgroup_id(skb)` is non-zero, add 1 to
  `rx_pkts[cgid]`. **Every path returns 1.** The program never drops a packet.

### 5.3 Loader (`attrib.c`)
`attrib [--interval N]` (1–60, default 5); anything else prints usage and exits 2. Open and load the skeleton; attach
the tracepoints; attach `cgroup_skb/ingress` with `bpf_program__attach_cgroup` on an `O_RDONLY|O_DIRECTORY` fd of
`/sys/fs/cgroup` (a `bpf_link`, so it detaches when the process exits). Every interval, print **one** line to stdout
and `fflush`. SIGINT/SIGTERM → destroy the skeleton, exit 0. Errors → stderr, exit 1. **The loader opens no file for
writing.**

```json
{"ts": 1758100000.123, "ncpu": 16,
 "vec_ns": [[<ncpu u64>], … 10 vectors],
 "rx_pkts": {"<cgid>": [<ncpu u64>]},
 "blamed_ns": {"<cgid>": [<10 u64, summed over CPUs>]}}
```
Values are cumulative since load. `ncpu` = `libbpf_num_possible_cpus()`. `ts` is CLOCK_REALTIME.

### 5.4 Userspace (`irm/attrib.py`)
- `attribute(prev, cur)`: returns `None` if `cur.ts <= prev.ts`, `ncpu` differs, or any cumulative value decreased (loader
  restart). Otherwise, with `Δt = cur.ts − prev.ts` and `R_c = Δvec_ns[3][c]`:
  for each CPU `c`, `P_c = Σ_g Δrx_pkts[g][c]` (a cgid absent from `prev` counts 0). If `P_c > 0`, add
  `R_c · Δrx_pkts[g][c] / P_c` to `attrib[g]`; otherwise add `R_c` to `unattrib`.
  Returns `{"dt", "attrib_cores": {cgid: ns/1e9/Δt}, "blamed_cores": {cgid: Δblamed_ns[g][3]/1e9/Δt},
  "unattrib_cores", "vec_cores": [10 values]}`.
- `cgroup_ids(root)`: `{os.stat(dir).st_ino: name}` for every cgroup directory (R5).
- `Reader(stream)`: daemon thread; for each line: invalid JSON is skipped and counted; `attribute(prev, cur)` is
  published with its monotonic arrival time. `latest(max_age)` returns the newest result or `None` if older than
  `max_age` (3 × interval).
- In the monitor with `--attrib -`: while a fresh result exists, leaves get `netrx_attrib_cores` / `netrx_blamed_cores`
  (0.0 when absent from the result); the host row gets `netrx_unattrib_cores` and the root cgroup's blamed value. With no
  fresh result, these columns are `NULL`.

## 6. Datasets

### 6.1 Source (verified 2026-09-17)
Base `https://github.com/Azure/AzurePublicDataset/releases/download/dataset-v2/`.
`trace_data_vmtable_vmtable.csv.gz` (437,594,496 bytes);
`trace_data_vm_cpu_readings_vm_cpu_readings-file-{i}-of-195.csv.gz` (file 1: 856,259,637 bytes). No header rows (R7).
vmtable columns: vmid, subscription, deployment, created, deleted, maxcpu, avgcpu, p95maxcpu, category,
cores_bucket, memory_bucket. Readings columns: timestamp, vmid, mincpu, maxcpu, avgcpu.

### 6.2 `irm data fetch`
- vmtable → `data/raw/vmtable.csv.gz`: stream to `.part`, rename only when the byte count equals `Content-Length`;
  append `<sha256>  <name>` to `data/raw/SHA256SUMS`. Skip if the final file exists.
- Readings files `i` from `last_file − files + 1` to `last_file`: stream-decompress (`gzip.GzipFile(fileobj=response)`),
  keep rows where `zlib.crc32(vmid.encode()) % 1000 < sample_per_mille`, write to
  `data/raw/readings/{i:03d}.csv.gz.part`, rename when complete. Skip if the final file exists. Print
  `file i: rows, kept, ts_min, ts_max`.
- `urllib.request` with a 60 s timeout. Errors propagate (exit 1).

### 6.3 `irm data prepare` → `data/processed/series.npz`
- Grid: `t_start` = min timestamp, `t_end` = max, step 300, `T = (t_end − t_start)/300 + 1`.
- VMs with ≥ 12 readings; if more than `--max-vms`, choose randomly with `--seed`.
- `cpu_min`, `cpu_avg`, `cpu_max` `[V, T]` float32 percent, NaN where unobserved. Forward-fill gaps of ≤ 2 steps
  inside a VM's first-to-last reading span.
- Buckets: an integer string → that value; `>N` → `2 × N` (ponytail: true size unknown above the top bucket). Any
  other string raises `ValueError` naming it. Category: `Unknown` 0, `Delay-insensitive` 1, `Interactive` 2; other
  strings raise.
- Keys: `vmid` (U), `sub` (U), `cores` f32, `mem_gb` f32, `created` i64, `deleted` i64, `category` i8, `t_start`,
  `t_split`, `step` (i64 scalars), `cpu_min`, `cpu_avg`, `cpu_max`.
- `t_split = t_start + floor((T·300 − 86400) / 3600) · 3600`, so the held-out test period is the last ~24 h.
- `np.load(..., allow_pickle=False)` everywhere.

### 6.4 Synthetic data
`synthetic_series(V, T, seed)` returns the same keys: diurnal sinusoid + Gaussian noise + Pareto-sized bursts
(shape 1.5), clipped to 0–100; cores from {2, 4, 8}; memory = 4 × cores; random created/deleted. Used by tests and by
`irm reproduce --synthetic`.

## 7. Offline models: P95 and lifetime buckets

- Input: `data/raw/vmtable.csv.gz` (all VMs). `trace_end = max(deleted)`.
- Targets: P95 bucket of `p95maxcpu` in [0,25), [25,50), [50,75), [75,100]; lifetime (`deleted − created`) in
  < 15 min, 15–60 min, 1–24 h, ≥ 24 h. **Censoring**: a VM with `deleted ≥ trace_end − 300` and lifetime < 24 h has no
  lifetime label.
- Features at creation time `t_c`: category; cores; memory; hour of day; day index mod 7;
  `sub_prior_count`, `sub_prior_p95_mean`, `sub_prior_loglife_mean` over VMs of the same subscription with
  **`deleted < t_c`** (NaN when none); `dep_prior_created` = VMs of the same deployment with `created < t_c`.
- History must be vectorized: sort deletions by `(sub_code, deleted)` with cumulative sums, then `np.searchsorted` on
  the combined key `sub_code · 2^32 + time` with `side="left"` (strictly earlier). `dep_prior_created` uses
  `groupby(deployment).created.rank(method="min") − 1`.
- Model: `HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, categorical_features=[0], random_state=0)`.
  **Never pickled.**
- Evaluation: train on VMs with created and deleted < 20 days (1,728,000 s); test on VMs created in
  [20 days, `t_start` of `series.npz`, or 27 days if absent). Writes `reports/lifetime.json` per target: `accuracy`,
  `macro_f1`, `baseline_accuracy` (train majority class), `confusion`, `n_train`, `n_test`.
- Priors: retrain on VMs with `deleted < t_start` and predict for the VMs in `series.npz` →
  `data/processed/priors.npz`: `vmid`, `p95_bucket` i8, `life_bucket` i8, `p95_proba` [V,4], `life_proba` [V,4].

## 8. Forecaster

- Window: 48 input steps (4 h), 12 output steps (1 h), stride 3. Valid only when all 60 `cpu_*` values are finite.
- Inputs `[B,48,5]`: min/100, avg/100, max/100, sin and cos of time of day. Decoder inputs `[B,12,2]`: sin, cos of each
  target step. Target `[B,12]`: max/100.
- `AttnLSTM(hidden=64)`: encoder `nn.LSTM(5, 64, batch_first=True)`; decoder `nn.LSTMCell(2 + 64, 64)` initialised
  from the encoder's final state, fed the previous context (zeros first). Score `h_j · W_a e_i` (`W_a` = `Linear(64, 64,
  bias=False)`), softmax over the 48 steps, context `Σ α_i e_i`, output `Linear(64,1)(tanh(Linear(128,64)([h_j; ctx])))`.
  `forward` returns `(q [B,12], attn [B,12,48])`. The decoder never consumes its own predictions.
- Loss: pinball, τ = 0.95, `mean(max(τ·e, (τ−1)·e))` with `e = y − q`.
- Split: train windows whose last target step is `< t_split`; validation = those whose last target step falls in
  the final 10% of `[t_start, t_split)`; test = first target step `≥ t_split`. At most 400,000 train windows (seeded).
- Training: Adam 1e-3, batch 512, ≤ `--epochs`, early stop after 3 epochs without validation improvement, seeds for
  `torch` and `numpy`, a plain index loop (no `DataLoader` workers). Save the best
  `{"state_dict", "hidden", "t_in", "t_out"}` to `models/forecast.pt`; load with `torch.load(..., weights_only=True)`.
- Baselines: **last-window P95** = `np.percentile(input max/100, 95)` repeated 12 times; **ARIMA(2,0,1)** fitted with
  statsmodels on the 48 max/100 values, `q95 = get_forecast(12).conf_int(alpha=0.10)[:, 1]` (the upper end of a
  two-sided 90% interval is the one-sided 95% quantile). Warnings suppressed; a failed fit uses the last-window value and
  is counted in `arima_failures`.
- Metrics: `pinball`, `coverage = mean(y ≤ q)`, `mean_under = mean(max(0, y − q))`, `mean_over = mean(max(0, q − y))`.
- `reports/forecast.json`: `test_all` {lstm, last_window}, `test_arima_subset` {lstm, last_window, arima} over
  `--arima-windows` seeded test windows, `n_windows`, `arima_failures`, `epochs_run`, `val_pinball`,
  `mean_attention` (48 values).
- `--source local`: windows from `data/irm.db` (§11.2 buckets); evaluate only → `reports/forecast_local.json`.
- `predict_q95(model, x, dec)` is the one inference helper, used by §9 and §11.

## 9. Cluster simulator

Constants: `HOST_CORES 48`, `HOST_MEM_GB 384`, `OVERCOMMIT 2.0`, `P_IDLE_W 100`, `P_MAX_W 250`, `STEP 300`,
`K_WINDOW 144`, `K_MIN 24`.

- `n_hosts = max(4, ceil(1.25 × peak Σ cores of alive VMs / (HOST_CORES × OVERCOMMIT)))`.
- Each step `t` (from `t0` to `t1`), in order: **departures** (`deleted ≤ t`); **arrivals** (`created ≤ t`, not yet
  placed, `deleted > t`, in `created` order; VMs created before `t0` arrive at `t0`); **evacuation** on hour boundaries
  after `t0`; **accounting**; hosts with no VMs are off.
- Feasible host: `Σ cores + cores_v ≤ HOST_CORES × OVERCOMMIT` and `Σ mem + mem_v ≤ HOST_MEM_GB`. An arrival with no
  feasible host is rejected and counted.
- Evacuation: the source is the active host with the lowest `host_q95_frac`, if at least two hosts are active and a
  First-Fit plan (largest cores first) onto other **active** hosts exists. Then each of its VMs, largest first, is a
  decision whose candidates are feasible active hosts other than the source. If a VM has no candidate, evacuation stops
  there. Each move counts as a migration.
- Accounting: utilization is `cpu_max/100` and `cpu_avg/100`; a NaN reading means the VM contributes nothing this step
  but keeps its allocation. For each active host: `demand_max = Σ util_max · cores`,
  `energy += (P_IDLE + (P_MAX − P_IDLE) · min(1, demand_avg / HOST_CORES)) · 300 / 3.6e6` kWh,
  `overload = max(0, demand_max − HOST_CORES)`.
- Metrics: `energy_kwh`, `sla_overload_frac = Σ overload / Σ demand_max`, `overloaded_host_step_frac`, `migrations`,
  `rejected`, `mean_active_hosts`, `decisions`.
- **K(v, h)**: `x` = v's last `K_WINDOW` steps of `util_max · cores` before `t`; `y` = the same summed over the VMs
  currently on `h`, excluding `v`. Over steps where both are finite: if fewer than `K_MIN`, or either standard deviation
  is 0, `K = 0.5`; else `K = (1 − pearson(x, y)) / 2`.
- Forecast cache `data/processed/forecasts.npz`: `vm_q95 [V, hours]` = max over the hour of `predict_q95` using the 48
  steps before the hour; NaN when those steps are not all finite. A VM without a forecast uses its prior's upper bucket
  edge (0.25, 0.5, 0.75, 1.0) or 1.0 without priors, and `has_history = 0`.
- Candidate features (float32, in this order): `host_active`, `host_alloc_frac = Σ cores / (HOST_CORES × OVERCOMMIT)`,
  `host_mem_frac`, `host_util_now = demand_max / HOST_CORES`, `host_q95_frac = Σ_{v∈h} vm_q95 · cores / HOST_CORES`,
  `vm_cores_frac`, `vm_q95_frac`, `vm_has_history`, `life_b0..life_b3` (one-hot prior; all 0 without priors), `K`,
  `post_q95_frac = host_q95_frac + vm_q95_frac`.
- Policy: `choose(features [n,14], host_ids) -> index`. **FirstFit** = lowest host id. **BestFit** = highest
  `host_alloc_frac` after placement, ties to lowest id. Policies see only these features.
- Rewards, per decision on host `h` at `t_d`: `−W_SLA · Σ_{t ∈ (t_d, t_d+3600]} overload_h(t) / HOST_CORES / 12`;
  `−W_ENERGY` if `h` was off; `+W_ENERGY` to the decision that empties an evacuation source; `−W_MIG` for a migration.
  A reward is final after one hour or at episode end. The transition carries the next decision's candidate features
  and mask, and `done` for the episode's last decision.
- Ranges: test `[t_split, t_end]`; training episodes are random 12 h windows inside `[t_start, t_split − 12 h]`.

## 10. DQN

- `QNet`: MLP 14 → 64 → 64 → 1, ReLU. Q of a decision = `QNet(chosen features)`.
- Double DQN target: `y = r + γ (1 − done) Q_tgt(X_next[a*])`, `a* = argmax over the mask of Q_online(X_next)`; an
  empty mask means done. γ = 0.9, Huber loss, Adam 1e-3, gradient clip 10.
- Replay: numpy ring buffer of 50,000; next-candidate features padded to `n_hosts` with a mask. Batch 256, learn every
  4 decisions after 1,000 transitions, target sync every 1,000 gradient steps.
- ε decays linearly from 1.0 to 0.05 over the first 60% of expected decisions (episode 1's decision count ×
  `--episodes`). Seeds for torch, numpy, and episode windows.
- Save `models/dqn.pt` `{"state_dict", "w_sla", "w_energy", "w_mig"}`; load with `weights_only=True`.
- `irm evaluate placement`: FirstFit, BestFit, DQN (greedy) on the test range → `reports/placement.json`
  `{policy: metrics, "n_hosts", "t0", "t1", "weights"}`.
- `irm evaluate study` (added after the prototype; `.tasks/p8-dqn-study.md`): seeds 0–4, `util_scale 1.4`,
  `host_slack 2.0` so placement choices matter; policies FirstFit, BestFit, DQN, and DQN_noK (K feature fixed at 0.5,
  trained and evaluated that way) → `reports/placement_study.json` with mean and `ci95 = 1.96 × std / √n` per metric.

## 11. Live recommender

1. Leaf cgroups with samples in the last 24 h (not `host`). `ncpu = os.cpu_count()`.
2. Demand per sample `d = cpu_cores + coalesce(netrx_attrib_cores, 0)`. Buckets aligned to multiples of 300 s:
   min, mean, and max of `d / ncpu`.
3. Peak: if the last 48 buckets all exist and `models/forecast.pt` exists → `max(predict_q95) × ncpu`, source
   `forecast`. Else if ≥ `--min-samples` samples in 24 h → 95th percentile of `d`, source `empirical`. Else listed in
   `skipped` with the reason.
4. **Targets** (fixed after the prototype demo, BUILD_LOG): protected cgroups (`--protect`, exact names: `cpu.max =
   "max"`, `cpu.weight = 1000`, `memory.high = "max"`); non-protected cgroups with `peak ≥ --min-cores` (0.5); with
   `--only`, only the named non-protected cgroups. Every other qualifying cgroup is **background**: no item, listed in
   `skipped`, and its peak still counts as demand.
5. `reserve = Σ peak × headroom` (protected); `background = Σ peak` (background);
   `avail = max(0, ncpu − reserve − background)` — no floor term. Non-protected targets: `want = peak × headroom`; if
   `Σ want > avail`, scale each by `avail / Σ want`; then a 0.1-core floor per item (noted in its reason); ≥ 0.9 × ncpu →
   `"max"`; else `"<round(quota × 100000)> 100000"`; `cpu.weight = null`.
   `memory.high = max(64 MiB, ceil_MiB(max mem_bytes over 24 h × headroom))`.
6. Each item has a human-readable `reason`, e.g. `"q95 1.20 cores (forecast) × 1.25; squeezed to fit 12.3 free cores"`.
7. Pairs: among leaves with peak ≥ 0.5 cores, Pearson over the last 24 h of bucket max with ≥ 24 common buckets and
   non-zero variance; keep ρ ≥ 0.7, top 10: `{a, b, rho, k = (1 − ρ)/2}`.
8. Output `{"generated_at", "ncpu", "items": [{"cgroup", "source", "peak_cores", "cpu_max", "memory_high",
   "cpu_weight", "old": {file: value|null}, "reason"}], "pairs", "skipped"}`.

## 12. Execute

- Allowed prefixes: `/user.slice/user-{uid}.slice/user@{uid}.service/` (`os.getuid()`) plus each `--allow`.
- Validation of `(cgroup, file, value)`, all required: the name starts with `/`, contains no NUL and no `..`
  component; `realpath(root + name)` lies **strictly inside** `realpath(root + prefix)` for some prefix
  (`os.path.commonpath`); `cgroup.controllers` exists there; `file` ∈ {`cpu.max`, `memory.high`, `cpu.weight`}; the value
  matches `^(max|[0-9]+) 100000$` with quota ≥ 1000, or `^(max|[0-9]+)$` ≥ 67,108,864, or an integer in 1–10000,
  respectively. A failed item is rejected with a reason and never written.
- Clamp at apply time: a numeric `memory.high` below `memory.current × 1.1` is raised to `ceil_MiB` of that, noted
  `clamped`.
- Journal (in `data/irm.db`):
  ```sql
  create table if not exists journal (id integer primary key, batch integer not null, ts integer not null,
    cgroup text not null, file text not null, old text, new text not null, status text not null);
  ```
- `apply`: the plan is `cpu.max` and `memory.high` for every item, plus `cpu.weight` when not null, skipping
  `old == new`. Without `--yes`: print `cgroup  file  old → new` and write **nothing**, not even journal rows. With
  `--yes`: `batch = max(batch) + 1`. For each item: read `old`, insert `planned` and commit, write the value, then set
  `applied` or `failed:<errno name>` and commit. Continue after failures. Exit 1 if anything was rejected or failed.
- `revert`: rows of the batch with status `applied`, in descending `id`; write `old`; set `reverted` or
  `revert_failed:<errno name>`. A NULL `old` is skipped with a note.

## 13. Dashboard

- `ThreadingHTTPServer(("127.0.0.1", port))`. GET only (others → 405). `Host` must be `127.0.0.1:<port>` or
  `localhost:<port>`, else 421.
- Static files from a fixed map: `/` → `index.html`, `/app.js`, `/style.css`. No path is built from the URL.
- API (JSON; errors `{"error": …}` with 400/404):
  `GET /api/cgroups` (latest row per cgroup in the last 5 min); `GET /api/series?cgroup=&metric=&hours=` (metric from a
  whitelist of `samples` columns, `hours` an integer 1–48, cgroup must exist, at most 2,000 points averaged in SQL);
  `GET /api/attribution` (per cgroup, last hour mean `netrx_blamed_cores` vs `netrx_attrib_cores`);
  `GET /api/recommendations`; `GET /api/reports` (`overhead`, `lifetime`, `forecast`, `forecast_local`, `placement`,
  `slo` from `reports/*.json`, missing → null).
- SQLite opened read-only per request (§3). Values are passed as SQL parameters; the metric is used only after
  whitelist membership is checked.
- Headers on every response: `Content-Security-Policy: default-src 'self'; img-src 'self' data:; frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- UI: cgroup table; SVG line chart for a selected cgroup and metric; attribution table; recommendations table;
  report summaries. Text is set with `textContent` only. No external resources, no inline script or style.

## 14. SLO experiment

- Each role runs in its own transient scope: `systemd-run --user --scope --quiet --unit=irm-exp-<role>-<hex> --
  <sys.executable> -m irm.experiment <role> …`. Scopes land under `user@<uid>.service/app.slice`, inside §12's default
  prefix (R9).
- Roles: **service** (asyncio HTTP/1.1 on 127.0.0.1, each request runs SHA-256 over 64 KiB, repeated to about 2 ms,
  calibrated at start); **loadgen** (open-loop Poisson arrivals at `--rate` over 32 keep-alive connections; latency is
  measured from the **scheduled** send time, so queueing is not hidden; writes latencies to a temporary file);
  **cpuhog** (`os.cpu_count()` busy-loop processes; reports iterations/s); **nethog** (UDP 1,400-byte sender plus a
  draining receiver on loopback).
- Conditions: **A** service + loadgen; **B** A + cpuhog + nethog; **C** B with `irm`. For C, `irm monitor --interval 1`
  runs into a temporary database for a 60 s warm-up, then `recommend --protect <service> <loadgen> --min-samples 60`
  and `apply --yes`; measure; `revert` in `finally`.
- Repetition order: ABC, BCA, CAB, … for `--reps`, `--minutes` per condition. SLO target = 2 × p99 of the first A.
- `reports/slo.json` per condition: pooled p50/p95/p99 ms, per-rep p99 min/mean/max, `violation_rate` (fraction of 1 s
  windows whose p99 exceeds the target), completed rps, errors, cpuhog iterations/s; plus C's applied plan.
- `finally`: stop every scope (`systemctl --user stop`) and revert any applied batch.

## 15. Reproduce

`irm reproduce [--synthetic]` runs in order, printing elapsed time per step: prepare (or synthetic data), train
lifetime (skipped when synthetic), train forecast, evaluate forecast, forecast cache, train dqn, evaluate placement,
figures, `reports/RESULTS.md`. Seed 0 throughout.
Figures (matplotlib `Agg`, SVG, in `reports/figures/`): `forecast_example`, `forecast_metrics`, `attention`,
`placement` (normalised to FirstFit = 1), `slo` (when `reports/slo.json` exists). Colours and layout follow the
architect's figure spec. `RESULTS.md` is generated tables and figure links only; the narrative is `REPORT.md`.

## 16. Tests

- The coding agent runs tests only through `/home/shreenipane/.local/bin/irm-test` (SECURITY.md T2):
  `irm-test [pytest args]` runs `pytest -m "not live"`; `irm-test bpf` runs `make -C bpf`; `irm-test run <irm args>`
  runs `python -m irm` with `data/processed`, `models/`, `reports/` writable.
- `live` marker: needs real cgroup writes, `systemd-run`, root, or the network. The architect runs these.
- Fixture cgroup and `/proc` trees are built in `tmp_path` by helpers in `conftest.py`, not committed.
- Tiny sizes, fixed seeds, and the whole suite under 3 minutes.

## 17. Unverified claims

| # | Claim | Verified by |
|---|---|---|
| R1 | ~~`bwrap` nests inside agy's `--sandbox`~~ **Moot:** `--sandbox` hides `~/.local/bin`; agy runs without it | BUILD_LOG D.6 |
| R2 | **True with a static launcher:** the rule refuses other binaries, chaining, substitution, newlines, redirects; environment prefixes are accepted but cannot affect the launcher | BUILD_LOG D.6–D.7 |
| R3 | **True only when agy starts outside the repo:** its working directory is writable regardless of rules | BUILD_LOG D.6–D.7 |
| R4 | `bpf_skb_cgroup_id` is allowed in `cgroup_skb` programs and returns the socket's cgroup | Phase 2 load + live test |
| R5 | cgroup id equals `st_ino` of the cgroupfs directory | Phase 2 live test |
| R6 | `cgroup_skb/ingress` runs inside NET_RX softirq for local TCP/UDP delivery | Phase 2 live test (attribution ≥ 80%) |
| R7 | V2 CSVs have no header; reading files are time-ordered, so files 176–195 are the last ~3 days | Phase 3 fetch output |
| R8 | Bucket strings are integers or `>N` | Phase 3 prepare |
| R9 | `cpu.max`, `memory.high`, `cpu.weight` are writable in user scopes | Phase 9 live test |
| R10 | Monitor overhead < 1% of one core with ~180 cgroups | Phase 1 benchmark |
| R11 | **True:** `torch 2.14.0+cpu` installed from the PyTorch CPU index | Phase D `uv sync` |
