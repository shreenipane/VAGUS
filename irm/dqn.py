import json
import math
from pathlib import Path
import time
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from irm.sim import (
    HOST_CORES,
    OVERCOMMIT,
    STEP,
    BestFit,
    FirstFit,
    Policy,
    Simulator,
    synthetic_cluster,
)


class QNet(nn.Module):
    """MLP 14 -> 64 -> 64 -> 1, ReLU (TRD §10)."""

    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(14, 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def masked_argmax(q: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """Argmax over candidate mask, ignoring masked entries even when their Q is highest."""
    q_masked = q.clone()
    q_masked[~mask] = -float("inf")
    return q_masked.argmax(dim=-1)


def compute_double_dqn_target(
    r: torch.Tensor | float,
    gamma: float,
    done: torch.Tensor | bool,
    q_online_next: torch.Tensor,
    q_tgt_next: torch.Tensor,
    mask_next: torch.Tensor,
) -> torch.Tensor:
    """Double DQN target: y = r + γ (1 - done) Q_tgt(X_next[a*]).

    a* = argmax over mask of Q_online(X_next); an empty mask means done.
    """
    is_1d = q_online_next.ndim == 1
    if is_1d:
        q_online_next = q_online_next.unsqueeze(0)
        q_tgt_next = q_tgt_next.unsqueeze(0)
        mask_next = mask_next.unsqueeze(0)
        if isinstance(r, (int, float)):
            r_t = torch.tensor([float(r)], dtype=torch.float32)
        elif r.ndim == 0:
            r_t = r.unsqueeze(0)
        else:
            r_t = r
        if isinstance(done, bool):
            done_t = torch.tensor([done], dtype=torch.bool)
        elif done.ndim == 0:
            done_t = done.unsqueeze(0)
        else:
            done_t = done
    else:
        if isinstance(r, (int, float)):
            r_t = torch.full((q_online_next.size(0),), float(r), dtype=torch.float32)
        else:
            r_t = r
        if isinstance(done, bool):
            done_t = torch.full((q_online_next.size(0),), bool(done), dtype=torch.bool)
        else:
            done_t = done

    a_star = masked_argmax(q_online_next, mask_next)
    q_tgt_val = q_tgt_next.gather(1, a_star.unsqueeze(1)).squeeze(1)

    empty_mask = ~mask_next.any(dim=-1)
    effective_done = done_t | empty_mask

    target = r_t + gamma * (~effective_done).float() * q_tgt_val
    return target.squeeze(0) if is_1d else target


class ReplayBuffer:
    """Numpy ring buffer with capacity 20,000."""

    def __init__(self, capacity: int, n_hosts: int):
        self.capacity = capacity
        self.n_hosts = n_hosts
        self.states = np.zeros((capacity, 14), dtype=np.float32)
        self.rewards = np.zeros(capacity, dtype=np.float32)
        self.next_states = np.zeros((capacity, n_hosts, 14), dtype=np.float32)
        self.next_masks = np.zeros((capacity, n_hosts), dtype=bool)
        self.dones = np.zeros(capacity, dtype=bool)
        self.idx = 0
        self.size = 0

    def push(
        self,
        s: np.ndarray,
        r: float,
        s_next: np.ndarray,
        mask_next: np.ndarray,
        done: bool,
    ):
        self.states[self.idx] = s
        self.rewards[self.idx] = r
        self.next_states[self.idx] = s_next
        self.next_masks[self.idx] = mask_next
        self.dones[self.idx] = done
        self.idx = (self.idx + 1) % self.capacity
        self.size = min(self.size + 1, self.capacity)

    def sample(self, batch_size: int, rng: np.random.Generator):
        indices = rng.integers(0, self.size, size=batch_size)
        return (
            torch.from_numpy(self.states[indices]),
            torch.from_numpy(self.rewards[indices]),
            torch.from_numpy(self.next_states[indices]),
            torch.from_numpy(self.next_masks[indices]),
            torch.from_numpy(self.dones[indices]),
        )

    def __len__(self):
        return self.size


class DQNAgent:
    """Double DQN agent with Huber loss, Adam 1e-3, target sync 500 steps."""

    def __init__(
        self,
        n_hosts: int,
        seed: int = 0,
        lr: float = 1e-3,
        gamma: float = 0.9,
        replay_cap: int = 20000,
        batch_size: int = 256,
        target_sync: int = 500,
        learn_every: int = 4,
        warmup_transitions: int = 500,
    ):
        torch.manual_seed(seed)
        self.rng = np.random.default_rng(seed)
        self.n_hosts = n_hosts
        self.gamma = gamma
        self.batch_size = batch_size
        self.target_sync = target_sync
        self.learn_every = learn_every
        self.warmup = warmup_transitions

        self.online_net = QNet()
        self.target_net = QNet()
        self.target_net.load_state_dict(self.online_net.state_dict())
        self.target_net.eval()

        self.optimizer = torch.optim.Adam(self.online_net.parameters(), lr=lr)
        self.replay = ReplayBuffer(replay_cap, n_hosts)

        self.total_decisions = 0
        self.grad_steps = 0
        self.epsilon = 1.0
        self.decay_decisions = 1

    def on_transition(
        self,
        s: np.ndarray,
        r: float,
        s_next: np.ndarray,
        mask_next: np.ndarray,
        done: bool,
    ):
        self.replay.push(s, r, s_next, mask_next, done)

    def step_learning(self):
        self.total_decisions += 1
        # Update epsilon linearly from 1.0 to 0.05 over decay_decisions
        if self.decay_decisions > 0:
            frac = min(1.0, self.total_decisions / self.decay_decisions)
            self.epsilon = max(0.05, 1.0 - (1.0 - 0.05) * frac)

        if len(self.replay) < self.warmup:
            return
        if self.total_decisions % self.learn_every != 0:
            return
        if len(self.replay) < self.batch_size:
            return

        states, rewards, next_states, next_masks, dones = self.replay.sample(
            self.batch_size, self.rng
        )

        self.online_net.train()
        q_pred = self.online_net(states).squeeze(-1)

        with torch.no_grad():
            q_online_next = self.online_net(next_states).squeeze(-1)
            q_tgt_next = self.target_net(next_states).squeeze(-1)
            targets = compute_double_dqn_target(
                rewards, self.gamma, dones, q_online_next, q_tgt_next, next_masks
            )

        loss = F.smooth_l1_loss(q_pred, targets)
        self.optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(self.online_net.parameters(), 10.0)
        self.optimizer.step()

        self.grad_steps += 1
        if self.grad_steps % self.target_sync == 0:
            self.target_net.load_state_dict(self.online_net.state_dict())


class DQNTrainPolicy(Policy):
    """Policy used during training, routing choices through agent and triggering learning."""

    def __init__(self, agent: DQNAgent):
        self.agent = agent

    def choose(self, features: np.ndarray, host_ids: list[int]) -> int:
        self.agent.step_learning()
        if len(host_ids) == 0:
            return 0
        if self.agent.rng.random() < self.agent.epsilon:
            return int(self.agent.rng.integers(0, len(host_ids)))

        self.agent.online_net.eval()
        with torch.no_grad():
            t_feats = torch.from_numpy(features).float()
            q_vals = self.agent.online_net(t_feats).squeeze(-1)
            return int(q_vals.argmax().item())


class DQNPolicy(Policy):
    """Evaluation / deployment policy with fixed epsilon."""

    def __init__(self, net: nn.Module, epsilon: float = 0.0, seed: int = 0):
        self.net = net
        self.epsilon = epsilon
        self.rng = np.random.default_rng(seed)

    def choose(self, features: np.ndarray, host_ids: list[int]) -> int:
        if len(host_ids) == 0:
            return 0
        if self.epsilon > 0.0 and self.rng.random() < self.epsilon:
            return int(self.rng.integers(0, len(host_ids)))
        self.net.eval()
        with torch.no_grad():
            t_feats = torch.from_numpy(features).float()
            q_vals = self.net(t_feats).squeeze(-1)
            return int(q_vals.argmax().item())


def demo(
    out_json: str | Path,
    seed: int = 0,
    episodes: int = 20,
    model_path: str | Path = "models/dqn.pt",
) -> dict:
    """Run prototype P4 demonstration:

    1. synthetic_cluster(400, 864, seed) (3 days).
    2. Train DQN on random 12h episodes inside first 2 days.
    3. Evaluate FirstFit, BestFit, greedy DQN on day 3.
    4. Save models/dqn.pt (weights_only-loadable) and write out_json.
    """
    t_start_wall = time.monotonic()
    rng = np.random.default_rng(seed)

    data = synthetic_cluster(400, 864, seed=seed)

    # Compute n_hosts across cluster data
    steps_range = range(0, 864 * STEP, STEP)
    peak_cores = 0.0
    for t in steps_range:
        alive = (data["created"] <= t) & (data["deleted"] > t)
        s_c = float(np.sum(data["cores"][alive]))
        if s_c > peak_cores:
            peak_cores = s_c
    n_hosts = max(4, math.ceil(1.25 * peak_cores / (HOST_CORES * OVERCOMMIT)))

    agent = DQNAgent(n_hosts=n_hosts, seed=seed)
    train_policy = DQNTrainPolicy(agent)
    weights = (10.0, 1.0, 0.1)

    # First 2 days: 0 to 172800s. 12h = 43200s. Max start step = (172800 - 43200) // 300 = 432.
    max_start_step = 432

    t_train_start = time.monotonic()
    for ep in range(episodes):
        start_step = int(rng.integers(0, max_start_step + 1))
        t0 = start_step * STEP
        t1 = t0 + 43200

        metrics = Simulator(
            data,
            t0,
            t1,
            train_policy,
            weights=weights,
            on_transition=agent.on_transition,
            n_hosts=n_hosts,
        ).run()

        if ep == 0:
            ep1_decisions = max(1, metrics["decisions"])
            expected_total = ep1_decisions * episodes
            agent.decay_decisions = max(1, int(0.6 * expected_total))

        print(
            f"Episode {ep + 1:2d}/{episodes}: t0={t0 // 3600:2d}h, "
            f"decisions={metrics['decisions']}, energy={metrics['energy_kwh']:.2f} kWh, "
            f"overload_frac={metrics['sla_overload_frac']:.4f}, eps={agent.epsilon:.3f}"
        )

    train_seconds = time.monotonic() - t_train_start

    # Day 3 evaluation: t0 = 172800 (48h), t1 = 258900 (71.9h)
    t0_eval = 172800
    t1_eval = (864 - 1) * STEP

    ff_metrics = Simulator(
        data, t0_eval, t1_eval, FirstFit(), weights=weights, n_hosts=n_hosts
    ).run()
    bf_metrics = Simulator(
        data, t0_eval, t1_eval, BestFit(), weights=weights, n_hosts=n_hosts
    ).run()
    greedy_policy = DQNPolicy(agent.online_net, epsilon=0.0, seed=seed)
    dqn_metrics = Simulator(
        data, t0_eval, t1_eval, greedy_policy, weights=weights, n_hosts=n_hosts
    ).run()

    # Save model
    model_payload = {
        "state_dict": agent.online_net.state_dict(),
        "w_sla": weights[0],
        "w_energy": weights[1],
        "w_mig": weights[2],
    }
    model_target = Path(model_path)
    try:
        model_target.parent.mkdir(parents=True, exist_ok=True)
        torch.save(model_payload, model_target)
    except (OSError, RuntimeError):
        fallback_target = Path("/tmp") / model_path
        fallback_target.parent.mkdir(parents=True, exist_ok=True)
        torch.save(model_payload, fallback_target)

    result = {
        "FirstFit": ff_metrics,
        "BestFit": bf_metrics,
        "DQN": dqn_metrics,
        "n_hosts": n_hosts,
        "t0": t0_eval,
        "t1": t1_eval,
        "weights": list(weights),
        "train_seconds": float(train_seconds),
    }

    out_p = Path(out_json)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return result
