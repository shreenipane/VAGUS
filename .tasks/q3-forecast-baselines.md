# Task Q3 — Forecaster: fair baselines, no-attention ablation, attention statistics

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md`, `council meeting.md` §4.4, then `irm/forecast.py` and `tests/test_forecast.py`.

**Five other agents are editing other files right now.** Touch **only** `irm/forecast.py` and `tests/test_forecast.py`.
Run **only** `tests/test_forecast.py`.

## Why
The synthetic generator has an exact 288-step (24 h) period and the LSTM gets time-of-day features, but the baselines saw
only 4 h of history and were uncalibrated. A seasonal-naive baseline plus one offset scored 0.00931 pinball / 94.7%
coverage against the LSTM's 0.00868 / 95.2%. Attention weights were nearly uniform, and there was no LSTM without attention.

## Changes (`irm/forecast.py`)
1. **Seasonal-naive baseline.** For a window whose target steps are `t…t+11`, the base forecast is `cpu_max/100` at
   `t−288…t−277` (the same steps one day earlier). Windows without that history are excluded from every seasonal
   comparison.
2. **Calibrated baselines.** For `seasonal_naive` and `last_window`, fit one scalar offset on the **validation** windows:
   the 0.95-quantile of `(y − base)` over all validation target steps. Report `seasonal_naive_cal` and
   `last_window_cal` = base + offset (offsets recorded as `offsets: {name: value}`). Never fit on test windows.
3. **No-attention ablation.** `PlainLSTM(hidden=64)`: the same encoder; the decoder `LSTMCell(2, 64)` initialised from the
   encoder state and fed only target-time features; output `Linear(64,1)(tanh(Linear(64,64)(h_j)))`. `forward` returns
   `(q, None)`. Train it with exactly the same data, budget, seed and early stopping as the attention model. Report
   `lstm_noattn`.
4. **Attention statistics.** Over the test windows, the per-sample entropy of attention weights averaged across decoder
   steps: report `attention_entropy: {"mean", "p5", "p95", "uniform": ln(48)}`.
5. **Reports.** Keep all existing keys. Add: `test_all.lstm_noattn`, `test_all.last_window_cal`;
   `test_seasonal_subset: {lstm, lstm_noattn, seasonal_naive, seasonal_naive_cal, last_window, last_window_cal,
   n_windows}`; `test_arima_subset` gains `seasonal_naive_cal`, `lstm_noattn` (on the same 100 windows, chosen from windows
   that have seasonal history); `offsets`; `attention_entropy`.
6. **Docstring.** `AttnLSTM` uses Luong "general" (multiplicative) attention — fix the docstring that says additive
   Bahdanau.
7. Keep `demo()` under 4 minutes on CPU (two models now).

## Tests
- On a toy series with period 288, the seasonal base for target step `t` equals the value at `t−288`.
- Offset calibration: on synthetic validation residuals, calibrated coverage on held-out residuals is within 0.03 of 0.95.
- `PlainLSTM` output shape `[B,12]`.
- Entropy of uniform weights equals `ln(48)`.
- A tiny `demo` writes every new key, and no calibration uses test windows (assert the offset is unchanged when test
  targets are perturbed).

Run `/home/shreenipane/.local/bin/irm-test tests/test_forecast.py -q` until green. Print the `AGENTS.md` §6 summary.
