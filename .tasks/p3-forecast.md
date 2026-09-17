# Prototype task P3 — Attention-LSTM P95 forecaster on synthetic bursty workloads

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, then `TRD.md` §8.

**Deadline: a faculty demo in 25 minutes.** Other agents are writing `irm/cli.py`, `irm/recommend.py`,
`irm/execute.py`, `irm/dashboard.py`, `irm/sim.py`, `irm/dqn.py` right now: never touch those files.

## Files to touch
- `irm/forecast.py` (create), `tests/test_forecast.py` (create)

## `irm/forecast.py`
- `synthetic(V, T, seed) -> dict`: `cpu_min`, `cpu_avg`, `cpu_max` float32 `[V, T]` percent in 0–100, 5-minute steps,
  `t_start = 0`. Per VM: base level 5–40%, diurnal sinusoid (amplitude 5–25, random phase), Gaussian noise, and
  heavy-tailed bursts (onsets with probability 0.01 per step, height from a Pareto with shape 1.5 scaled by 10, lasting
  1–6 steps). `cpu_max ≥ cpu_avg ≥ cpu_min`, clipped. Deterministic for a seed.
- `AttnLSTM(hidden=64)` exactly as TRD §8; `pinball(q, y, tau=0.95)`; `metrics(q, y) -> dict` with `pinball`,
  `coverage`, `mean_under`, `mean_over`.
- `windows(data, t_in=48, t_out=12, stride=3)` → `(X [N,48,5], D [N,12,2], Y [N,12], t_end [N])` using TRD §8 features.
- Time split: test = windows whose first target step is in the last 20% of steps; validation = the last 10% of the
  remaining windows by time; train = the rest (their targets end before the test period starts).
- Baselines: last-window P95; ARIMA(2,0,1) via statsmodels on 100 seeded test windows (warnings suppressed, failure →
  last-window value, counted).
- `demo(out_json, seed=0, epochs=5, V=300, T=2016) -> dict`: at most 60,000 train windows (seeded), Adam 1e-3, batch
  512, early stop patience 2, `torch.manual_seed`. Must finish in under 2 minutes on CPU; print one line per epoch.
  Writes JSON (indent 2): `test_all` {lstm, last_window}, `test_arima_subset` {lstm, last_window, arima},
  `n_windows`, `arima_failures`, `epochs_run`, `val_pinball`, `mean_attention` (48 floats),
  `example` {`history` (48 values of max/100), `actual` (12), `lstm` (12), `last_window` (12)} for one test window
  that contains a burst in its target if one exists. Saves `models/forecast.pt` per TRD §8 (create the directory). The
  JSON's parent directory is created if missing.

## Tests (fast)
Pinball on hand values; coverage math; attention weights sum to 1; windows never put a target step of a train window
inside the test period; a tiny `demo` (`V=12, T=500, epochs=1`) writes every JSON key and a model that loads with
`torch.load(..., weights_only=True)`.

Run `/home/shreenipane/.local/bin/irm-test -q` until green. Print the `AGENTS.md` §6 summary.
