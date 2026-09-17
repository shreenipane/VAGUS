import io
import json
import pytest
from irm.attrib import NET_RX, Reader, attribute, cgroup_ids
from irm.cli import main
from irm.monitor import run, sweep


def test_1_two_cpus_proportional_attribution():
    prev = {
        "ts": 100.0,
        "ncpu": 2,
        "vec_ns": [[0, 0] for _ in range(10)],
        "rx_pkts": {"10": [0, 0], "20": [0, 0]},
        "blamed_ns": {"10": [0] * 10, "20": [0] * 10},
    }
    cur = {
        "ts": 102.0,
        "ncpu": 2,
        "vec_ns": [[0, 0] for _ in range(10)],
        "rx_pkts": {"10": [30, 0], "20": [10, 5]},
        "blamed_ns": {"10": [0] * 10, "20": [0] * 10},
    }
    cur["vec_ns"][NET_RX] = [4_000_000_000, 1_000_000_000]

    res = attribute(prev, cur)
    assert res is not None
    assert res["dt"] == 2.0
    assert res["attrib_cores"] == {10: 1.5, 20: 0.5 + 0.5}
    assert res["unattrib_cores"] == 0.0


def test_2_cpu_with_netrx_time_no_packets():
    prev = {
        "ts": 10.0,
        "ncpu": 2,
        "vec_ns": [[0, 0] for _ in range(10)],
        "rx_pkts": {"10": [0, 0]},
        "blamed_ns": {"10": [0] * 10},
    }
    cur = {
        "ts": 12.0,
        "ncpu": 2,
        "vec_ns": [[0, 0] for _ in range(10)],
        "rx_pkts": {"10": [10, 0]},  # packets on CPU 0 only, none on CPU 1
        "blamed_ns": {"10": [0] * 10},
    }
    # CPU 0 has 2e9 ns, CPU 1 has 3e9 ns over dt = 2s
    cur["vec_ns"][NET_RX] = [2_000_000_000, 3_000_000_000]

    res = attribute(prev, cur)
    assert res is not None
    # CPU 0: 2e9 / 1e9 / 2 = 1.0 attributed to cgroup 10
    assert res["attrib_cores"] == {10: 1.0}
    # CPU 1: 3e9 / 1e9 / 2 = 1.5 unattributed
    assert res["unattrib_cores"] == 1.5


def test_3_invalid_snapshots_return_none():
    base_prev = {
        "ts": 100.0,
        "ncpu": 2,
        "vec_ns": [[100, 100] for _ in range(10)],
        "rx_pkts": {"10": [10, 10]},
        "blamed_ns": {"10": [50] * 10},
    }
    base_cur = {
        "ts": 101.0,
        "ncpu": 2,
        "vec_ns": [[200, 200] for _ in range(10)],
        "rx_pkts": {"10": [20, 20]},
        "blamed_ns": {"10": [60] * 10},
    }
    assert attribute(base_prev, base_cur) is not None

    # 1. ts did not increase
    assert attribute(base_prev, dict(base_cur, ts=100.0)) is None
    assert attribute(base_prev, dict(base_cur, ts=99.0)) is None

    # 2. ncpu differs
    assert attribute(base_prev, dict(base_cur, ncpu=4)) is None

    # 3. cumulative value decreased
    cur_dec_vec = dict(base_cur, vec_ns=[[50, 200] if v == 0 else [200, 200] for v in range(10)])
    assert attribute(base_prev, cur_dec_vec) is None

    cur_dec_rx = dict(base_cur, rx_pkts={"10": [5, 20]})
    assert attribute(base_prev, cur_dec_rx) is None

    cur_dec_blamed = dict(base_cur, blamed_ns={"10": [40] * 10})
    assert attribute(base_prev, cur_dec_blamed) is None

    cur_missing_cg = dict(base_cur, rx_pkts={})
    assert attribute(base_prev, cur_missing_cg) is None


def test_4_cgroup_absent_from_prev_counts_from_zero():
    prev = {
        "ts": 10.0,
        "ncpu": 1,
        "vec_ns": [[0] for _ in range(10)],
        "rx_pkts": {"10": [5]},
        "blamed_ns": {"10": [0] * 10},
    }
    cur = {
        "ts": 12.0,
        "ncpu": 1,
        "vec_ns": [[0] for _ in range(10)],
        "rx_pkts": {"10": [15], "20": [10]},  # cgroup 20 absent from prev
        "blamed_ns": {
            "10": [0] * 10,
            "20": [0, 0, 0, 4_000_000_000, 0, 0, 0, 0, 0, 0],
        },
    }
    cur["vec_ns"][NET_RX] = [2_000_000_000]

    # Δt = 2.0s
    # cgroup 10 delta pkts: 10
    # cgroup 20 delta pkts: 10 (counted from 0)
    # Total packets: 20
    # R_c = 2e9 ns
    # Each gets 1e9 ns -> 1e9 / 1e9 / 2 = 0.5 cores
    # cgroup 20 blamed_ns[3] delta = 4e9 -> 4e9 / 1e9 / 2 = 2.0 cores
    res = attribute(prev, cur)
    assert res is not None
    assert res["attrib_cores"] == {10: 0.5, 20: 0.5}
    assert res["blamed_cores"] == {10: 0.0, 20: 2.0}


def test_5_blamed_cores_only_vector_3_and_vec_cores_10_entries():
    prev = {
        "ts": 50.0,
        "ncpu": 1,
        "vec_ns": [[0] for _ in range(10)],
        "rx_pkts": {},
        "blamed_ns": {"42": [0] * 10},
    }
    cur = {
        "ts": 51.0,
        "ncpu": 1,
        "vec_ns": [[v * 1_000_000_000] for v in range(10)],
        "rx_pkts": {},
        # Vector 1 has 9e9 ns, Vector 3 has 3e9 ns, Vector 5 has 8e9 ns
        "blamed_ns": {"42": [0, 9_000_000_000, 0, 3_000_000_000, 0, 8_000_000_000, 0, 0, 0, 0]},
    }
    res = attribute(prev, cur)
    assert res is not None
    assert res["blamed_cores"] == {42: 3.0}
    assert len(res["vec_cores"]) == 10
    for v in range(10):
        assert res["vec_cores"][v] == pytest.approx(float(v))


def test_6_reader_io_stringio_and_clock():
    now_time = 1000.0

    def mock_clock():
        return now_time

    s1 = json.dumps({
        "ts": 10.0, "ncpu": 1,
        "vec_ns": [[0] for _ in range(10)],
        "rx_pkts": {"10": [0]},
        "blamed_ns": {"10": [0] * 10},
    })
    s2 = json.dumps({
        "ts": 12.0, "ncpu": 1,
        "vec_ns": [[0 if v != 3 else 1_000_000_000] for v in range(10)],
        "rx_pkts": {"10": [5]},
        "blamed_ns": {"10": [0] * 10},
    })
    invalid_line = "NOT_A_VALID_JSON_LINE\n"
    s3 = json.dumps({
        "ts": 14.0, "ncpu": 1,
        "vec_ns": [[0 if v != 3 else 3_000_000_000] for v in range(10)],
        "rx_pkts": {"10": [15]},
        "blamed_ns": {"10": [0] * 10},
    })

    stream = io.StringIO(f"{s1}\n{s2}\n{invalid_line}{s3}\n")
    reader = Reader(stream, clock=mock_clock)
    reader.join(timeout=2.0)

    assert reader.invalid == 1
    latest_res = reader.latest(max_age=10.0)
    assert latest_res is not None
    assert latest_res["dt"] == 2.0
    assert latest_res["attrib_cores"] == {10: 1.0}

    now_time += 15.0
    assert reader.latest(max_age=10.0) is None


def test_7_sweep_with_fixture_tree(tmp_path, make_cgroup, make_proc):
    root = tmp_path / "sys" / "fs" / "cgroup"
    proc = tmp_path / "proc"
    make_proc(
        proc,
        stat="cpu  100 0 50 800 0 10 20 0\n",
        meminfo="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n",
        cpu_psi="", mem_psi="", io_psi="",
    )
    cg1 = make_cgroup(root, "leaf1.slice", cgroup_events="populated 1\n", cpu_stat="usage_usec 1000\n")
    cg2 = make_cgroup(root, "leaf2.slice", cgroup_events="populated 1\n", cpu_stat="usage_usec 1000\n")

    root_ino = root.stat().st_ino
    cg1_ino = cg1.stat().st_ino

    hand_made_result = {
        "dt": 2.0,
        "attrib_cores": {cg1_ino: 1.5},
        "blamed_cores": {cg1_ino: 0.8, root_ino: 0.3},
        "unattrib_cores": 0.2,
        "vec_cores": [0.0] * 10,
    }

    # 1. Sweep with hand_made_result
    rows, _ = sweep(root, proc, state=None, now_wall=1000.0, now_mono=100.0, attrib=hand_made_result)
    r_leaf1 = next(r for r in rows if r["cgroup"] == "/leaf1.slice")
    r_leaf2 = next(r for r in rows if r["cgroup"] == "/leaf2.slice")
    r_host = next(r for r in rows if r["cgroup"] == "host")

    # leaf1 listed in attrib: filled
    assert r_leaf1["netrx_attrib_cores"] == 1.5
    assert r_leaf1["netrx_blamed_cores"] == 0.8
    assert r_leaf1["netrx_unattrib_cores"] is None

    # leaf2 unlisted in attrib: gets 0.0
    assert r_leaf2["netrx_attrib_cores"] == 0.0
    assert r_leaf2["netrx_blamed_cores"] == 0.0
    assert r_leaf2["netrx_unattrib_cores"] is None

    # host row: unattributed and root-blamed values
    assert r_host["netrx_attrib_cores"] is None
    assert r_host["netrx_blamed_cores"] == 0.3
    assert r_host["netrx_unattrib_cores"] == 0.2

    # 2. Sweep with attrib=None: all four columns are None
    rows_none, _ = sweep(root, proc, state=None, now_wall=1000.0, now_mono=100.0, attrib=None)
    r1_none = next(r for r in rows_none if r["cgroup"] == "/leaf1.slice")
    r2_none = next(r for r in rows_none if r["cgroup"] == "/leaf2.slice")
    rh_none = next(r for r in rows_none if r["cgroup"] == "host")

    for r in (r1_none, r2_none, rh_none):
        assert r["netrx_attrib_cores"] is None
        assert r["netrx_blamed_cores"] is None
        assert r["netrx_unattrib_cores"] is None


def test_cgroup_ids_and_cli(tmp_path, make_cgroup):
    root = tmp_path / "sys" / "fs" / "cgroup"
    c1 = make_cgroup(root, "a.slice")
    c2 = make_cgroup(root, "a.slice/b.slice")
    ids = cgroup_ids(root)
    assert ids[root.stat().st_ino] == "/"
    assert ids[c1.stat().st_ino] == "/a.slice"
    assert ids[c2.stat().st_ino] == "/a.slice/b.slice"

    # --attrib choices validation
    with pytest.raises(SystemExit) as exc:
        main(["monitor", "--attrib", "invalid"])
    assert exc.value.code == 2
