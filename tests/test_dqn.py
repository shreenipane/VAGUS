import json
from pathlib import Path
import pytest
import torch

from irm.dqn import (
    QNet,
    compute_double_dqn_target,
    demo,
    masked_argmax,
)
import irm.dqn


def test_masked_argmax_ignores_masked_highest():
    """Masked argmax ignores masked entries even when their Q is highest."""
    # 1D test
    q = torch.tensor([100.0, 5.0, 10.0])
    mask = torch.tensor([False, True, True])
    # Even though index 0 has Q=100.0, mask[0] is False. Highest valid is index 2 with Q=10.0
    chosen = masked_argmax(q, mask)
    assert chosen.item() == 2

    # 2D test
    q_batch = torch.tensor([
        [50.0, 1.0, 2.0],
        [3.0, 200.0, 5.0],
    ])
    mask_batch = torch.tensor([
        [False, True, True],
        [True, False, True],
    ])
    chosen_batch = masked_argmax(q_batch, mask_batch)
    assert chosen_batch.tolist() == [2, 2]


def test_double_dqn_target_hand_values():
    """Double-DQN target on hand values: y = r + γ (1 - done) Q_tgt(X_next[a*])."""
    r = 2.0
    gamma = 0.9
    done = False

    q_online_next = torch.tensor([1.0, 3.0])  # a* = 1
    q_tgt_next = torch.tensor([4.0, 5.0])     # Q_tgt(1) = 5.0
    mask = torch.tensor([True, True])

    # Hand calculation: 2.0 + 0.9 * 5.0 = 6.5
    target = compute_double_dqn_target(r, gamma, done, q_online_next, q_tgt_next, mask)
    assert target.item() == pytest.approx(6.5)

    # If host 1 is masked out: a* = 0, Q_tgt(0) = 4.0 -> 2.0 + 0.9 * 4.0 = 5.6
    mask_masked = torch.tensor([True, False])
    target_masked = compute_double_dqn_target(r, gamma, done, q_online_next, q_tgt_next, mask_masked)
    assert target_masked.item() == pytest.approx(5.6)

    # If done is True -> target = r = 2.0
    target_done = compute_double_dqn_target(r, gamma, True, q_online_next, q_tgt_next, mask)
    assert target_done.item() == pytest.approx(2.0)

    # If mask is empty -> target = r = 2.0
    mask_empty = torch.tensor([False, False])
    target_empty = compute_double_dqn_target(r, gamma, False, q_online_next, q_tgt_next, mask_empty)
    assert target_empty.item() == pytest.approx(2.0)


def test_tiny_demo_writes_every_key(tmp_path, monkeypatch):
    """A tiny demo (episodes=1, monkeypatch to 40 VMs over 1.5 days if needed) writes every key."""
    out_json = tmp_path / "placement.json"
    model_path = tmp_path / "models" / "dqn.pt"

    # Monkeypatch synthetic_cluster to smaller size: 40 VMs over 864 steps (or smaller)
    orig_synthetic_cluster = irm.dqn.synthetic_cluster

    def small_synthetic_cluster(n_vms, steps, seed=0):
        # 40 VMs, keep steps=864 so evaluation indices match
        return orig_synthetic_cluster(40, steps, seed=seed)

    monkeypatch.setattr(irm.dqn, "synthetic_cluster", small_synthetic_cluster)

    res = demo(out_json, seed=0, episodes=1, model_path=model_path)

    # Check returned dict keys
    required_keys = ["FirstFit", "BestFit", "DQN", "n_hosts", "t0", "t1", "weights", "train_seconds"]
    for key in required_keys:
        assert key in res

    # Check metrics keys inside each policy
    metric_keys = [
        "energy_kwh",
        "sla_overload_frac",
        "overloaded_host_step_frac",
        "migrations",
        "rejected",
        "mean_active_hosts",
        "decisions",
    ]
    for pol in ["FirstFit", "BestFit", "DQN"]:
        for m_key in metric_keys:
            assert m_key in res[pol]

    # Check file was written and is valid JSON
    assert out_json.exists()
    file_data = json.loads(out_json.read_text(encoding="utf-8"))
    assert file_data["n_hosts"] == res["n_hosts"]

    # Check saved model
    assert model_path.exists()
    loaded = torch.load(model_path, weights_only=True)
    assert "state_dict" in loaded
    assert "w_sla" in loaded
    assert "w_energy" in loaded
    assert "w_mig" in loaded

    # Verify QNet can load state_dict
    qnet = QNet()
    qnet.load_state_dict(loaded["state_dict"])


def test_demo_full_20_episodes(tmp_path):
    """Full 20-episode demo on 400 VMs over 3 days (synthetic_cluster(400, 864, seed=0)).

    Must finish in under 3 minutes on CPU.
    """
    out_json = tmp_path / "placement_full.json"
    model_path = tmp_path / "models" / "dqn.pt"

    res = demo(out_json, seed=0, episodes=20, model_path=model_path)

    assert out_json.exists()
    assert model_path.exists()
    assert res["train_seconds"] < 180.0  # Under 3 minutes
    assert "FirstFit" in res
    assert "BestFit" in res
    assert "DQN" in res
