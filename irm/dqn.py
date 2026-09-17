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
    ForecastBestFit,
    ForecastFirstFit,
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


def train_dqn(
    data: dict,
    n_hosts: int,
    episodes: int,
    seed: int,
    use_k: bool = True,
    weights: tuple[float, float, float] = (10.0, 1.0, 0.1),
    verbose: bool = True,
) -> nn.Module:
    """Train DQN agent on random 12h episodes inside first 2 days.

    Returns the trained online network (nn.Module).
    """
    rng = np.random.default_rng(seed)
    agent = DQNAgent(n_hosts=n_hosts, seed=seed)
    train_policy = DQNTrainPolicy(agent)

    # First 2 days: 0 to 172800s. 12h = 43200s. Max start step = (172800 - 43200) // 300 = 432.
    max_start_step = 432

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
            use_k=use_k,
        ).run()

        if ep == 0:
            ep1_decisions = max(1, metrics["decisions"])
            expected_total = ep1_decisions * episodes
            agent.decay_decisions = max(1, int(0.6 * expected_total))

        if verbose:
            print(
                f"Episode {ep + 1:2d}/{episodes}: t0={t0 // 3600:2d}h, "
                f"decisions={metrics['decisions']}, energy={metrics['energy_kwh']:.2f} kWh, "
                f"overload_frac={metrics['sla_overload_frac']:.4f}, eps={agent.epsilon:.3f}"
            )

    return agent.online_net


def evaluate(
    data: dict,
    n_hosts: int,
    t0: int,
    t1: int,
    policies: dict[str, Policy],
    use_k: bool = True,
    weights: tuple[float, float, float] = (10.0, 1.0, 0.1),
) -> dict[str, dict]:
    """Evaluate policies from t0 to t1 using Simulator.

    Returns dict mapping policy name to metrics dict.
    """
    results = {}
    for name, policy in policies.items():
        sim = Simulator(
            data,
            t0,
            t1,
            policy,
            weights=weights,
            n_hosts=n_hosts,
            use_k=use_k,
        )
        results[name] = sim.run()
    return results


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

    weights = (10.0, 1.0, 0.1)

    t_train_start = time.monotonic()
    online_net = train_dqn(
        data,
        n_hosts,
        episodes=episodes,
        seed=seed,
        use_k=True,
        weights=weights,
        verbose=True,
    )
    train_seconds = time.monotonic() - t_train_start

    # Day 3 evaluation: t0 = 172800 (48h), t1 = 258900 (71.9h)
    t0_eval = 172800
    t1_eval = (864 - 1) * STEP

    eval_policies = {
        "FirstFit": FirstFit(),
        "BestFit": BestFit(),
        "DQN": DQNPolicy(online_net, epsilon=0.0, seed=seed),
    }
    eval_results = evaluate(
        data,
        n_hosts,
        t0_eval,
        t1_eval,
        eval_policies,
        use_k=True,
        weights=weights,
    )

    # Save model
    model_payload = {
        "state_dict": online_net.state_dict(),
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
        "FirstFit": eval_results["FirstFit"],
        "BestFit": eval_results["BestFit"],
        "DQN": eval_results["DQN"],
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


T_CRITICAL_95: dict[int, float] = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def t_critical_value(df: int) -> float:
    """Return two-sided 95% t critical value for df 1-30, 1.96 beyond, or 0.0 for df <= 0."""
    if df <= 0:
        return 0.0
    return T_CRITICAL_95.get(df, 1.96)


def compute_n_hosts(
    data: dict,
    host_slack: float = 2.0,
    t_max: int = 172800,
) -> int:
    """Size cluster from training days only (t < t_max, default 172800)."""
    step = int(data.get("step", STEP))
    steps_range = range(0, t_max, step)
    peak_cores = 0.0
    for t in steps_range:
        alive = (data["created"] <= t) & (data["deleted"] > t)
        s_c = float(np.sum(data["cores"][alive]))
        if s_c > peak_cores:
            peak_cores = s_c
    return max(4, math.ceil(host_slack * peak_cores / (HOST_CORES * OVERCOMMIT)))


def study(
    out_json: str | Path,
    seeds: tuple[int, ...] | list[int] = (0, 1, 2, 3, 4),
    episodes: int = 20,
    n_vms: int = 400,
    util_scale: float = 1.4,
    host_slack: float = 2.0,
) -> dict:
    """Run stressed cluster study with 5 seeds and no-K ablation (TRD P8)."""
    t_start_total = time.monotonic()
    per_seed = []
    n_hosts_list = []
    t0_eval = 172800
    t1_eval = (864 - 1) * STEP
    weights = (10.0, 1.0, 0.1)

    for seed in seeds:
        t_seed_start = time.monotonic()
        data = synthetic_cluster(n_vms, 864, seed=seed, util_scale=util_scale)

        # Peak Σ cores of alive VMs across training days only (t < 172800)
        n_hosts = compute_n_hosts(data, host_slack=host_slack, t_max=172800)
        n_hosts_list.append(n_hosts)

        # Train DQN (use_k=True) and DQN_noK (use_k=False) on first 2 days
        net_dqn = train_dqn(
            data,
            n_hosts,
            episodes=episodes,
            seed=seed,
            use_k=True,
            weights=weights,
            verbose=False,
        )
        net_nok = train_dqn(
            data,
            n_hosts,
            episodes=episodes,
            seed=seed,
            use_k=False,
            weights=weights,
            verbose=False,
        )

        # Evaluate FirstFit, BestFit, ForecastFirstFit_0.8, ForecastBestFit_1.0, DQN, DQN_noK on day 3
        eval_k_true = evaluate(
            data,
            n_hosts,
            t0_eval,
            t1_eval,
            policies={
                "FirstFit": FirstFit(),
                "BestFit": BestFit(),
                "ForecastFirstFit_0.8": ForecastFirstFit(cap=0.8),
                "ForecastBestFit_1.0": ForecastBestFit(cap=1.0),
                "DQN": DQNPolicy(net_dqn, epsilon=0.0, seed=seed),
            },
            use_k=True,
            weights=weights,
        )
        eval_k_false = evaluate(
            data,
            n_hosts,
            t0_eval,
            t1_eval,
            policies={
                "DQN_noK": DQNPolicy(net_nok, epsilon=0.0, seed=seed),
            },
            use_k=False,
            weights=weights,
        )

        seed_metrics = {
            "FirstFit": eval_k_true["FirstFit"],
            "BestFit": eval_k_true["BestFit"],
            "ForecastFirstFit_0.8": eval_k_true["ForecastFirstFit_0.8"],
            "ForecastBestFit_1.0": eval_k_true["ForecastBestFit_1.0"],
            "DQN": eval_k_true["DQN"],
            "DQN_noK": eval_k_false["DQN_noK"],
        }
        per_seed.append(seed_metrics)

        elapsed_seed = time.monotonic() - t_seed_start
        print(f"Seed {seed}: elapsed {elapsed_seed:.1f}s")

    # Compute summary statistics
    metrics_to_summarize = [
        "energy_kwh",
        "sla_overload_frac",
        "overloaded_host_step_frac",
        "migrations",
        "mean_active_hosts",
    ]
    policies_to_summarize = [
        "FirstFit",
        "BestFit",
        "ForecastFirstFit_0.8",
        "ForecastBestFit_1.0",
        "DQN",
        "DQN_noK",
    ]
    n_seeds = len(seeds)

    summary = {}
    for pol in policies_to_summarize:
        summary[pol] = {}
        for m in metrics_to_summarize:
            vals = [seed_res[pol][m] for seed_res in per_seed]
            mean_val = float(np.mean(vals)) if vals else 0.0
            if n_seeds <= 1:
                ci95_val = 0.0
            else:
                std_val = float(np.std(vals, ddof=1))
                df = n_seeds - 1
                t_val = t_critical_value(df)
                ci95_val = float(t_val * std_val / math.sqrt(n_seeds))
            summary[pol][m] = {"mean": mean_val, "ci95": ci95_val}

    total_seconds = float(time.monotonic() - t_start_total)

    result = {
        "seeds": list(seeds),
        "episodes": int(episodes),
        "n_vms": int(n_vms),
        "util_scale": float(util_scale),
        "host_slack": float(host_slack),
        "n_hosts": n_hosts_list,
        "sizing": "training days only",
        "ci_method": "t",
        "summary": summary,
        "per_seed": per_seed,
        "total_seconds": total_seconds,
    }

    out_p = Path(out_json)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(result, indent=2), encoding="utf-8")

    return result

