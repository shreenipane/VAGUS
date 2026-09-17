import numpy as np
import pytest

from irm.sim import (
    HOST_CORES,
    HOST_MEM_GB,
    OVERCOMMIT,
    STEP,
    BestFit,
    FirstFit,
    Policy,
    Simulator,
    compute_k,
    synthetic_cluster,
)


def test_handbuilt_2host_2vm_scenario():
    """Hand-built 2-host, 2-VM scenario with constant utilization:

    Exact energy_kwh and overload over 3 steps.
    """
    # 3 steps: t = 0, 300, 600
    t0 = 0
    t1 = 600

    # VM 0: 24 cores, 96 GB memory. Constant 100% max, 80% avg cpu
    # VM 1: 36 cores, 144 GB memory. Constant 100% max, 80% avg cpu
    # Both fit on Host 0 (24 + 36 = 60 cores <= 96 capacity, 96 + 144 = 240 GB <= 384 capacity).
    # FirstFit places both on Host 0. Host 1 is inactive (off, 0 W).
    # On Host 0:
    #   demand_max = 1.0 * 24 + 1.0 * 36 = 60.0 cores
    #   demand_avg = 0.8 * 24 + 0.8 * 36 = 48.0 cores
    #   overload = max(0, 60.0 - 48.0) = 12.0 cores per step
    #   power = 100 + (250 - 100) * min(1, 48.0 / 48) = 250 W
    #   energy_per_step = 250 * 300 / 3.6e6 = 1 / 48 kWh
    # Over 3 steps:
    #   total_overload = 3 * 12.0 = 36.0
    #   total_energy = 3 * (250 * 300 / 3.6e6) = 0.0625 kWh
    #   total_demand_max = 3 * 60.0 = 180.0
    #   sla_overload_frac = 36.0 / 180.0 = 0.2
    #   mean_active_hosts = 3 / 3 = 1.0
    #   overloaded_host_step_frac = 3 / 3 = 1.0
    #   decisions = 2
    cpu_max = np.array([
        [100.0, 100.0, 100.0],
        [100.0, 100.0, 100.0],
    ], dtype=np.float32)
    cpu_avg = np.array([
        [80.0, 80.0, 80.0],
        [80.0, 80.0, 80.0],
    ], dtype=np.float32)
    cores = np.array([24.0, 36.0], dtype=np.float32)
    mem_gb = np.array([96.0, 144.0], dtype=np.float32)
    created = np.array([0, 0], dtype=np.int64)
    deleted = np.array([1800, 1800], dtype=np.int64)

    data = {
        "cpu_max": cpu_max,
        "cpu_avg": cpu_avg,
        "cores": cores,
        "mem_gb": mem_gb,
        "created": created,
        "deleted": deleted,
        "t_start": 0,
        "step": STEP,
    }

    sim = Simulator(data, t0, t1, FirstFit(), weights=(10.0, 1.0, 0.1), n_hosts=2)
    metrics = sim.run()

    assert metrics["energy_kwh"] == pytest.approx(0.0625)
    assert metrics["overload"] == pytest.approx(36.0)
    assert metrics["sla_overload_frac"] == pytest.approx(0.2)
    assert metrics["decisions"] == 2
    assert metrics["migrations"] == 0
    assert metrics["rejected"] == 0
    assert metrics["mean_active_hosts"] == pytest.approx(1.0)
    assert metrics["overloaded_host_step_frac"] == pytest.approx(1.0)


def test_k():
    """K: identical series -> 0; negated series -> 1; constant or < 24 points -> 0.5."""
    rng = np.random.default_rng(123)
    s = rng.normal(10.0, 3.0, size=30)

    # Identical series
    assert compute_k(s, s) == pytest.approx(0.0)

    # Negated series
    assert compute_k(s, -s) == pytest.approx(1.0)

    # Constant series
    const_s = np.full(30, 4.0)
    assert compute_k(s, const_s) == 0.5
    assert compute_k(const_s, s) == 0.5
    assert compute_k(const_s, const_s) == 0.5

    # Fewer than 24 points
    short_s = s[:20]
    assert compute_k(short_s, short_s) == 0.5


def test_infeasible_hosts_never_appear_as_candidates():
    """Infeasible hosts (exceeding cores or mem limit) never appear in candidate hosts."""
    # Create 2 hosts. Host 0 is filled to exact cores capacity (96 cores).
    # When VM 2 arrives needing 4 cores, Host 0 cannot fit it. Only Host 1 is a candidate.
    t0 = 0
    t1 = 300
    cpu_max = np.full((3, 2), 50.0, dtype=np.float32)
    cpu_avg = np.full((3, 2), 40.0, dtype=np.float32)
    # VM 0: 48 cores, VM 1: 48 cores (together 96 cores = HOST_CORES * OVERCOMMIT)
    # VM 2: 4 cores
    cores = np.array([48.0, 48.0, 4.0], dtype=np.float32)
    mem_gb = np.array([100.0, 100.0, 16.0], dtype=np.float32)
    created = np.array([0, 0, 300], dtype=np.int64)
    deleted = np.array([1000, 1000, 1000], dtype=np.int64)

    data = {
        "cpu_max": cpu_max,
        "cpu_avg": cpu_avg,
        "cores": cores,
        "mem_gb": mem_gb,
        "created": created,
        "deleted": deleted,
        "t_start": 0,
        "step": STEP,
    }

    candidates_history = []

    class InspectPolicy(Policy):
        def choose(self, features, host_ids):
            candidates_history.append(list(host_ids))
            return int(np.argmin(host_ids))

    sim = Simulator(data, t0, t1, InspectPolicy(), n_hosts=2)
    sim.run()

    # Third decision is for VM 2 at step 300. Host 0 has 96 cores, so 96 + 4 = 100 > 96.
    # Host 0 must NOT appear as a candidate.
    assert len(candidates_history) == 3
    assert 0 not in candidates_history[2]
    assert candidates_history[2] == [1]


def test_synthetic_cluster():
    """Verify synthetic_cluster properties and determinism."""
    data1 = synthetic_cluster(40, 100, seed=42)
    data2 = synthetic_cluster(40, 100, seed=42)

    # Determinism
    np.testing.assert_array_equal(data1["cores"], data2["cores"])
    np.testing.assert_array_equal(data1["cpu_max"][:10], data2["cpu_max"][:10])

    # Shapes and constraints
    assert data1["cpu_max"].shape == (40, 100)
    assert data1["cpu_avg"].shape == (40, 100)
    assert len(data1["cores"]) == 40
    assert np.all(np.isin(data1["cores"], [2, 4, 8]))
    np.testing.assert_array_equal(data1["mem_gb"], data1["cores"] * 4.0)

    # 60% permanent VMs
    n_perm = int(round(40 * 0.6))
    assert np.all(data1["created"][:n_perm] == 0)
    assert np.all(data1["deleted"][:n_perm] == 100 * STEP)


def test_evacuation():
    """Verify evacuation on hour boundary after t0: lowest host_q95_frac evacuated."""
    # 2 hosts.
    # At t=0, VM 0 is placed on Host 0 (4 cores), VM 1 is placed on Host 1 (8 cores).
    # t ranges from 0 to 3600 (hour boundary at 3600).
    # Both hosts are active. Host 0 has lower q95 frac.
    # At t=3600, Host 0 is evacuated to Host 1.
    n_steps = 3600 // STEP + 1  # 13 steps
    cpu_max = np.full((2, n_steps), 20.0, dtype=np.float32)
    cpu_avg = np.full((2, n_steps), 15.0, dtype=np.float32)
    # VM 0 on Host 0, VM 1 on Host 1
    cores = np.array([4.0, 8.0], dtype=np.float32)
    mem_gb = np.array([16.0, 32.0], dtype=np.float32)
    created = np.array([0, 0], dtype=np.int64)
    deleted = np.array([7200, 7200], dtype=np.int64)

    data = {
        "cpu_max": cpu_max,
        "cpu_avg": cpu_avg,
        "cores": cores,
        "mem_gb": mem_gb,
        "created": created,
        "deleted": deleted,
        "t_start": 0,
        "step": STEP,
    }

    # Custom policy that initially places VM 0 on host 0, VM 1 on host 1
    class SeparatePolicy(Policy):
        def __init__(self):
            self.call = 0

        def choose(self, features, host_ids):
            self.call += 1
            if self.call == 1:
                return host_ids.index(0)
            elif self.call == 2:
                return host_ids.index(1)
            # Evacuation move
            return 0

    sim = Simulator(data, 0, 3600, SeparatePolicy(), weights=(10.0, 1.0, 0.1), n_hosts=2)
    metrics = sim.run()

    # VM 0 should have been migrated from Host 0 to Host 1 at t=3600
    assert metrics["migrations"] == 1


def test_bestfit_vs_firstfit():
    """Verify BestFit chooses highest post_alloc_frac while FirstFit chooses lowest ID."""
    t0 = 0
    t1 = 300
    cpu_max = np.full((3, 2), 10.0, dtype=np.float32)
    cpu_avg = np.full((3, 2), 10.0, dtype=np.float32)

    # VM 0: 20 cores (places on 0)
    # VM 1: 40 cores (places on 1)
    # VM 2: 4 cores arriving at step 300.
    # Host 0 would have (20+4)/96 = 24/96 alloc.
    # Host 1 would have (40+4)/96 = 44/96 alloc.
    # BestFit should choose Host 1. FirstFit should choose Host 0.
    cores = np.array([20.0, 40.0, 4.0], dtype=np.float32)
    mem_gb = np.array([80.0, 160.0, 16.0], dtype=np.float32)
    created = np.array([0, 0, 300], dtype=np.int64)
    deleted = np.array([1000, 1000, 1000], dtype=np.int64)

    data = {
        "cpu_max": cpu_max,
        "cpu_avg": cpu_avg,
        "cores": cores,
        "mem_gb": mem_gb,
        "created": created,
        "deleted": deleted,
        "t_start": 0,
        "step": STEP,
    }

    # With FirstFit, initial VM 0 and VM 1 would both go to 0 because 20 + 40 = 60 <= 96.
    # Let's test BestFit choose directly on features
    features = np.zeros((2, 14), dtype=np.float32)
    # host 0: alloc_frac = 20/96
    features[0, 1] = 20.0 / (HOST_CORES * OVERCOMMIT)
    # host 1: alloc_frac = 40/96
    features[1, 1] = 40.0 / (HOST_CORES * OVERCOMMIT)
    # vm_cores_frac = 4/96
    features[:, 5] = 4.0 / (HOST_CORES * OVERCOMMIT)

    bf = BestFit()
    assert bf.choose(features, [0, 1]) == 1

    ff = FirstFit()
    assert ff.choose(features, [0, 1]) == 0


def test_transitions_callback():
    """Verify Simulator calls on_transition with valid tuple shapes and final done=True."""
    t0 = 0
    t1 = 600
    cpu_max = np.full((2, 3), 10.0, dtype=np.float32)
    cpu_avg = np.full((2, 3), 10.0, dtype=np.float32)
    cores = np.array([4.0, 4.0], dtype=np.float32)
    mem_gb = np.array([16.0, 16.0], dtype=np.float32)
    created = np.array([0, 300], dtype=np.int64)
    deleted = np.array([1000, 1000], dtype=np.int64)

    data = {
        "cpu_max": cpu_max,
        "cpu_avg": cpu_avg,
        "cores": cores,
        "mem_gb": mem_gb,
        "created": created,
        "deleted": deleted,
        "t_start": 0,
        "step": STEP,
    }

    recorded = []

    def on_trans(s, r, s_next, mask_next, done):
        recorded.append((s, r, s_next, mask_next, done))

    sim = Simulator(data, t0, t1, FirstFit(), on_transition=on_trans, n_hosts=4)
    metrics = sim.run()

    assert len(recorded) == 2
    # Transition 0: done is False, next_mask has candidates
    assert recorded[0][0].shape == (14,)
    assert isinstance(recorded[0][1], float)
    assert recorded[0][2].shape == (4, 14)
    assert recorded[0][3].shape == (4,)
    assert recorded[0][4] is False

    # Last transition: done is True
    assert recorded[1][4] is True

