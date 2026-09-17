# Council Meeting — Verification of the VAGUS Project

| | |
|---|---|
| **Date** | 17 September 2026 |
| **Subject** | Independent verification of VAGUS before the faculty review |
| **Artifacts reviewed** | Repository `~/Heavy Coding/Projects/intelligent-resource-manager` (code, tests, `reports/*.json`, docs) · report `~/Downloads/vagus.md` · proposal slides `~/Downloads/Intelligent_Linux_Resource_Management_260917_011044.pdf` |
| **Question** | Are the implementation and the report's claims correct and supported by the code and committed results, and does the project address the proposal's problem statement? |
| **Chair** | Claude (main session), who also acted as the project's architect and authored much of the reviewed material |
| **Outcome** | Engineering verified as real and safe; headline claims over-credited; documentation contains false statements; two stage hazards found |

---

## 1. Members and method

| Seat | Persona | Model | Lens |
|---|---|---|---|
| A | The Architect | Opus | Architecture, whether the MAPE-K loop really closes, docs vs code |
| B | The Skeptic | Opus | Red-team: fairness of experiments, leakage, baselines, overclaims |
| C | The Pragmatist | Opus | Does it install and demo safely; smallest fixes before faculty |
| D | The Researcher | Opus | Kernel/eBPF correctness, ML methodology, prior art, fit with the proposal |

**Stage 1 — Independent review.** Each member worked in a fresh context with read-only access. No member could see the
others. Members read code, ran targeted tests, and wrote read-only probe scripts (reconstructing the recommender inputs,
re-running forecasting and placement baselines, checking the saved model, building the package from a clean copy).

**Stage 2 — Anonymized peer review.** All four reports were shuffled and relabelled Responses A–D. Each member critiqued
and ranked all four (including its own, impartially), and checked the claims on which reports differed.

**Stage 3 — Chair's synthesis.** Rankings aggregated; disputed claims resolved from the members' re-checks; action plan
merged.

**Ground rules for members.** No file modifications; no training, experiments, `irm apply --yes`, `sudo`, cgroup writes,
or network downloads.

---

## 2. Overall verdict

> **The engineering is real and safe, and every committed number is accurate — but VAGUS is not yet the system the
> README and report describe, and each headline claim is weaker than stated.**

Unanimous across all four members.

---

## 3. What was verified as correct

| Item | Verified by |
|---|---|
| All numbers in `reports/*.json` match the README and report | All four |
| First-Fit placement result reproduces exactly (15.66% overload, 68.28 kWh) | Architect, Skeptic, Researcher (independent probes) |
| LSTM test metrics reproduce from `models/forecast.pt` (0.0087 pinball, 95.2% coverage) | Researcher |
| SLO arithmetic: p99 −56.9%; violations 299 / 19 / 1 of 540 windows = 55.4% / 3.5% / 0.19% | Skeptic |
| Monitor overhead 0.684% of one core, 135 cgroups (scoped: 5 s interval, eBPF off) | Pragmatist, Skeptic |
| 94 tests collected; targeted suites pass (27, 22 and 36 tests across members) | Architect, Skeptic, Pragmatist |
| Dry-run `apply` writes nothing; dashboard bound to 127.0.0.1 | Pragmatist |
| eBPF design is valid against kernel source: `bpf_skb_cgroup_id` permitted in `cgroup_skb`; ingress hook runs before the TCP backlog, i.e. in NET_RX softirq context; entry/exit timing sound | Researcher |
| The DQN is still the best placement policy in the simulator (Pareto-best on overload and energy) | Architect, Skeptic, Researcher |
| Other references accurate: Banga/Druschel/Mogul OSDI'99, Cortez SOSP'17, Rzadca EuroSys'20, Hadary OSDI'20, Lo ISCA'15, Chen/Delimitrou/Martínez ASPLOS'19 | Researcher |

---

## 4. Findings

### 4.1 Stage hazards — found by the Pragmatist, confirmed by all

| # | Severity | Finding | Evidence | Fix |
|---|---|---|---|---|
| H1 | Critical | **A fresh clone does not install.** The VAGUS rename set `name = "vagus"` in `pyproject.toml`, but the code lives in `irm/`; `uv_build` expects `vagus/__init__.py`. The advertised `vagus` command does not exist. `run_tests.sh` also breaks because it calls `uv sync` when the venv is missing. | `git archive HEAD` into scratch → `uv build` / `uv sync --offline` fail: *"Expected a Python module at: vagus/__init__.py"*; `uv.lock:191` still `name = "irm"` | Add `module-name = "irm"` under `[tool.uv.build-backend]`, then `uv lock` (verified in scratch: wheel builds with both `irm` and `vagus` entry points) |
| H2 | Critical | **The default plan is the old over-reach plan.** `irm apply --from` defaults to `data/recommendations.json`, which dates from before the recommender fix: 137 items, 136 CPU caps (mostly 0.1 core) on `init.scope`, system services, Firefox and the Ptyxis terminal. The dashboard's Plan card serves it now. A skipped `recommend` plus `apply --yes` on stage would throttle the browser and terminal. | File contents; `cli.py:56`; journal batches 1–2 (114 writes each, since reverted, plus 36 EACCES failures) | Delete the file; have `DEMO.md` use `recommend --out data/demo-plan.json` and `apply --from data/demo-plan.json`; optionally make `apply` refuse plans older than a few minutes |

### 4.2 False or unsupported documentation claims — found by all four

| Claim (where) | Reality (evidence) |
|---|---|
| "Closed-loop autonomic MAPE-K", "dynamically clamping hogs" (README) | Operator-triggered one-shot commands; no daemon, no periodic re-plan, no feedback or auto-rollback. The SLO experiment applies a plan once (`experiment.py` condition C) |
| LSTM feeds the host recommender; "the planes share one forecaster and one co-location function" (README diagram, `ARCHITECTURE.md:13`, D14, line 68; report §5.2) | `recommend.py:72` uses `np.percentile(d, 95)` and labels every item `"source": "empirical"`; `sim.py:208-229` uses the empirical P95 of the last 48 readings; `predict_q95` is called only from a test; `recommend.py` re-implements correlation instead of using `compute_k` |
| Gradient-boosted trees for lifetime and sizing (README diagram) | Not built; the four lifetime features in the simulator are always zero |
| "Double-DQN with dueling heads … trained on the Azure 2019 VM trace" (`README.md:71`) | `QNet` is a plain 14→64→64→1 MLP (`dqn.py:22-35`); all data is synthetic (`sim.py:36`) |
| "All claims … measured on live systems" (README) | Forecasting and placement are synthetic and simulated |
| Limits written "atomically" (README) | Each write is committed separately and failures continue (`execute.py:183-202`) |
| eBPF attribution shown as operational (README) | Never loaded; `BUILD_LOG.md` records "Live load pending"; the SLO experiment passes no attribution stream |
| `irm/data.py`, `lifetime.py`, `reproduce.py` and an "Azure 2019 research plane" (`ARCHITECTURE.md`) | None of these modules exist |
| "Every stage of the control loop runs end to end" (report status line) | The Analyse stage is not connected to Plan |
| Report §2.2: softirq work run from `local_bh_enable` "is charged to whichever task was running" | Wrong. `kernel/sched/cputime.c` `irqtime_account_irq` books it as `CPUTIME_SOFTIRQ` unless the current task is `ksoftirqd`, so it is excluded from task and cgroup runtime. On this kernel (`IRQ_TIME_ACCOUNTING=y`) softirq time is **invisible** to cgroups, not misattributed |
| "Runs the full test suite in the sandbox" (README, `run_all.sh`) | `run_tests.sh` runs pytest directly, without the bubblewrap jail |
| "Independent security review" (report, pitch) | An automated review by an agent the architect ran |
| `DEMO.md` / `HOWTO.md` | Still call eBPF and the SLO experiment future work, list an already-fixed bug as a limitation, and `DEMO.md` still uses the old project name |
| README quick start | SSH clone URL (needs a GitHub key); no PATH note for `irm` |

### 4.3 The SLO headline — the Skeptic's key finding (top-ranked)

The latency result is real, but it does not demonstrate the mechanism it is credited to.

| # | Severity | Finding | Evidence |
|---|---|---|---|
| S1 | Critical | **No capacity was reserved for the protected service.** It was idle during the 60 s of monitoring, because the load generator starts only after `apply`. | `slo.json` applied plan: *"protected; peak 0.00 cores"*, *"0.00 reserved and 1.10 background"* |
| S2 | High | **The squeeze was small and depended on incidental laptop activity.** The CPU hog was capped at 13.24 cores against an observed 13.86 (−4.5%); the network hog at 1.66 vs 1.74. Rebuilding the recommender inputs reproduces the plan; with zero background, the two hog caps would sum to exactly 16.00 cores. | Skeptic's probe (`1323808` vs committed `1323824`) |
| S3 | High | **The likely lever is `cpu.weight=1000`, not reservation or ML** — but no weight-only or cap-only run exists to prove it. | No ablation arms in `experiment.py` |
| S4 | High | **Results come from superseded code and cannot be recomputed.** `slo.json` (commit `ec65dda`, 11:57) predates the experiment fix (`4923a6c`); raw latencies were written to a temporary directory and discarded. | `git log`; `experiment.py:531` |
| S5 | High | **The report's "biases against VAGUS" are wrong.** One listed bias affects only a statistic that is null; the other is ~0.5 s. Unlisted biases **favour** condition C: the unprotected load generator also benefits from freed CPU, and latency is end-to-end on the same host. | `vagus.md:553-554`; `slo.json` `cpuhog_ips: null` |
| S6 | Medium | **"Close to running alone" is an overstatement.** C's p99 (9.53 ms) is 43% above A (6.65 ms); C's violations (3.5%) are 19× A's (0.19%). Only 3 repetitions, no statistical test (best-case rank test p ≈ 0.10 two-sided). | `slo.json` |
| S7 | Medium | **Per-request work is uncontrolled.** `calibrate_iters` times 50 hashes at each service start and never logs the result; repeated calls gave 12–23 iterations on a busy CPU. C's p50 below A's is consistent with this. (Suspected; evidence weak on a shared CPU.) | Skeptic's probe |
| S8 | Medium | **The neighbour's cost is unmeasured.** The hog's throughput under the cap was not recorded for condition C. | `slo.json` |
| S9 | Low | In this experiment the network hog's sender and receiver share one scope, so eBPF attribution could not have separated them even if enabled. | Architect; confirmed by Pragmatist |

**Accurate phrasing agreed by the council:** *"A cgroup weight plus hog-cap policy derived from 60 s of telemetry cut the
service's end-to-end p99 latency by 57% under noisy neighbours; the ML and eBPF components were not involved."*

### 4.4 Weak baselines — Skeptic and Researcher, reproduced by three members

**Forecaster.**

| Model | Pinball loss | P95 coverage | Source |
|---|---|---|---|
| Attention LSTM | 0.00868 | 95.2% | `forecast.json` |
| **Seasonal-naive + one validation-fitted offset** | **0.00931** | 94.7–94.8% | Skeptic; reproduced by Architect and Researcher |
| Last-window P95 + same offset | 0.01188 | 95.1% | Skeptic; reproduced by Architect |
| Last-window P95 (as reported) | 0.01483 | 80.2% | Reproduced exactly |

- The synthetic generator has an exact 24-hour (288-step) period and the LSTM receives time-of-day features; the reported
  baselines receive neither. Against a fair seasonal baseline the LSTM is **about 7% better, not 41%**.
- The 95% coverage "win" is calibration that any baseline achieves with one offset.
- **Attention is inert:** per-sample attention entropy 3.867 vs 3.871 for uniform weights (Researcher); mean weights
  0.0179–0.0244 vs 1/48 = 0.0208. No LSTM-without-attention comparison exists.
- ARIMA(2,0,1) is implemented correctly (upper bound of the 90% interval = Gaussian q95) but is a straw man: fixed order,
  48 points, no seasonality. The report's "ARIMA assumes stationary data" is imprecise (the I term handles
  non-stationarity; the limit is linear, Gaussian errors).
- The `forecast.py` docstring says "additive Bahdanau" attention; the implementation is multiplicative (Luong-general).

**Placement.**

| Policy (5 seeds, study config) | SLA overload | Energy (kWh) | Migrations | Source |
|---|---|---|---|---|
| First-Fit (as reported) | 15.66% | 68.3 | 2.2 | Reproduced exactly |
| Best-Fit capped at forecast P95 ≤ 1.1 | 12.53% | 69.9 | 50.6 | Skeptic; reproduced by Architect and Researcher |
| Threshold best-fit on forecast P95 | 12.3% | 70.5 | — | Researcher |
| First-Fit with forecast cap 0.9 / 0.8 / 0.7 | 12.50% / 12.36% / 11.40% | 71.7 / 72.3 / 74.7 | — | Skeptic's re-run of the Architect's heuristic |
| **DQN (as reported)** | **10.60%** | **69.2** | 47 | `placement_study.json` |

- The reported baselines use only requested cores under 2× overcommit, so they cannot avoid overload.
- **Forecast-awareness alone recovers 58–66% of the DQN's gain.** The DQN remains Pareto-best, by about 2 points.
- Per seed (Architect): the DQN wins seeds 0–3; on seed 4, forecast caps of 0.8 and 0.7 beat it on overload (11.1% and
  11.0% vs 12.2%) at 7–9% more energy.
- **The co-location coefficient K has no effect** (DQN without K: 10.45%).
- **Confidence intervals** use z = 1.96 at n = 5 (`dqn.py:545`); t₄ = 2.776 widens them 1.42×. The conclusion still holds.
- The DQN makes 47 migrations per day vs ~3 for the baselines and uses +1.4% energy, contradicting the proposal's
  "migration-free scheduling" and its efficiency goal.
- Setup caveats: the "held-out day" uses the same VMs and a stationary periodic generator; cluster size is derived from
  the 3-day peak including the test day; the regime (2× overcommit, ×1.4 utilisation) is contrived; the per-decision
  MDP with γ = 0.9 is not a Markov model of the cluster.

### 4.5 Fit with the proposal — Researcher and Architect

| Proposal deliverable (slide 12) | Council assessment |
|---|---|
| 1. Telemetry with sub-1% overhead | **Partly met.** Monitor meets the target (scoped: 5 s, no eBPF). eBPF never loaded or verifier-checked; its per-packet and per-softirq overhead is unmeasured. The monitor ignores `irq.pressure`, which exists on this machine |
| 2. Attention-LSTM for P95 and lifetime, *deployed* | **Partly met.** P95 forecaster on synthetic data only, not deployed into Plan; attention unproven; no lifetime model |
| 3. Co-location-aware DQN consolidation | **Met in simulation only.** K has no effect; energy got worse; heavy migrations |
| 4. Kernel-level enforcement bridging Resource Containers with cgroups v2 | **Not met as stated.** Userspace `cpu.max` / `cpu.weight` writes; no kernel-level charging (disclosed in report §11) |

Additional points:

- **The over-provisioning and waste half of the problem is never measured** on the live host.
- **eBPF blind spots** even when loaded: threaded NAPI, busy-poll and tun delivery never set the NET_RX flag; the socket's
  cgroup is its creator's (systemd socket activation, docker-proxy); GRO skbs are not packets; only NET_RX is covered.
- **K is used differently from EVMC**, which uses the co-location coefficient to choose which VM to migrate off an
  overloaded host. VAGUS has no overload-triggered migration, and new arrivals within the test window get K = 0.5. Present
  `K = (1 − ρ)/2` as VAGUS's own definition.
- **Missing prior art:** **Iron (Khalid et al., NSDI 2018)** — per-container network softirq CPU accounting enforced against
  cgroup quotas, the closest match to the proposal's Execute slide. Also LRP (Druschel & Banga, OSDI 1996);
  MQ-RNN, DeepAR and TFT (quantile sequence forecasters); Paragon and Quasar (interference-aware placement).
- **Incomplete citation:** EVMC = Zhang, Gao, Liu, Tan, *Electronics* 14(19):3813, 2025, doi:10.3390/electronics14193813
  (exact formula not verified; publisher page blocked).
- **Resource Central** predicts P95 *buckets at VM creation*; VAGUS forecasts per-step quantiles from 4 h of history.

### 4.6 Smaller design issues

- `apply` never checks the plan's `generated_at`, so stale plans can be applied (the root cause of H2).
- `revert` writes old values without checking the current value, so it can overwrite someone else's later change.
- Protected cgroups are forced to `cpu.max=max` and `memory.high=max`, erasing any limits an operator had set.
- Overhead was not measured at the 1 s interval used in the experiment, nor with eBPF attribution on (which adds a second
  cgroup-tree walk per sweep).
- The report's "~63% readiness" is fair; the council advised **not** presenting "85% demo-ready".

---

## 5. Peer review

### 5.1 Rankings

Anonymized labels: Response A = Skeptic · B = Researcher · C = Architect · D = Pragmatist.

| Reviewer | Ranking (best → worst) |
|---|---|
| Pragmatist | A > B > D > C |
| Architect | A > D > B > C |
| Researcher | A > D > B > C |
| Skeptic | B > A > D > C |

### 5.2 Aggregate

| Member (model) | Stance | Avg peer rank |
|---|---|---|
| **The Skeptic** (Opus) | Numbers are real, but every headline rests on a weak comparison; README overclaims badly | **1.25** |
| The Researcher (Opus) | Careful engineering, but it is a cgroup CPU-limit tool plus separate simulated ML, not the proposal's closed loop | 2.25 |
| The Pragmatist (Opus) | Real core, but the clone won't install, the stale plan is a stage hazard, and the README is easy to disprove — all fixable with edits | 2.50 |
| The Architect (Opus) | Components solid, but the claimed system doesn't exist: three disconnected pieces, no loop | 4.00 |

**Chair's note on the ranking.** Not overruled, but the Architect's last place reflects overlap with others and one wrong
citation (`cli.py:249-457` in a 223-line file), not wrong conclusions. Its "no closed loop / Analyse not connected"
finding is among the most important framing corrections.

### 5.3 Peer critiques in brief

- **Skeptic (Response A):** best methodology, with reproducible probes that shrank the forecasting and placement claims
  and exposed the SLO provenance problem. Overstated "~1.1 free cores" as the mechanism (a CPU cap throttles rather than
  idling cores) and a one-sided p-value. Missed the install break, the stale plan, the §2.2 kernel error and Iron.
- **Researcher (Response B):** deepest technical review — kernel-source checks, §2.2 error, inert attention, Iron, EVMC.
  Overgeneralised "new arrivals get K = 0.5" (long-lived VMs at the start of day 3 have real K). Missed the install break,
  stale plan, README/ARCHITECTURE falsehoods and the superseded-code issue.
- **Pragmatist (Response D):** the only member to find the two stage hazards. Called batches 1–2 "applied" without noting
  they were later reverted; one line citation off. Thin on ML and kernel methodology.
- **Architect (Response C):** fullest account of the missing loop, the unconnected Analyse stage and the non-existent
  modules; spotted the shared nethog scope. Wrong `cli.py` line numbers; an idle-time estimate ignored the network hog's
  cap; understated how much of the DQN gain simple heuristics recover.

---

## 6. Disputed claims and how they were resolved

| Claim | Resolution |
|---|---|
| Fresh clone fails `uv sync` (Pragmatist) | **Confirmed.** Pragmatist and one reviewer built from `git archive HEAD` and saw the error; others confirmed from `pyproject.toml` and `uv.lock` |
| Report §2.2 softirq billing is wrong (Researcher) | **Confirmed.** Three reviewers agree with the reading of `irqtime_account_irq`; `IRQ_TIME_ACCOUNTING=y` confirmed on this machine |
| Seasonal-naive baseline 0.00931 / 94.7% (Skeptic) | **Confirmed** by two independent re-runs (offsets 0.0665 and 0.0675; coverage 94.67–94.8%) |
| Heuristic placement numbers differ across A, B, C | **Not contradictory.** They are different heuristics on one overload–energy trade-off curve; all three re-run; all recover ~58–66% of the gap; none beats the DQN's mean |
| "Hog caps sum to exactly 16.00 cores with zero background" (Skeptic) | **Confirmed** by arithmetic (`recommend.py:139-142`) |
| Mechanism of the SLO win: ~1.1 incidental free cores vs `cpu.weight=1000` | **Unresolved.** Architect and the Skeptic's own peer review favour weight (a cap throttles rather than idles cores). Settled only by a weight-only experimental arm |
| DQN strength: "holds up" (Architect) vs "two-thirds is forecast-awareness" (Skeptic, Researcher) | **Both true.** DQN is Pareto-best in simulation, but by ~2 points |
| CI error "≈30% too narrow" vs "≈42%" | **Same fact:** current intervals are ~29% too narrow; correct ones are 1.42× wider |
| eBPF "not verifier-passed" (Researcher) | **Confirmed.** Load failed with EPERM before the verifier ran |

---

## 7. Consensus and dissent

- **Strongest agreement.** Every number is arithmetically correct but over-credited. The live SLO win used neither ML nor
  eBPF. The README and ARCHITECTURE must be corrected before faculty open the repository.
- **Sharpest disagreement.** What actually protected the service in the SLO experiment — incidental free capacity or the
  service's CPU weight. Only a weight-only run can settle it.
- **Minority view (Researcher).** On modern Linux, the proposal's "misattribution" problem is really *invisibility* of
  softirq time to cgroups; the problem statement (slide 3) should be reframed accordingly.

---

## 8. Chair's acknowledgements

The chair also acted as the project's architect. The following errors in the reviewed material are the chair's own:

1. **"Planes share the forecaster" and "live limits come from q95 forecasts"** in `ARCHITECTURE.md` and the report. The
   prototype specifications deliberately switched the recommender and simulator to an empirical P95, and the documents
   were never updated.
2. **Report §2.2** misdescribes how Linux accounts softirq time.
3. **SLO experiment design:** the load generator starts after monitoring, so the protected service's reserve was always
   zero; raw latencies were not persisted; no ablation arms.
4. **Forecaster baselines** without seasonality, which inflated the claimed LSTM advantage ("41%").
5. **Confidence intervals** specified with z = 1.96 instead of t for n = 5.
6. **Sequencing:** results committed before a later code change, so they cannot be reproduced exactly.
7. **The report's "biases against VAGUS"** paragraph, which was wrong.
8. **The stale plan file** left on disk from the live demo.
9. **Pitch and deck wording** ("41% lower loss", "reserves CPU for protected services", "85% demo-ready", "independent
   security review").

Items not authored by the chair but also requiring correction: the README rewrite and project rename (commit `45593a1`),
which introduced the install break and the dueling-DQN, Azure-trace, GBDT and "atomic" claims.

---

## 9. Action plan

| Priority | Action | Effort | Addresses |
|---|---|---|---|
| **1 — now** | Add `module-name = "irm"` and run `uv lock`; delete `data/recommendations.json`; make `DEMO.md` use an explicit plan file; fix the README quick start (HTTPS clone URL, PATH note) | ~10 min | H1, H2 |
| **2 — before faculty** | Correct README, ARCHITECTURE.md, `vagus.md`, the pitch script and deck: single-pass operator-run pipeline; empirical P95 live, LSTM offline; no GBDT, dueling heads, Azure data or atomic writes; eBPF not loaded; §2.2 rewritten; SLO metric defined and results labelled one-shot, end-to-end, same-host; forecasting and DQN labelled synthetic/simulated; "automated agent review"; cite Iron; full EVMC citation | 30–60 min | §4.2, §4.3, §4.5 |
| **3 — if ~1.5 h is available** | Re-run the SLO experiment on current code: service under load while monitored (real reserve); weight-only, cap-only and static-setting arms; fixed per-request work; raw latencies persisted; hog throughput recorded; load generator protected or separated | ~1.5 h | S1–S8 |
| **4 — next** | Add fair baselines and restate gains: seasonal-naive + offset and a plain LSTM for forecasting; forecast-aware heuristic for placement; t-based confidence intervals; report migrations and energy; drop "attention helps" and "co-location-aware" | 1–2 h | §4.4 |
| **5 — next** | Run eBPF live with sender and receiver in different cgroups and measure its overhead — or present attribution strictly as a design and state that the attribution gap and deliverable 4 remain open | ~30 min + root | §4.5 |
| Later | Connect the LSTM to the recommender and simulator, or keep describing it as standalone; add a periodic loop with a PSI/SLO check and auto-revert, or stop calling the system autonomic; `apply` refuses stale plans; `revert` checks current values; lifetime model; real Azure trace | Larger | §4.2, §4.6 |

---

## 10. Prepared answers for the faculty review

| Likely question | Answer |
|---|---|
| **What did the ML contribute to the 57% latency reduction?** | "Nothing yet. The live result comes from a cgroup weight plus hog-cap policy derived from measured P95. The forecaster and DQN are evaluated offline, and connecting them is the next step." |
| **Is it really a closed loop?** | "Not yet. It is an operator-triggered Monitor → Plan → Execute pipeline with dry run and revert. Continuous re-planning with a pressure-based rollback check is future work." |
| **How much better is the LSTM?** | "About 7% lower pinball loss than a seasonal-naive baseline on synthetic data; attention did not measurably help." |
| **How much better is the DQN?** | "In simulation it has the lowest overload, about 2 points better than a forecast-aware heuristic, at slightly higher energy and many more migrations. The co-location coefficient made no difference." |
| **Does eBPF fix the attribution gap?** | "The design is checked against kernel source but has not been loaded yet. On modern Linux softirq time is invisible to cgroups rather than mis-billed; eBPF can measure it, but charging it inside the kernel would need a patch. The closest prior work is Iron (NSDI 2018)." |

---

*The council consisted of four independent Claude agents (fresh contexts, distinct personas) with an anonymized
peer-review round, run on the user's own subscription.*
