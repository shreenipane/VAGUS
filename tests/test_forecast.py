"""Tests for Attention-LSTM P95 forecaster on bursty workloads per TRD §8."""

from pathlib import Path
import json

import numpy as np
import pytest
import torch

from irm.forecast import (
    AttnLSTM,
    PlainLSTM,
    compute_attention_entropy,
    demo,
    fit_offset,
    metrics,
    pinball,
    predict_q95,
    seasonal_base,
    split_windows,
    synthetic,
    time_split,
    windows,
)


def test_pinball_hand_values():
    # y=1.0, q=0.8 -> e=0.2 > 0 -> 0.95 * 0.2 = 0.19
    # y=0.5, q=0.7 -> e=-0.2 < 0 -> (0.95 - 1.0) * (-0.2) = 0.01
    # y=0.6, q=0.6 -> e=0.0 -> 0.0
    # Mean: (0.19 + 0.01 + 0.0) / 3 = 0.20 / 3 = 1 / 15
    y_np = np.array([1.0, 0.5, 0.6], dtype=np.float64)
    q_np = np.array([0.8, 0.7, 0.6], dtype=np.float64)
    expected = 0.20 / 3.0

    loss_np = pinball(q_np, y_np, tau=0.95)
    assert pytest.approx(loss_np, 1e-6) == expected

    # PyTorch tensor
    y_th = torch.tensor([1.0, 0.5, 0.6], dtype=torch.float32)
    q_th = torch.tensor([0.8, 0.7, 0.6], dtype=torch.float32)
    loss_th = pinball(q_th, y_th, tau=0.95)
    assert pytest.approx(loss_th.item(), 1e-5) == expected


def test_coverage_math():
    y = np.array([0.2, 0.5, 0.8, 0.9])
    q = np.array([0.3, 0.4, 0.8, 0.7])
    # y <= q: [True, False, True, False] -> coverage = 2/4 = 0.5
    # under: [0, 0.1, 0, 0.2] -> mean_under = 0.3 / 4 = 0.075
    # over:  [0.1, 0, 0, 0]   -> mean_over = 0.1 / 4 = 0.025
    # pinball: [0.05 * 0.1, 0.95 * 0.1, 0, 0.95 * 0.2] / 4
    #        = [0.005, 0.095, 0, 0.190] / 4 = 0.290 / 4 = 0.0725
    m = metrics(q, y)
    assert m["coverage"] == pytest.approx(0.5)
    assert m["mean_under"] == pytest.approx(0.075)
    assert m["mean_over"] == pytest.approx(0.025)
    assert m["pinball"] == pytest.approx(0.0725)


def test_attention_weights_sum_to_one():
    torch.manual_seed(42)
    model = AttnLSTM(hidden=64, t_in=48, t_out=12)
    x = torch.randn(3, 48, 5)
    d = torch.randn(3, 12, 2)

    q, attn = model(x, d)
    assert q.shape == (3, 12)
    assert attn.shape == (3, 12, 48)

    sums = attn.sum(dim=-1)
    assert torch.allclose(sums, torch.ones_like(sums), atol=1e-5)
    assert (attn >= 0.0).all()


def test_windows_no_train_step_in_test_period():
    V, T = 6, 250
    data = synthetic(V, T, seed=7)
    X, D, Y, t_end = windows(data, t_in=48, t_out=12, stride=3)

    assert len(X) > 0
    t_split = int(0.8 * T)  # 200
    train_idx, val_idx, test_idx = time_split(t_end, T=T, t_in=48, t_out=12)

    # Test windows: first target step >= t_split
    first_target = t_end[test_idx] - 12 + 1
    assert (first_target >= t_split).all()

    # Train windows: target must strictly end before test period starts (t_end < t_split)
    train_last_step = t_end[train_idx]
    assert (train_last_step < t_split).all()

    # Targets of train windows must never enter test period
    assert np.max(train_last_step) < t_split

    # No index overlap between train, val, and test
    assert len(set(train_idx).intersection(set(test_idx))) == 0
    assert len(set(val_idx).intersection(set(test_idx))) == 0
    assert len(set(train_idx).intersection(set(val_idx))) == 0


def test_tiny_demo_writes_json_and_model(tmp_path: Path):
    out_json = tmp_path / "reports" / "forecast.json"

    report = demo(
        out_json=out_json,
        seed=0,
        epochs=1,
        V=12,
        T=500,
    )

    # Verify JSON file and structure
    assert out_json.exists()
    with open(out_json, encoding="utf-8") as f:
        doc = json.load(f)

    for key in (
        "test_all",
        "test_seasonal_subset",
        "test_arima_subset",
        "offsets",
        "attention_entropy",
        "n_windows",
        "arima_failures",
        "epochs_run",
        "val_pinball",
        "mean_attention",
        "example",
    ):
        assert key in doc

    assert "lstm" in doc["test_all"]
    assert "lstm_noattn" in doc["test_all"]
    assert "last_window" in doc["test_all"]
    assert "last_window_cal" in doc["test_all"]

    for subkey in (
        "lstm",
        "lstm_noattn",
        "seasonal_naive",
        "seasonal_naive_cal",
        "last_window",
        "last_window_cal",
        "n_windows",
    ):
        assert subkey in doc["test_seasonal_subset"]

    assert "lstm" in doc["test_arima_subset"]
    assert "lstm_noattn" in doc["test_arima_subset"]
    assert "last_window" in doc["test_arima_subset"]
    assert "seasonal_naive_cal" in doc["test_arima_subset"]
    assert "arima" in doc["test_arima_subset"]

    assert "seasonal_naive" in doc["offsets"]
    assert "last_window" in doc["offsets"]

    for k in ("mean", "p5", "p95", "uniform"):
        assert k in doc["attention_entropy"]

    assert isinstance(doc["n_windows"], int) and doc["n_windows"] > 0
    assert isinstance(doc["arima_failures"], int)
    assert doc["epochs_run"] == 1
    assert isinstance(doc["val_pinball"], float)

    # Attention weights: 48 floats summing to 1
    assert len(doc["mean_attention"]) == 48
    assert pytest.approx(sum(doc["mean_attention"]), 1e-3) == 1.0

    # Example structure
    ex = doc["example"]
    assert len(ex["history"]) == 48
    assert len(ex["actual"]) == 12
    assert len(ex["lstm"]) == 12
    assert len(ex["last_window"]) == 12

    # Verify model saving and loading with weights_only=True
    model_path = tmp_path / "models" / "forecast.pt"
    assert model_path.exists()
    loaded = torch.load(model_path, weights_only=True)
    assert "state_dict" in loaded
    assert loaded["hidden"] == 64
    assert loaded["t_in"] == 48
    assert loaded["t_out"] == 12

    # Instantiate and predict
    loaded_model = AttnLSTM(hidden=loaded["hidden"], t_in=loaded["t_in"], t_out=loaded["t_out"])
    loaded_model.load_state_dict(loaded["state_dict"])
    loaded_model.eval()

    pred = predict_q95(loaded_model, np.array(ex["history"], dtype=np.float32))
    assert pred.shape == (12,)

    # predict_q95 with path
    pred_path = predict_q95(model_path, np.array(ex["history"], dtype=np.float32))
    assert pred_path.shape == (12,)
    assert np.allclose(pred, pred_path)


def test_synthetic_invariants():
    data1 = synthetic(V=5, T=100, seed=42)
    data2 = synthetic(V=5, T=100, seed=42)
    data3 = synthetic(V=5, T=100, seed=43)

    for k in ("cpu_min", "cpu_avg", "cpu_max"):
        assert np.array_equal(data1[k], data2[k])
        assert not np.array_equal(data1[k], data3[k])
        assert (data1[k] >= 0.0).all()
        assert (data1[k] <= 100.0).all()
        assert data1[k].shape == (5, 100)
        assert data1[k].dtype == np.float32

    # Ordering invariant: cpu_max >= cpu_avg >= cpu_min
    assert (data1["cpu_max"] >= data1["cpu_avg"]).all()
    assert (data1["cpu_avg"] >= data1["cpu_min"]).all()


def test_seasonal_base_toy_period_288():
    period = 288
    T = 600
    rng = np.random.default_rng(123)
    base_pattern = rng.uniform(10.0, 80.0, size=period).astype(np.float32)
    series = np.tile(base_pattern, int(np.ceil(T / period)))[:T]

    for t in [288, 300, 450, 580]:
        base = seasonal_base(series, t=t, t_out=12, period=period)
        assert len(base) == 12
        # On a toy series with period 288, the seasonal base for target step t equals the value at t-288
        assert base[0] == series[t - 288]
        for j in range(12):
            assert base[j] == series[t + j - 288]


def test_offset_calibration_held_out_coverage():
    rng = np.random.default_rng(42)
    n_val = 50000
    val_res = rng.normal(loc=0.0, scale=0.1, size=n_val)
    val_base = np.zeros(n_val, dtype=np.float64)
    val_y = val_res.copy()

    offset = fit_offset(val_base, val_y, tau=0.95)

    n_test = 50000
    test_res = rng.normal(loc=0.0, scale=0.1, size=n_test)
    test_base = np.zeros(n_test, dtype=np.float64)
    test_y = test_res.copy()

    test_pred_cal = test_base + offset
    coverage = float(np.mean(test_y <= test_pred_cal))

    # Calibrated coverage on held-out residuals is within 0.03 of 0.95
    assert abs(coverage - 0.95) <= 0.03


def test_plain_lstm_output_shape():
    B = 7
    model = PlainLSTM(hidden=64, t_in=48, t_out=12)
    x = torch.randn(B, 48, 5)
    d = torch.randn(B, 12, 2)
    q, attn = model(x, d)
    # PlainLSTM output shape [B, 12]
    assert q.shape == (B, 12)
    assert attn is None


def test_attention_entropy_uniform():
    uniform_val = np.log(48)
    attn = np.full((16, 12, 48), 1.0 / 48.0, dtype=np.float64)
    stats = compute_attention_entropy(attn, t_in=48)

    # Entropy of uniform weights equals ln(48)
    assert pytest.approx(stats["uniform"], 1e-6) == uniform_val
    assert pytest.approx(stats["mean"], 1e-6) == uniform_val
    assert pytest.approx(stats["p5"], 1e-6) == uniform_val
    assert pytest.approx(stats["p95"], 1e-6) == uniform_val


def test_no_calibration_uses_test_windows():
    data = synthetic(V=12, T=500, seed=0)
    X, D, Y, t_end = windows(data)
    train_idx, val_idx, test_idx = time_split(t_end, T=500)
    seasonal_all, has_seasonal = seasonal_base(data)

    p95_val = np.percentile(X[val_idx, :, 2], 95, axis=1, keepdims=True)
    base_last_val = np.repeat(p95_val, 12, axis=1)
    offset_last_orig = fit_offset(base_last_val, Y[val_idx], tau=0.95)

    val_seasonal_idx = val_idx[has_seasonal[val_idx]]
    offset_seasonal_orig = fit_offset(seasonal_all[val_seasonal_idx], Y[val_seasonal_idx], tau=0.95)

    # Perturb test region (t >= 400)
    data_perturbed = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in data.items()}
    data_perturbed["cpu_max"][:, 400:] += 50.0

    X_p, D_p, Y_p, t_end_p = windows(data_perturbed)
    train_idx_p, val_idx_p, test_idx_p = time_split(t_end_p, T=500)
    seasonal_all_p, has_seasonal_p = seasonal_base(data_perturbed)

    p95_val_p = np.percentile(X_p[val_idx_p, :, 2], 95, axis=1, keepdims=True)
    base_last_val_p = np.repeat(p95_val_p, 12, axis=1)
    offset_last_pert = fit_offset(base_last_val_p, Y_p[val_idx_p], tau=0.95)

    val_seasonal_idx_p = val_idx_p[has_seasonal_p[val_idx_p]]
    offset_seasonal_pert = fit_offset(seasonal_all_p[val_seasonal_idx_p], Y_p[val_seasonal_idx_p], tau=0.95)

    # Assert offsets are completely unchanged when test targets are perturbed
    assert offset_last_orig == offset_last_pert
    assert offset_seasonal_orig == offset_seasonal_pert

