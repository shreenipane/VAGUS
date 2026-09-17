# How to run the Intelligent Resource Manager

Tested on Fedora 44, kernel 7.1, Python 3.14, uv 0.12. Every command below runs from the repository root:

```sh
cd ~/"Heavy Coding/Projects/intelligent-resource-manager"
```

## 1. One-time setup

**Requirements:** Linux with cgroup v2 (`stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`), Python ≥ 3.14, `uv`,
`bubblewrap` (for the jailed test runner), and systemd user sessions (for the demo scopes).

```sh
# Dependencies into a virtualenv kept outside the repository (see SECURITY.md T3)
UV_PROJECT_ENVIRONMENT=~/.local/share/irm/venv uv sync
```

Two helper commands live in `~/.local/bin` (make sure it is on your `PATH`):

| Command | What it does |
|---|---|
| `irm` | Runs the project from that virtualenv. Source copy of the wrapper: `.tasks/` (see BUILD_LOG D.5) |
| `irm-test` | Runs tests, the BPF build, or long jobs inside a bubblewrap jail (no network, no home directory) |

If `irm` is missing on a new machine, this is equivalent:
`~/.local/share/irm/venv/bin/irm <args>` (or `uv run irm <args>` with the same `UV_PROJECT_ENVIRONMENT`).

## 2. Live host: Monitor → Plan → Execute

```sh
irm monitor &                          # telemetry every 5 s into data/irm.db (Ctrl-C or kill to stop)
irm dashboard                          # http://127.0.0.1:8765  (localhost only, read-only)
```

After at least a minute of monitoring:

```sh
irm recommend --min-samples 12 --hours 1                 # limits for every busy cgroup → data/recommendations.json
irm recommend --protect <cgroup path> --min-samples 12   # reserve capacity for a latency-critical cgroup
irm apply                                                # dry run: prints old → new, writes nothing
irm apply --yes                                          # writes cpu.max / memory.high / cpu.weight, journaled
irm revert                                               # restores the previous values of the last batch
```

- ⚠️ **Prototype bug:** `recommend` currently emits limits for every busy cgroup in your session, and the 0.1-core floor
  per cgroup can over-squeeze the budget. Until it is fixed, filter `data/recommendations.json` to the cgroups you mean
  (see DEMO.md §3) and pass the filtered file to `apply --from`. If you already applied everything: `irm revert`.
- Cgroup paths look like `/user.slice/user-1000.slice/user@1000.service/app.slice/<name>.scope` (the dashboard shows
  the full path when you hover over a name).
- `apply` only writes inside your own systemd user subtree (`user@<uid>.service`). Add others with `--allow`, at your
  own risk.
- Defaults: `--min-samples 360` (30 minutes at 5 s) and `--hours 24`. The lower values above are for demos.

Measure the monitor's own cost:

```sh
irm bench overhead --seconds 60        # → reports/overhead.json
```

## 3. Research plane: Analyse and Plan models

```sh
irm train forecast                     # attention-LSTM P95 forecaster vs last-window P95 and ARIMA → reports/forecast.json
irm evaluate placement                 # simulated cluster: First-Fit, Best-Fit, DQN → reports/placement.json
```

Both use synthetic workloads in the prototype and take about 1–3 minutes on CPU. Models are saved to `models/`. To
run them jailed (recommended for code you have not reviewed): `irm-test run train forecast`, `irm-test run evaluate
placement`. The dashboard's Analyse card shows the results.

## 4. Tests

```sh
irm-test -q                            # full suite, jailed (tests marked `live` are excluded)
irm-test tests/test_monitor.py -q      # one file
irm-test .tasks/test_jail_probe.py -q  # checks that the jail still hides your home directory and network
```

## 5. Demo

Step-by-step faculty demo with talking points: [DEMO.md](DEMO.md).

## 6. Coming later (designed, not yet built)

| Feature | Needs |
|---|---|
| eBPF softirq attribution: `sudo bpf/attrib \| irm monitor --attrib -` | `sudo dnf install -y clang llvm bpftool libbpf-devel elfutils-libelf-devel zlib-devel`, then Phase 2b |
| Real Azure 2019 trace: `irm data fetch`, `irm data prepare` | Phase 3; ~17 GB streamed, ~2% kept |
| Live SLO experiment: `irm experiment slo` | Phase 11 |
| One-command reproduction: `irm reproduce` | Phase 12 |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `irm: command not found` | Add `~/.local/bin` to `PATH`, or use `~/.local/share/irm/venv/bin/irm` |
| Dashboard port in use | `irm dashboard --port 8800` |
| `recommend` lists everything under `skipped` | Monitor longer, or lower `--min-samples` |
| `apply` rejects a cgroup | It is outside `user@<uid>.service`, or the path does not exist; see the printed reason |
| Leftover demo load | `systemctl --user stop noisy-demo.scope critical-demo.scope` |
| Undo every applied change | `irm revert` (repeat with `--batch N` for older batches) |
