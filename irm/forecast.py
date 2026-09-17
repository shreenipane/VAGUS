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
    """Encoder-decoder LSTM with Luong multiplicative attention per TRD §8."""

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


class PlainLSTM(nn.Module):
    """Encoder-decoder LSTM without attention (ablation)."""

    def __init__(self, hidden: int = 64, t_in: int = 48, t_out: int = 12):
        super().__init__()
        self.hidden = hidden
        self.t_in = t_in
        self.t_out = t_out

        self.encoder = nn.LSTM(input_size=5, hidden_size=hidden, batch_first=True)
        self.decoder_cell = nn.LSTMCell(input_size=2, hidden_size=hidden)
        self.fc1 = nn.Linear(hidden, hidden)
        self.fc2 = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor, d: torch.Tensor) -> tuple[torch.Tensor, None]:
        B = x.size(0)
        enc_out, (h_n, c_n) = self.encoder(x)
        h = h_n[-1]
        c = c_n[-1]

        q_list = []
        t_out = d.size(1) if d is not None else self.t_out

        for j in range(t_out):
            cell_in = d[:, j, :]
            h, c = self.decoder_cell(cell_in, (h, c))
            q_j = self.fc2(torch.tanh(self.fc1(h))).squeeze(-1)
            q_list.append(q_j)

        q = torch.stack(q_list, dim=1)
        return q, None


def seasonal_base(
    series_or_data: np.ndarray | dict,
    t: int | None = None,
    t_out: int = 12,
    period: int = 288,
    stride: int = 3,
    t_in: int = 48,
    v: int = 0,
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Return seasonal base forecast.

    - 1D array: returns series[t - period : t - period + t_out] for target step t.
    - dict (data): returns (base_all [N, t_out], has_history [N]) matching windows(data).
    """
    if isinstance(series_or_data, dict):
        cpu_max = np.asarray(series_or_data["cpu_max"], dtype=np.float32)
        cpu_min = np.asarray(series_or_data["cpu_min"], dtype=np.float32)
        cpu_avg = np.asarray(series_or_data["cpu_avg"], dtype=np.float32)
        V, T = cpu_max.shape
        W = t_in + t_out
        if T < W:
            return np.empty((0, t_out), dtype=np.float32), np.empty((0,), dtype=bool)

        start_indices = np.arange(0, T - W + 1, stride, dtype=np.int64)
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
            return np.empty((0, t_out), dtype=np.float32), np.empty((0,), dtype=bool)

        t_first = start_indices[k_idx] + t_in
        has_history = t_first >= period

        seasonal = np.zeros((N, t_out), dtype=np.float32)
        valid_pos = np.where(has_history)[0]
        if len(valid_pos) > 0:
            sw_seasonal = np.lib.stride_tricks.sliding_window_view(cpu_max / 100.0, t_out, axis=1)
            t0 = t_first[valid_pos] - period
            v_sel = v_idx[valid_pos]
            seasonal[valid_pos] = sw_seasonal[v_sel, t0]

        return seasonal, has_history
    else:
        arr = np.asarray(series_or_data, dtype=np.float32)
        if arr.ndim == 2:
            arr = arr[v]
        if t is None:
            raise ValueError("Target step t must be specified for a series array")
        if t < period:
            raise ValueError(f"Target step {t} has no seasonal history with period {period}")
        return arr[t - period : t - period + t_out]


def fit_offset(base: np.ndarray, y: np.ndarray, tau: float = 0.95) -> float:
    """Fit a scalar calibration offset as the tau-quantile of (y - base) over all target steps."""
    residuals = (y - base).ravel()
    if len(residuals) == 0:
        return 0.0
    return float(np.quantile(residuals, tau))


def compute_attention_entropy(attn: np.ndarray | torch.Tensor, t_in: int = 48) -> dict[str, float]:
    """Compute per-sample attention entropy averaged across decoder steps."""
    uniform_val = float(np.log(t_in))
    if isinstance(attn, torch.Tensor):
        attn_arr = attn.detach().cpu().numpy()
    else:
        attn_arr = np.asarray(attn, dtype=np.float64)

    if len(attn_arr) == 0:
        return {
            "mean": uniform_val,
            "p5": uniform_val,
            "p95": uniform_val,
            "uniform": uniform_val,
        }

    eps = 1e-12
    attn_safe = np.clip(attn_arr, eps, 1.0)
    h_step = -np.sum(attn_arr * np.log(attn_safe), axis=-1)  # [N, t_out]
    h_sample = np.mean(h_step, axis=1)  # [N]

    return {
        "mean": float(np.mean(h_sample)),
        "p5": float(np.percentile(h_sample, 5)),
        "p95": float(np.percentile(h_sample, 95)),
        "uniform": uniform_val,
    }


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
    data = synthetic(V, T, seed)
    X, D, Y, t_end = windows(data)

    train_idx, val_idx, test_idx = time_split(t_end, T=T)

    # Subsample training windows if more than 60,000
    rng = np.random.default_rng(seed)
    if len(train_idx) > 60000:
        train_idx = rng.choice(train_idx, size=60000, replace=False)

    batch_size = 512
    n_train = len(train_idx)

    def _train(model: nn.Module) -> tuple[nn.Module, float, int]:
        torch.manual_seed(seed)
        model_rng = np.random.default_rng(seed)
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)

        best_val = float("inf")
        best_sd = None
        patience = 2
        patience_ctr = 0
        ep_run = 0

        for epoch in range(1, epochs + 1):
            ep_run += 1
            model.train()
            perm = model_rng.permutation(train_idx)
            train_losses = []

            for i in range(0, n_train, batch_size):
                b_idx = perm[i : i + batch_size]
                xb = torch.from_numpy(X[b_idx])
                db = torch.from_numpy(D[b_idx])
                yb = torch.from_numpy(Y[b_idx])

                optimizer.zero_grad()
                qb, _ = model(xb, db)
                loss = pinball(qb, yb, tau=0.95)
                loss.backward()
                optimizer.step()
                train_losses.append(loss.item() * len(b_idx))

            train_loss = sum(train_losses) / max(1, n_train)

            # Validation
            model.eval()
            val_losses = []
            with torch.no_grad():
                for i in range(0, len(val_idx), 1024):
                    vb = val_idx[i : i + 1024]
                    xb = torch.from_numpy(X[vb])
                    db = torch.from_numpy(D[vb])
                    yb = torch.from_numpy(Y[vb])
                    qb, _ = model(xb, db)
                    val_losses.append(pinball(qb, yb, tau=0.95).item() * len(vb))

            val_loss = sum(val_losses) / max(1, len(val_idx))
            print(f"[{model.__class__.__name__}] Epoch {epoch}/{epochs} - train_pinball: {train_loss:.4f} - val_pinball: {val_loss:.4f}")

            if val_loss < best_val:
                best_val = val_loss
                best_sd = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                patience_ctr = 0
            else:
                patience_ctr += 1
                if patience_ctr >= patience:
                    break

        if best_sd is not None:
            model.load_state_dict(best_sd)
        else:
            best_sd = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            best_val = val_loss

        return model, best_val, ep_run

    # 1. Train Attention LSTM
    attn_model, best_val_loss, epochs_run = _train(AttnLSTM(hidden=64))

    # 2. Train Plain LSTM (ablation without attention)
    plain_model, _, _ = _train(PlainLSTM(hidden=64))

    # Save attention model checkpoint
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
            "state_dict": {k: v.cpu().clone() for k, v in attn_model.state_dict().items()},
            "hidden": 64,
            "t_in": 48,
            "t_out": 12,
        },
        model_path,
    )

    # Evaluate Attention LSTM on all test windows
    attn_model.eval()
    q_lstm_list = []
    attn_list = []
    with torch.no_grad():
        for i in range(0, len(test_idx), 1024):
            tb = test_idx[i : i + 1024]
            xb = torch.from_numpy(X[tb])
            db = torch.from_numpy(D[tb])
            qb, attnb = attn_model(xb, db)
            q_lstm_list.append(qb.cpu().numpy())
            attn_list.append(attnb.cpu().numpy())

    if len(q_lstm_list) > 0:
        q_lstm_all = np.concatenate(q_lstm_list, axis=0)
        attn_all = np.concatenate(attn_list, axis=0)
        mean_attention = attn_all.mean(axis=(0, 1)).tolist()
    else:
        q_lstm_all = np.empty((0, 12), dtype=np.float32)
        attn_all = np.empty((0, 12, 48), dtype=np.float32)
        mean_attention = [0.0] * 48

    # Evaluate Plain LSTM on all test windows
    plain_model.eval()
    q_plain_list = []
    with torch.no_grad():
        for i in range(0, len(test_idx), 1024):
            tb = test_idx[i : i + 1024]
            xb = torch.from_numpy(X[tb])
            db = torch.from_numpy(D[tb])
            qb, _ = plain_model(xb, db)
            q_plain_list.append(qb.cpu().numpy())

    if len(q_plain_list) > 0:
        q_plain_all = np.concatenate(q_plain_list, axis=0)
    else:
        q_plain_all = np.empty((0, 12), dtype=np.float32)

    # Attention statistics
    attention_entropy = compute_attention_entropy(attn_all)

    # Seasonal-naive base for all windows
    seasonal_all, has_seasonal = seasonal_base(data)

    # Fit calibration offsets on VALIDATION windows only (never test)
    if len(val_idx) > 0:
        p95_val = np.percentile(X[val_idx, :, 2], 95, axis=1, keepdims=True)
        base_last_val = np.repeat(p95_val, 12, axis=1)
        offset_last = fit_offset(base_last_val, Y[val_idx], tau=0.95)
    else:
        offset_last = 0.0

    val_seasonal_mask = has_seasonal[val_idx]
    val_seasonal_idx = val_idx[val_seasonal_mask]
    if len(val_seasonal_idx) > 0:
        offset_seasonal = fit_offset(seasonal_all[val_seasonal_idx], Y[val_seasonal_idx], tau=0.95)
    else:
        offset_seasonal = 0.0

    offsets = {
        "seasonal_naive": float(offset_seasonal),
        "last_window": float(offset_last),
    }

    # Last-window baseline for all test windows
    if len(test_idx) > 0:
        p95_inputs = np.percentile(X[test_idx, :, 2], 95, axis=1, keepdims=True)
        q_last_all = np.repeat(p95_inputs, 12, axis=1)
        q_last_cal_all = q_last_all + offset_last
    else:
        q_last_all = np.empty((0, 12), dtype=np.float32)
        q_last_cal_all = np.empty((0, 12), dtype=np.float32)

    test_all = {
        "lstm": metrics(q_lstm_all, Y[test_idx]),
        "lstm_noattn": metrics(q_plain_all, Y[test_idx]),
        "last_window": metrics(q_last_all, Y[test_idx]),
        "last_window_cal": metrics(q_last_cal_all, Y[test_idx]),
    }

    # Seasonal subset of test windows (windows with seasonal history)
    test_seasonal_mask = has_seasonal[test_idx]
    test_seasonal_pos = np.where(test_seasonal_mask)[0]  # position within test_idx
    test_seasonal_idx = test_idx[test_seasonal_mask]  # window indices
    n_seasonal_windows = len(test_seasonal_idx)

    if n_seasonal_windows > 0:
        y_seasonal = Y[test_seasonal_idx]
        q_seasonal_sub = seasonal_all[test_seasonal_idx]
        q_seasonal_cal_sub = q_seasonal_sub + offset_seasonal
        test_seasonal_subset = {
            "lstm": metrics(q_lstm_all[test_seasonal_pos], y_seasonal),
            "lstm_noattn": metrics(q_plain_all[test_seasonal_pos], y_seasonal),
            "seasonal_naive": metrics(q_seasonal_sub, y_seasonal),
            "seasonal_naive_cal": metrics(q_seasonal_cal_sub, y_seasonal),
            "last_window": metrics(q_last_all[test_seasonal_pos], y_seasonal),
            "last_window_cal": metrics(q_last_cal_all[test_seasonal_pos], y_seasonal),
            "n_windows": int(n_seasonal_windows),
        }
    else:
        empty_m = {"pinball": 0.0, "coverage": 0.0, "mean_under": 0.0, "mean_over": 0.0}
        test_seasonal_subset = {
            "lstm": empty_m,
            "lstm_noattn": empty_m,
            "seasonal_naive": empty_m,
            "seasonal_naive_cal": empty_m,
            "last_window": empty_m,
            "last_window_cal": empty_m,
            "n_windows": 0,
        }

    # ARIMA baseline on seeded test subset chosen from windows that have seasonal history
    n_arima = min(arima_windows, n_seasonal_windows)
    arima_failures = 0

    if n_arima > 0:
        sub_chosen = rng.choice(n_seasonal_windows, size=n_arima, replace=False)
        sub_chosen.sort()
        arima_test_pos = test_seasonal_pos[sub_chosen]  # position in test_idx
        arima_window_idx = test_seasonal_idx[sub_chosen]  # window index
        q_arima_sub = np.empty((n_arima, 12), dtype=np.float32)

        for i, (test_k, w_idx) in enumerate(zip(arima_test_pos, arima_window_idx)):
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
                    q_arima_sub[i] = q_last_all[test_k]

        y_arima_sub = Y[arima_window_idx]
        q_seasonal_cal_arima = seasonal_all[arima_window_idx] + offset_seasonal
        test_arima_subset = {
            "lstm": metrics(q_lstm_all[arima_test_pos], y_arima_sub),
            "lstm_noattn": metrics(q_plain_all[arima_test_pos], y_arima_sub),
            "last_window": metrics(q_last_all[arima_test_pos], y_arima_sub),
            "seasonal_naive_cal": metrics(q_seasonal_cal_arima, y_arima_sub),
            "arima": metrics(q_arima_sub, y_arima_sub),
        }
    else:
        test_arima_subset = {
            "lstm": metrics(q_lstm_all, Y[test_idx]),
            "lstm_noattn": metrics(q_plain_all, Y[test_idx]),
            "last_window": metrics(q_last_all, Y[test_idx]),
            "seasonal_naive_cal": metrics(q_last_all, Y[test_idx]),
            "arima": metrics(q_last_all, Y[test_idx]),
        }

    # Find an example test window containing a burst in its target if possible
    example_k = 0
    if len(test_idx) > 0:
        found = False
        for k in range(len(test_idx)):
            w = test_idx[k]
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
        "test_seasonal_subset": test_seasonal_subset,
        "test_arima_subset": test_arima_subset,
        "offsets": offsets,
        "attention_entropy": attention_entropy,
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
