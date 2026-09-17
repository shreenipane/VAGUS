"""Attention-LSTM P95 forecaster on bursty workloads per TRD §8."""

from __future__ import annotations

import json
from pathlib import Path
import warnings

import numpy as np
from statsmodels.tsa.arima.model import ARIMA
import torch
import torch.nn as nn
import torch.nn.functional as F

from irm import HOME


def synthetic(V: int, T: int, seed: int = 0) -> dict:
    """Generate synthetic VM CPU time series with diurnal cycle, noise, and bursts.

    Returns float32 arrays [V, T] in percent (0-100) with cpu_max >= cpu_avg >= cpu_min.
    Deterministic for a given seed.
    """
    rng = np.random.default_rng(seed)

    base = rng.uniform(5.0, 40.0, size=(V, 1)).astype(np.float32)
    amp = rng.uniform(5.0, 25.0, size=(V, 1)).astype(np.float32)
    phase = rng.uniform(0.0, 2.0 * np.pi, size=(V, 1)).astype(np.float32)

    t = np.arange(T, dtype=np.float32)
    # 5-minute steps -> 288 steps in a 24-hour day
    sinusoid = amp * np.sin(2.0 * np.pi * t / 288.0 + phase).astype(np.float32)
    noise = rng.normal(0.0, 2.0, size=(V, T)).astype(np.float32)

    # Heavy-tailed bursts: onsets with p=0.01 per step, height Pareto(1.5)*10, lasting 1-6 steps
    burst = np.zeros((V, T), dtype=np.float32)
    onsets = rng.random(size=(V, T)) < 0.01
    v_idx, t_idx = np.where(onsets)
    if len(v_idx) > 0:
        heights = ((rng.pareto(1.5, size=len(v_idx)) + 1.0) * 10.0).astype(np.float32)
        durations = rng.integers(1, 7, size=len(v_idx))
        for vi, ti, h, d in zip(v_idx, t_idx, heights, durations):
            burst[vi, ti : min(T, ti + d)] += h

    sig = base + sinusoid + noise + burst

    spread_max = np.abs(rng.normal(0.0, 2.0, size=(V, T))).astype(np.float32)
    spread_min = np.abs(rng.normal(0.0, 2.0, size=(V, T))).astype(np.float32)

    cpu_max = np.clip(sig + spread_max, 0.0, 100.0).astype(np.float32)
    cpu_min = np.clip(sig - spread_min, 0.0, 100.0).astype(np.float32)
    cpu_avg = np.clip(sig, cpu_min, cpu_max).astype(np.float32)

    return {
        "cpu_min": cpu_min,
        "cpu_avg": cpu_avg,
        "cpu_max": cpu_max,
        "t_start": 0,
        "step": 300,
        "burst": burst,
    }


def windows(
    data: dict,
    t_in: int = 48,
    t_out: int = 12,
    stride: int = 3,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Slice time series into encoder/decoder windows per TRD §8.

    Inputs X: [N, 48, 5] (min/100, avg/100, max/100, sin_tod, cos_tod)
    Decoder D: [N, 12, 2] (sin_tod, cos_tod)
    Target Y: [N, 12] (max/100)
    t_end: [N] (last target step index)
    Valid only when all 60 cpu_* values are finite.
    """
    cpu_min = np.asarray(data["cpu_min"], dtype=np.float32)
    cpu_avg = np.asarray(data["cpu_avg"], dtype=np.float32)
    cpu_max = np.asarray(data["cpu_max"], dtype=np.float32)
    t_start = int(data.get("t_start", 0))
    step = int(data.get("step", 300))

    V, T = cpu_min.shape
    W = t_in + t_out
    if T < W:
        return (
            np.empty((0, t_in, 5), dtype=np.float32),
            np.empty((0, t_out, 2), dtype=np.float32),
            np.empty((0, t_out), dtype=np.float32),
            np.empty((0,), dtype=np.int64),
        )

    start_indices = np.arange(0, T - W + 1, stride, dtype=np.int64)

    # Time of day (seconds in day [0, 86400))
    t_sec = t_start + np.arange(T, dtype=np.float64) * step
    tod = t_sec % 86400.0
    sin_tod = np.sin(2.0 * np.pi * tod / 86400.0).astype(np.float32)
    cos_tod = np.cos(2.0 * np.pi * tod / 86400.0).astype(np.float32)

    # Sliding windows: [V, K, W]
    sw_min = np.lib.stride_tricks.sliding_window_view(cpu_min, W, axis=1)[:, ::stride, :]
    sw_avg = np.lib.stride_tricks.sliding_window_view(cpu_avg, W, axis=1)[:, ::stride, :]
    sw_max = np.lib.stride_tricks.sliding_window_view(cpu_max, W, axis=1)[:, ::stride, :]

    valid = (
        np.isfinite(sw_min).all(axis=-1)
        & np.isfinite(sw_avg).all(axis=-1)
        & np.isfinite(sw_max).all(axis=-1)
    )

    v_idx, k_idx = np.where(valid)
    N = len(v_idx)
    if N == 0:
        return (
            np.empty((0, t_in, 5), dtype=np.float32),
            np.empty((0, t_out, 2), dtype=np.float32),
            np.empty((0, t_out), dtype=np.float32),
            np.empty((0,), dtype=np.int64),
        )

    sw_sin = np.lib.stride_tricks.sliding_window_view(sin_tod, W)[::stride, :]
    sw_cos = np.lib.stride_tricks.sliding_window_view(cos_tod, W)[::stride, :]

    X = np.empty((N, t_in, 5), dtype=np.float32)
    X[:, :, 0] = sw_min[v_idx, k_idx, :t_in] / 100.0
    X[:, :, 1] = sw_avg[v_idx, k_idx, :t_in] / 100.0
    X[:, :, 2] = sw_max[v_idx, k_idx, :t_in] / 100.0
    X[:, :, 3] = sw_sin[k_idx, :t_in]
    X[:, :, 4] = sw_cos[k_idx, :t_in]

    D = np.empty((N, t_out, 2), dtype=np.float32)
    D[:, :, 0] = sw_sin[k_idx, t_in:]
    D[:, :, 1] = sw_cos[k_idx, t_in:]

    Y = (sw_max[v_idx, k_idx, t_in:] / 100.0).astype(np.float32)
    t_end = (start_indices[k_idx] + W - 1).astype(np.int64)

    return X, D, Y, t_end


def time_split(
    t_end: np.ndarray,
    T: int | None = None,
    t_in: int = 48,
    t_out: int = 12,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split windows into train, validation, and test indices by timestamp.

    - Test: windows whose first target step is in the last 20% of steps.
    - Remaining pre-test windows have last target step before test period.
    - Validation: the last 10% of time before test period.
    - Train: the rest (targets strictly end before test period starts).
    """
    if len(t_end) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.array([], dtype=np.int64)

    if T is None:
        T = int(np.max(t_end)) + 1

    t_split = int(0.8 * T)
    first_target = t_end - t_out + 1

    test_mask = first_target >= t_split
    pre_test = t_end < t_split

    t_val_start = int(t_split - 0.1 * t_split)
    val_mask = pre_test & (t_end >= t_val_start)
    train_mask = pre_test & (t_end < t_val_start)

    return (
        np.where(train_mask)[0],
        np.where(val_mask)[0],
        np.where(test_mask)[0],
    )


def split_windows(
    X: np.ndarray,
    D: np.ndarray,
    Y: np.ndarray,
    t_end: np.ndarray,
    T: int | None = None,
    t_in: int = 48,
    t_out: int = 12,
) -> tuple[tuple[np.ndarray, ...], tuple[np.ndarray, ...], tuple[np.ndarray, ...]]:
    """Partition windows (X, D, Y, t_end) into train, val, test subsets."""
    tr_idx, va_idx, te_idx = time_split(t_end, T=T, t_in=t_in, t_out=t_out)
    return (
        (X[tr_idx], D[tr_idx], Y[tr_idx], t_end[tr_idx]),
        (X[va_idx], D[va_idx], Y[va_idx], t_end[va_idx]),
        (X[te_idx], D[te_idx], Y[te_idx], t_end[te_idx]),
    )


class AttnLSTM(nn.Module):
    """Encoder-decoder LSTM with additive Bahdanau attention per TRD §8."""

    def __init__(self, hidden: int = 64, t_in: int = 48, t_out: int = 12):
        super().__init__()
        self.hidden = hidden
        self.t_in = t_in
        self.t_out = t_out

        self.encoder = nn.LSTM(input_size=5, hidden_size=hidden, batch_first=True)
        self.decoder_cell = nn.LSTMCell(input_size=2 + hidden, hidden_size=hidden)
        self.W_a = nn.Linear(hidden, hidden, bias=False)
        self.fc1 = nn.Linear(hidden + hidden, hidden)
        self.fc2 = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor, d: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        B = x.size(0)
        enc_out, (h_n, c_n) = self.encoder(x)
        h = h_n[-1]
        c = c_n[-1]

        Wa_e = self.W_a(enc_out)  # [B, t_in, hidden]
        ctx = torch.zeros(B, self.hidden, device=x.device, dtype=x.dtype)

        q_list = []
        attn_list = []
        t_out = d.size(1) if d is not None else self.t_out

        for j in range(t_out):
            cell_in = torch.cat([d[:, j, :], ctx], dim=-1)
            h, c = self.decoder_cell(cell_in, (h, c))

            scores = torch.bmm(Wa_e, h.unsqueeze(2)).squeeze(2)  # [B, t_in]
            alpha = F.softmax(scores, dim=-1)  # [B, t_in]
            ctx = torch.bmm(alpha.unsqueeze(1), enc_out).squeeze(1)  # [B, hidden]

            out_in = torch.cat([h, ctx], dim=-1)
            q_j = self.fc2(torch.tanh(self.fc1(out_in))).squeeze(-1)

            q_list.append(q_j)
            attn_list.append(alpha)

        q = torch.stack(q_list, dim=1)
        attn = torch.stack(attn_list, dim=1)
        return q, attn


def pinball(q, y, tau: float = 0.95):
    """Pinball loss for quantile tau: mean(max(tau*e, (tau-1)*e)) where e = y - q."""
    if isinstance(q, torch.Tensor):
        if not isinstance(y, torch.Tensor):
            y = torch.as_tensor(y, dtype=q.dtype, device=q.device)
        e = y - q
        loss = torch.maximum(tau * e, (tau - 1.0) * e)
        return loss.mean()
    else:
        q_arr = np.asarray(q, dtype=np.float64)
        y_arr = np.asarray(y, dtype=np.float64)
        e = y_arr - q_arr
        loss = np.maximum(tau * e, (tau - 1.0) * e)
        return float(loss.mean())


def metrics(q, y) -> dict[str, float]:
    """Evaluation metrics: pinball, coverage, mean_under, mean_over."""
    if isinstance(q, torch.Tensor):
        q_arr = q.detach().cpu().numpy().astype(np.float64)
    else:
        q_arr = np.asarray(q, dtype=np.float64)

    if isinstance(y, torch.Tensor):
        y_arr = y.detach().cpu().numpy().astype(np.float64)
    else:
        y_arr = np.asarray(y, dtype=np.float64)

    e = y_arr - q_arr
    pb = float(np.mean(np.maximum(0.95 * e, -0.05 * e)))
    cov = float(np.mean(y_arr <= q_arr))
    under = float(np.mean(np.maximum(0.0, e)))
    over = float(np.mean(np.maximum(0.0, -e)))

    return {
        "pinball": pb,
        "coverage": cov,
        "mean_under": under,
        "mean_over": over,
    }


def predict_q95(model, x, dec=None) -> np.ndarray:
    """Inference helper predicting 95th percentile CPU util for next 12 steps."""
    if isinstance(model, (str, Path)):
        ckpt = torch.load(model, weights_only=True)
        m = AttnLSTM(hidden=ckpt.get("hidden", 64), t_in=ckpt.get("t_in", 48), t_out=ckpt.get("t_out", 12))
        m.load_state_dict(ckpt["state_dict"])
        model = m

    model.eval()

    if isinstance(x, torch.Tensor):
        x_arr = x.detach().cpu().numpy()
    else:
        x_arr = np.asarray(x, dtype=np.float32)

    is_1d = False
    if x_arr.ndim == 1:
        is_1d = True
        x_arr = x_arr[np.newaxis, :, np.newaxis]
        x_5 = np.zeros((1, x_arr.shape[1], 5), dtype=np.float32)
        x_5[:, :, :3] = x_arr
        x_arr = x_5
    elif x_arr.ndim == 2:
        if x_arr.shape[1] == 5:
            x_arr = x_arr[np.newaxis, :, :]
            is_1d = True
        else:
            B = x_arr.shape[0]
            x_5 = np.zeros((B, x_arr.shape[1], 5), dtype=np.float32)
            x_5[:, :, :3] = x_arr[:, :, np.newaxis]
            x_arr = x_5

    B = x_arr.shape[0]
    if dec is None:
        dec_arr = np.zeros((B, 12, 2), dtype=np.float32)
        dec_arr[:, :, 1] = 1.0
    else:
        if isinstance(dec, torch.Tensor):
            dec_arr = dec.detach().cpu().numpy()
        else:
            dec_arr = np.asarray(dec, dtype=np.float32)
        if dec_arr.ndim == 2:
            dec_arr = np.tile(dec_arr[np.newaxis, :, :], (B, 1, 1))

    device = next(model.parameters()).device
    with torch.no_grad():
        x_t = torch.as_tensor(x_arr, dtype=torch.float32, device=device)
        d_t = torch.as_tensor(dec_arr, dtype=torch.float32, device=device)
        q_t, _ = model(x_t, d_t)
        res = q_t.cpu().numpy()

    return res[0] if is_1d else res


def demo(
    out_json: str | Path,
    seed: int = 0,
    epochs: int = 5,
    V: int = 300,
    T: int = 2016,
    model_path: str | Path | None = None,
    arima_windows: int = 100,
) -> dict:
    """Train and evaluate the forecaster prototype, writing JSON and models/forecast.pt."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)

    data = synthetic(V, T, seed)
    X, D, Y, t_end = windows(data)

    train_idx, val_idx, test_idx = time_split(t_end, T=T)

    # Subsample training windows if more than 60,000
    if len(train_idx) > 60000:
        train_idx = rng.choice(train_idx, size=60000, replace=False)

    model = AttnLSTM(hidden=64)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)

    best_val_loss = float("inf")
    best_state_dict = None
    patience = 2
    patience_counter = 0
    epochs_run = 0

    batch_size = 512
    n_train = len(train_idx)

    for epoch in range(1, epochs + 1):
        epochs_run += 1
        model.train()
        train_perm = rng.permutation(train_idx)
        train_losses = []

        for i in range(0, n_train, batch_size):
            batch_indices = train_perm[i : i + batch_size]
            xb = torch.from_numpy(X[batch_indices])
            db = torch.from_numpy(D[batch_indices])
            yb = torch.from_numpy(Y[batch_indices])

            optimizer.zero_grad()
            qb, _ = model(xb, db)
            loss = pinball(qb, yb, tau=0.95)
            loss.backward()
            optimizer.step()
            train_losses.append(loss.item() * len(batch_indices))

        train_loss = sum(train_losses) / max(1, n_train)

        # Validation
        model.eval()
        val_losses = []
        with torch.no_grad():
            for i in range(0, len(val_idx), 1024):
                val_b = val_idx[i : i + 1024]
                xb = torch.from_numpy(X[val_b])
                db = torch.from_numpy(D[val_b])
                yb = torch.from_numpy(Y[val_b])
                qb, _ = model(xb, db)
                val_losses.append(pinball(qb, yb, tau=0.95).item() * len(val_b))

        val_loss = sum(val_losses) / max(1, len(val_idx))
        print(f"Epoch {epoch}/{epochs} - train_pinball: {train_loss:.4f} - val_pinball: {val_loss:.4f}")

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state_dict = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= patience:
                break

    if best_state_dict is not None:
        model.load_state_dict(best_state_dict)
    else:
        best_state_dict = {k: v.cpu().clone() for k, v in model.state_dict().items()}
        best_val_loss = val_loss

    # Save model
    out_p = Path(out_json)
    if model_path is None:
        if out_p.parent.name == "reports":
            model_path = out_p.parent.parent / "models" / "forecast.pt"
        else:
            model_path = out_p.parent / "models" / "forecast.pt"
    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": best_state_dict,
            "hidden": 64,
            "t_in": 48,
            "t_out": 12,
        },
        model_path,
    )

    # Evaluate on all test windows
    model.eval()
    q_lstm_list = []
    attn_list = []
    with torch.no_grad():
        for i in range(0, len(test_idx), 1024):
            tb = test_idx[i : i + 1024]
            xb = torch.from_numpy(X[tb])
            db = torch.from_numpy(D[tb])
            qb, attnb = model(xb, db)
            q_lstm_list.append(qb.cpu().numpy())
            attn_list.append(attnb.cpu().numpy())

    if len(q_lstm_list) > 0:
        q_lstm_all = np.concatenate(q_lstm_list, axis=0)
        attn_all = np.concatenate(attn_list, axis=0)
        mean_attention = attn_all.mean(axis=(0, 1)).tolist()
    else:
        q_lstm_all = np.empty((0, 12), dtype=np.float32)
        mean_attention = [0.0] * 48

    # Last-window P95 baseline for all test windows
    if len(test_idx) > 0:
        p95_inputs = np.percentile(X[test_idx, :, 2], 95, axis=1, keepdims=True)
        q_last_all = np.repeat(p95_inputs, 12, axis=1)
    else:
        q_last_all = np.empty((0, 12), dtype=np.float32)

    test_all = {
        "lstm": metrics(q_lstm_all, Y[test_idx]),
        "last_window": metrics(q_last_all, Y[test_idx]),
    }

    # ARIMA baseline on seeded test subset
    n_arima = min(arima_windows, len(test_idx))
    arima_failures = 0

    if n_arima > 0:
        sub_indices = rng.choice(len(test_idx), size=n_arima, replace=False)
        sub_indices.sort()
        q_arima_sub = np.empty((n_arima, 12), dtype=np.float32)

        for i, k in enumerate(sub_indices):
            w_idx = test_idx[k]
            series = X[w_idx, :, 2]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    ar_model = ARIMA(series, order=(2, 0, 1))
                    ar_res = ar_model.fit()
                    fc = ar_res.get_forecast(steps=12)
                    ci = fc.conf_int(alpha=0.10)
                    if hasattr(ci, "iloc"):
                        q95 = ci.iloc[:, 1].to_numpy()
                    elif hasattr(ci, "values"):
                        q95 = ci.values[:, 1]
                    else:
                        q95 = np.asarray(ci)[:, 1]
                    if np.isnan(q95).any() or np.isinf(q95).any():
                        raise ValueError("ARIMA produced NaN/inf")
                    q_arima_sub[i] = q95
                except Exception:
                    arima_failures += 1
                    q_arima_sub[i] = q_last_all[k]

        y_sub = Y[test_idx[sub_indices]]
        test_arima_subset = {
            "lstm": metrics(q_lstm_all[sub_indices], y_sub),
            "last_window": metrics(q_last_all[sub_indices], y_sub),
            "arima": metrics(q_arima_sub, y_sub),
        }
    else:
        test_arima_subset = {
            "lstm": metrics(q_lstm_all, Y[test_idx]),
            "last_window": metrics(q_last_all, Y[test_idx]),
            "arima": metrics(q_last_all, Y[test_idx]),
        }

    # Find an example test window containing a burst in its target if possible
    example_k = 0
    if len(test_idx) > 0:
        found = False
        for k in range(len(test_idx)):
            w = test_idx[k]
            # Burst indicator: target peak substantially exceeds history mean
            if np.max(Y[w]) - np.mean(X[w, :, 2]) > 0.20:
                example_k = k
                found = True
                break
        if not found:
            spikes = [np.max(Y[test_idx[k]]) - np.mean(X[test_idx[k], :, 2]) for k in range(len(test_idx))]
            example_k = int(np.argmax(spikes))

        ex_w = test_idx[example_k]
        example = {
            "history": [float(v) for v in X[ex_w, :, 2]],
            "actual": [float(v) for v in Y[ex_w]],
            "lstm": [float(v) for v in q_lstm_all[example_k]],
            "last_window": [float(v) for v in q_last_all[example_k]],
        }
    else:
        example = {
            "history": [0.0] * 48,
            "actual": [0.0] * 12,
            "lstm": [0.0] * 12,
            "last_window": [0.0] * 12,
        }

    report = {
        "test_all": test_all,
        "test_arima_subset": test_arima_subset,
        "n_windows": int(len(X)),
        "arima_failures": int(arima_failures),
        "epochs_run": int(epochs_run),
        "val_pinball": float(best_val_loss),
        "mean_attention": [float(v) for v in mean_attention],
        "example": example,
    }

    out_p = Path(out_json)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report
