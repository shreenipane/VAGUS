import math
from typing import Callable
import numpy as np

# TRD §9 Constants
HOST_CORES = 48
HOST_MEM_GB = 384
OVERCOMMIT = 2.0
P_IDLE_W = 100
P_MAX_W = 250
STEP = 300
K_WINDOW = 144
K_MIN = 24


def compute_k(x: np.ndarray, y: np.ndarray) -> float:
    """Compute K(v, h) between candidate VM series and other VMs on host.

    Over steps where both are finite: if fewer than K_MIN, or either standard
    deviation is 0, K = 0.5; else K = (1 - pearson(x, y)) / 2.
    """
    valid = np.isfinite(x) & np.isfinite(y)
    x_v = x[valid]
    y_v = y[valid]
    if len(x_v) < K_MIN:
        return 0.5
    std_x = float(np.std(x_v))
    std_y = float(np.std(y_v))
    if std_x == 0.0 or std_y == 0.0:
        return 0.5
    r = float(np.corrcoef(x_v, y_v)[0, 1])
    r = max(-1.0, min(1.0, r))
    return float((1.0 - r) / 2.0)


def synthetic_cluster(
    n_vms: int, steps: int, seed: int = 0, util_scale: float = 1.0
) -> dict:
    """Generate synthetic VM traces for simulation.

    Returns dict with keys:
    - cpu_max, cpu_avg: [V, T] float32 percent
    - cores: [V] from {2, 4, 8}
    - mem_gb: 4 * cores
    - created, deleted: seconds
    - t_start: 0
    - step: 300
    """
    rng = np.random.default_rng(seed)
    total_seconds = steps * STEP
    t_arr = np.arange(steps) * STEP

    # Cores and memory
    cores = rng.choice([2, 4, 8], size=n_vms).astype(np.float32)
    mem_gb = (4.0 * cores).astype(np.float32)

    # VM lifetimes: 60% exist from t=0 to the end; rest arrive uniformly and live 2-24 h
    n_perm = int(round(n_vms * 0.6))
    created = np.zeros(n_vms, dtype=np.int64)
    deleted = np.zeros(n_vms, dtype=np.int64)

    created[:n_perm] = 0
    deleted[:n_perm] = total_seconds

    if n_vms > n_perm:
        dyn_count = n_vms - n_perm
        lifetimes = rng.integers(2 * 3600, 24 * 3600 + 1, size=dyn_count)
        # Arrive uniformly over [0, total_seconds]
        arr_times = rng.integers(0, max(1, total_seconds), size=dyn_count)
        created[n_perm:] = arr_times
        deleted[n_perm:] = arr_times + lifetimes

    # Diurnal sinusoids: half peak around noon (12:00 = 43200s), half around midnight (0s)
    # Day length = 86400s
    t_2d = t_arr[np.newaxis, :]  # [1, T]
    # For noon peak: cos reaches 1 at t=43200 -> -cos(2*pi*t/86400) reaches 1 at 43200 (-cos(pi) = 1)
    sinusoid_noon = -np.cos(2.0 * np.pi * t_2d / 86400.0)
    sinusoid_midnight = np.cos(2.0 * np.pi * t_2d / 86400.0)

    half = n_vms // 2
    sinusoids = np.zeros((n_vms, steps), dtype=np.float32)
    sinusoids[:half] = sinusoid_noon
    sinusoids[half:] = sinusoid_midnight

    # Base utilization: 35% +/- 20%
    base = 35.0 + 20.0 * sinusoids
    noise = rng.normal(0.0, 4.0, size=(n_vms, steps))
    # Occasional bursts (Pareto shape 1.5)
    burst_mask = rng.random(size=(n_vms, steps)) < 0.015
    burst_vals = rng.pareto(1.5, size=(n_vms, steps)) * 15.0
    series = (base + noise + np.where(burst_mask, burst_vals, 0.0)) * util_scale

    cpu_max = np.clip(series, 0.0, 100.0).astype(np.float32)
    # cpu_avg is a fraction of cpu_max, between 60% and 95%
    avg_factors = rng.uniform(0.6, 0.95, size=(n_vms, steps)).astype(np.float32)
    cpu_avg = np.clip(cpu_max * avg_factors, 0.0, cpu_max).astype(np.float32)

    # Set NaN where unobserved (t < created or t >= deleted)
    t_grid = t_arr[np.newaxis, :]
    alive = (t_grid >= created[:, np.newaxis]) & (t_grid < deleted[:, np.newaxis])
    cpu_max[~alive] = np.nan
    cpu_avg[~alive] = np.nan

    return {
        "cpu_max": cpu_max,
        "cpu_avg": cpu_avg,
        "cores": cores,
        "mem_gb": mem_gb,
        "created": created,
        "deleted": deleted,
        "t_start": 0,
        "step": STEP,
        "vmid": np.array([f"vm_{i}" for i in range(n_vms)]),
    }


class Policy:
    def choose(self, features: np.ndarray, host_ids: list[int]) -> int:
        raise NotImplementedError


class FirstFit(Policy):
    """FirstFit: choose lowest candidate host ID."""

    def choose(self, features: np.ndarray, host_ids: list[int]) -> int:
        return int(np.argmin(host_ids))


class BestFit(Policy):
    """BestFit: highest host_alloc_frac after placement, ties to lowest ID.

    Policies see only the 14 candidate features.
    host_alloc_frac is feature index 1; vm_cores_frac is feature index 5.
    """

    def choose(self, features: np.ndarray, host_ids: list[int]) -> int:
        post_alloc = features[:, 1] + features[:, 5]
        max_val = float(np.max(post_alloc))
        tie_indices = np.where(np.abs(post_alloc - max_val) < 1e-6)[0]
        best_sub = int(np.argmin([host_ids[i] for i in tie_indices]))
        return int(tie_indices[best_sub])


class PendingDecision:
    def __init__(self, dec_id: int, t_d: int, host: int, s: np.ndarray, r_imm: float):
        self.dec_id = dec_id
        self.t_d = t_d
        self.host = host
        self.s = s
        self.r_imm = r_imm
        self.overload_sum = 0.0
        self.s_next = None
        self.mask_next = None
        self.done = False
        self.finalized = False
        self.reward = 0.0


class Simulator:
    """Cluster simulator implementing TRD §9 with prototype changes."""

    def __init__(
        self,
        data: dict,
        t0: int,
        t1: int,
        policy: Policy,
        weights: tuple[float, float, float] = (10.0, 1.0, 0.1),
        on_transition: Callable | None = None,
        n_hosts: int | None = None,
        use_k: bool = True,
    ):
        self.data = data
        self.t0 = t0
        self.t1 = t1
        self.policy = policy
        self.w_sla, self.w_energy, self.w_mig = weights
        self.on_transition = on_transition
        self.use_k = use_k

        self.cpu_max = data["cpu_max"]
        self.cpu_avg = data["cpu_avg"]
        self.cores = data["cores"]
        self.mem_gb = data["mem_gb"]
        self.created = data["created"]
        self.deleted = data["deleted"]
        self.n_vms = len(self.cores)
        self.t_start = int(data.get("t_start", 0))
        self.step_size = int(data.get("step", STEP))

        # Compute n_hosts if not provided
        if n_hosts is not None:
            self.n_hosts = n_hosts
        else:
            # Peak Σ cores of alive VMs across simulation range
            steps_range = range(self.t0, self.t1 + 1, self.step_size)
            peak_cores = 0.0
            for t in steps_range:
                alive = (self.created <= t) & (self.deleted > t)
                s_cores = float(np.sum(self.cores[alive]))
                if s_cores > peak_cores:
                    peak_cores = s_cores
            self.n_hosts = max(4, math.ceil(1.25 * peak_cores / (HOST_CORES * OVERCOMMIT)))

        # Forecast cache: (v, s) -> (vm_q95, has_history)
        self._forecast_cache: dict[tuple[int, int], tuple[float, float]] = {}

    def _vm_forecast(self, v: int, s: int) -> tuple[float, float]:
        """VM forecast: 95th percentile of last 48 finite cpu_max/100 before s.

        With fewer than 12 finite values, returns (1.0, 0.0).
        """
        key = (v, s)
        if key in self._forecast_cache:
            return self._forecast_cache[key]
        if s == 0:
            res = (1.0, 0.0)
        else:
            # Finite cpu_max/100 before s
            row = self.cpu_max[v, :s]
            fin = row[np.isfinite(row)]
            if len(fin) < 12:
                res = (1.0, 0.0)
            else:
                last_48 = fin[-48:] / 100.0
                q95 = float(np.percentile(last_48, 95))
                res = (q95, 1.0)
        self._forecast_cache[key] = res
        return res

    def _candidate_features(
        self,
        v: int,
        candidates: list[int],
        s: int,
        host_vms: list[set[int]],
        host_cores: list[float],
        host_mem: list[float],
    ) -> np.ndarray:
        """Compute the 14 candidate features for host candidates in exact order."""
        features = np.zeros((len(candidates), 14), dtype=np.float32)
        v_cores = float(self.cores[v])
        v_q95, v_has_hist = self._vm_forecast(v, s)
        vm_cores_frac = v_cores / (HOST_CORES * OVERCOMMIT)
        vm_q95_frac = v_q95 * v_cores / HOST_CORES

        # Precompute v's series for K
        s_start = max(0, s - K_WINDOW)
        x_k = (self.cpu_max[v, s_start:s] / 100.0) * v_cores if self.use_k else None

        for i, h in enumerate(candidates):
            vms_on_h = host_vms[h]
            is_active = 1.0 if len(vms_on_h) > 0 else 0.0
            h_alloc_frac = host_cores[h] / (HOST_CORES * OVERCOMMIT)
            h_mem_frac = host_mem[h] / HOST_MEM_GB

            # host_util_now = demand_max / HOST_CORES
            d_max = 0.0
            h_q95_sum = 0.0
            for u in vms_on_h:
                val = self.cpu_max[u, s]
                u_util = (val / 100.0) if np.isfinite(val) else 0.0
                d_max += u_util * self.cores[u]
                u_q95, _ = self._vm_forecast(u, s)
                h_q95_sum += u_q95 * self.cores[u]

            h_util_now = d_max / HOST_CORES
            h_q95_frac = h_q95_sum / HOST_CORES

            # K(v, h)
            if not self.use_k:
                k_val = 0.5
            else:
                other_vms = [u for u in vms_on_h if u != v]
                if not other_vms:
                    k_val = 0.5
                else:
                    y_k = np.zeros_like(x_k)
                    for u in other_vms:
                        u_hist = self.cpu_max[u, s_start:s]
                        u_demand = np.nan_to_num(u_hist / 100.0, nan=0.0) * self.cores[u]
                        y_k += u_demand
                    k_val = compute_k(x_k, y_k)

            post_q95_frac = h_q95_frac + vm_q95_frac

            features[i, 0] = is_active
            features[i, 1] = h_alloc_frac
            features[i, 2] = h_mem_frac
            features[i, 3] = h_util_now
            features[i, 4] = h_q95_frac
            features[i, 5] = vm_cores_frac
            features[i, 6] = vm_q95_frac
            features[i, 7] = v_has_hist
            features[i, 8] = 0.0  # life_b0
            features[i, 9] = 0.0  # life_b1
            features[i, 10] = 0.0  # life_b2
            features[i, 11] = 0.0  # life_b3
            features[i, 12] = k_val
            features[i, 13] = post_q95_frac

        return features

    def run(self) -> dict:
        """Execute simulation from t0 to t1 and return metrics dict."""
        host_vms: list[set[int]] = [set() for _ in range(self.n_hosts)]
        host_cores: list[float] = [0.0] * self.n_hosts
        host_mem: list[float] = [0.0] * self.n_hosts
        vm_host: dict[int, int] = {}
        placed_vms: set[int] = set()

        total_energy_kwh = 0.0
        total_overload = 0.0
        total_demand_max = 0.0
        total_active_host_steps = 0
        overloaded_host_steps = 0
        migrations = 0
        rejected = 0
        decisions_count = 0

        pending_decisions: list[PendingDecision] = []
        last_decision: PendingDecision | None = None

        steps_range = list(range(self.t0, self.t1 + 1, self.step_size))
        n_steps = len(steps_range)

        def dispatch_finalized_transitions(flush_all: bool = False):
            if self.on_transition is None:
                return
            idx = 0
            while idx < len(pending_decisions):
                d = pending_decisions[idx]
                if d.finalized and (d.s_next is not None or flush_all):
                    if d.s_next is None:
                        d.s_next = np.zeros((self.n_hosts, 14), dtype=np.float32)
                        d.mask_next = np.zeros(self.n_hosts, dtype=bool)
                        d.done = True
                    self.on_transition(d.s, d.reward, d.s_next, d.mask_next, d.done)
                    idx += 1
                else:
                    break
            if idx > 0:
                del pending_decisions[:idx]

        for s_idx, t in enumerate(steps_range):
            s = (t - self.t_start) // self.step_size

            # 1. Departures: deleted <= t
            departing = [v for v, h in vm_host.items() if self.deleted[v] <= t]
            for v in departing:
                h = vm_host.pop(v)
                host_vms[h].remove(v)
                host_cores[h] -= float(self.cores[v])
                host_mem[h] -= float(self.mem_gb[v])

            # 2. Arrivals: created <= t, not yet placed, deleted > t, in created order
            # VMs created before t0 arrive at t0 (since created[v] <= t0 <= t)
            arriving_candidates = [
                v
                for v in range(self.n_vms)
                if v not in placed_vms and self.created[v] <= t and self.deleted[v] > t
            ]

            # Sort arrivals by created order (stable)
            arriving_candidates.sort(key=lambda v: (self.created[v], v))

            for v in arriving_candidates:
                placed_vms.add(v)
                v_cores = float(self.cores[v])
                v_mem = float(self.mem_gb[v])

                # Feasible hosts among all n_hosts
                feasible_hosts = [
                    h
                    for h in range(self.n_hosts)
                    if (host_cores[h] + v_cores <= HOST_CORES * OVERCOMMIT + 1e-6)
                    and (host_mem[h] + v_mem <= HOST_MEM_GB + 1e-6)
                ]

                if not feasible_hosts:
                    rejected += 1
                    continue

                features = self._candidate_features(v, feasible_hosts, s, host_vms, host_cores, host_mem)

                # Transition linkage for previous decision
                cand_padded = np.zeros((self.n_hosts, 14), dtype=np.float32)
                cand_mask = np.zeros(self.n_hosts, dtype=bool)
                for i_c, h_c in enumerate(feasible_hosts):
                    cand_padded[h_c] = features[i_c]
                    cand_mask[h_c] = True

                if last_decision is not None:
                    last_decision.s_next = cand_padded
                    last_decision.mask_next = cand_mask
                    last_decision.done = False

                chosen_sub = self.policy.choose(features, feasible_hosts)
                chosen_h = feasible_hosts[chosen_sub]

                # Immediate reward
                r_imm = 0.0
                if len(host_vms[chosen_h]) == 0:
                    r_imm -= self.w_energy

                # Record decision
                dec = PendingDecision(
                    dec_id=decisions_count,
                    t_d=t,
                    host=chosen_h,
                    s=features[chosen_sub],
                    r_imm=r_imm,
                )
                pending_decisions.append(dec)
                last_decision = dec
                decisions_count += 1

                # Place VM
                host_vms[chosen_h].add(v)
                host_cores[chosen_h] += v_cores
                host_mem[chosen_h] += v_mem
                vm_host[v] = chosen_h

            # 3. Evacuation on hour boundaries after t0: t > t0 and (t - t0) % 3600 == 0
            if (t > self.t0) and ((t - self.t0) % 3600 == 0):
                active_hosts = [h for h in range(self.n_hosts) if len(host_vms[h]) > 0]
                if len(active_hosts) >= 2:
                    # Find active host with lowest host_q95_frac
                    best_src = None
                    lowest_q95_frac = float("inf")
                    for h in sorted(active_hosts):
                        h_q95_sum = sum(
                            self._vm_forecast(u, s)[0] * self.cores[u] for u in host_vms[h]
                        )
                        frac = h_q95_sum / HOST_CORES
                        if frac < lowest_q95_frac:
                            lowest_q95_frac = frac
                            best_src = h

                    source = best_src
                    # Check if a First-Fit plan (largest cores first) onto other active hosts exists
                    other_active = [h for h in sorted(active_hosts) if h != source]
                    src_vms = sorted(host_vms[source], key=lambda u: (self.cores[u], u), reverse=True)

                    sim_cores = {h: host_cores[h] for h in other_active}
                    sim_mem = {h: host_mem[h] for h in other_active}
                    plan_possible = True

                    for u in src_vms:
                        u_c = float(self.cores[u])
                        u_m = float(self.mem_gb[u])
                        fitted = False
                        for h in other_active:
                            if (sim_cores[h] + u_c <= HOST_CORES * OVERCOMMIT + 1e-6) and (
                                sim_mem[h] + u_m <= HOST_MEM_GB + 1e-6
                            ):
                                sim_cores[h] += u_c
                                sim_mem[h] += u_m
                                fitted = True
                                break
                        if not fitted:
                            plan_possible = False
                            break

                    if plan_possible:
                        for u in src_vms:
                            u_c = float(self.cores[u])
                            u_m = float(self.mem_gb[u])
                            candidates = [
                                h
                                for h in other_active
                                if (host_cores[h] + u_c <= HOST_CORES * OVERCOMMIT + 1e-6)
                                and (host_mem[h] + u_m <= HOST_MEM_GB + 1e-6)
                            ]
                            if not candidates:
                                # If a VM has no candidate, evacuation stops there
                                break

                            features = self._candidate_features(
                                u, candidates, s, host_vms, host_cores, host_mem
                            )

                            cand_padded = np.zeros((self.n_hosts, 14), dtype=np.float32)
                            cand_mask = np.zeros(self.n_hosts, dtype=bool)
                            for i_c, h_c in enumerate(candidates):
                                cand_padded[h_c] = features[i_c]
                                cand_mask[h_c] = True

                            if last_decision is not None:
                                last_decision.s_next = cand_padded
                                last_decision.mask_next = cand_mask
                                last_decision.done = False

                            chosen_sub = self.policy.choose(features, candidates)
                            chosen_h = candidates[chosen_sub]

                            # Move VM
                            host_vms[source].remove(u)
                            host_cores[source] -= u_c
                            host_mem[source] -= u_m

                            host_vms[chosen_h].add(u)
                            host_cores[chosen_h] += u_c
                            host_mem[chosen_h] += u_m
                            vm_host[u] = chosen_h

                            migrations += 1

                            # Reward for evacuation
                            r_imm = -self.w_mig
                            if len(host_vms[source]) == 0:
                                r_imm += self.w_energy

                            dec = PendingDecision(
                                dec_id=decisions_count,
                                t_d=t,
                                host=chosen_h,
                                s=features[chosen_sub],
                                r_imm=r_imm,
                            )
                            pending_decisions.append(dec)
                            last_decision = dec
                            decisions_count += 1

            # 4. Accounting
            step_overloads: dict[int, float] = {}
            for h in range(self.n_hosts):
                vms_on_h = host_vms[h]
                if not vms_on_h:
                    continue  # off: 0 energy, 0 overload

                total_active_host_steps += 1
                d_max = 0.0
                d_avg = 0.0
                for u in vms_on_h:
                    max_val = self.cpu_max[u, s]
                    avg_val = self.cpu_avg[u, s]
                    u_max = (max_val / 100.0) if np.isfinite(max_val) else 0.0
                    u_avg = (avg_val / 100.0) if np.isfinite(avg_val) else 0.0
                    d_max += u_max * self.cores[u]
                    d_avg += u_avg * self.cores[u]

                total_demand_max += d_max
                power = P_IDLE_W + (P_MAX_W - P_IDLE_W) * min(1.0, d_avg / HOST_CORES)
                energy = power * self.step_size / 3.6e6
                total_energy_kwh += energy

                h_overload = max(0.0, d_max - HOST_CORES)
                total_overload += h_overload
                step_overloads[h] = h_overload

                if h_overload > 0.0:
                    overloaded_host_steps += 1

            # Update SLA overloads for pending decisions
            for d in pending_decisions:
                if d.t_d < t <= d.t_d + 3600:
                    d.overload_sum += step_overloads.get(d.host, 0.0)
                if (not d.finalized) and (t >= d.t_d + 3600):
                    d.finalized = True
                    r_sla = -self.w_sla * (d.overload_sum / HOST_CORES / 12.0)
                    d.reward = d.r_imm + r_sla

            dispatch_finalized_transitions(flush_all=False)

        # Episode end: finalize any remaining pending decisions
        for d in pending_decisions:
            if not d.finalized:
                d.finalized = True
                r_sla = -self.w_sla * (d.overload_sum / HOST_CORES / 12.0)
                d.reward = d.r_imm + r_sla

        if last_decision is not None:
            last_decision.done = True

        dispatch_finalized_transitions(flush_all=True)

        sla_overload_frac = (total_overload / total_demand_max) if total_demand_max > 0.0 else 0.0
        overloaded_host_step_frac = (
            (overloaded_host_steps / total_active_host_steps)
            if total_active_host_steps > 0
            else 0.0
        )
        mean_active_hosts = total_active_host_steps / n_steps if n_steps > 0 else 0.0

        return {
            "energy_kwh": float(total_energy_kwh),
            "sla_overload_frac": float(sla_overload_frac),
            "overloaded_host_step_frac": float(overloaded_host_step_frac),
            "migrations": int(migrations),
            "rejected": int(rejected),
            "mean_active_hosts": float(mean_active_hosts),
            "decisions": int(decisions_count),
            "overload": float(total_overload),
        }
