import json
from pathlib import Path
import shutil
import pytest
from irm import monitor
from irm.cli import main
from irm.monitor import (
    COLUMNS,
    bench,
    discover,
    host_gauges,
    leaf_gauges,
    next_deadline,
    open_db,
    prune,
    read_cgroup,
    read_host,
    sweep,
    write_rows,
)


def test_1_two_sweeps_leaf(tmp_path, make_cgroup, make_proc):
    root = tmp_path / "sys" / "fs" / "cgroup"
    proc = tmp_path / "proc"
    make_proc(
        proc,
        stat="cpu  100 0 50 800 0 10 20 0\n",
        meminfo="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n",
        cpu_psi="some avg10=1.00 avg60=0.50 avg300=0.20 total=100\n",
        mem_psi="some avg10=2.00 avg60=1.00 avg300=0.50 total=200\n",
        io_psi="some avg10=3.00 avg60=1.50 avg300=0.80 total=300\n",
    )
    make_cgroup(
        root,
        "leaf.slice",
        cgroup_events="populated 1\n",
        cpu_stat="usage_usec 10000000\nnr_periods 100\nnr_throttled 20\n",
        memory_current="50000000\n",
        io_stat="259:0 rbytes=100000 wbytes=200000 rios=1 wios=2 dbytes=0 dios=0\n",
        cpu_pressure="some avg10=1.00 avg60=0.50 avg300=0.20 total=100\n",
        memory_pressure="some avg10=2.00 avg60=1.00 avg300=0.50 total=200\n",
        io_pressure="some avg10=3.00 avg60=1.50 avg300=0.80 total=300\n",
        pids_current="42\n",
    )

    t0_mono = 100.0
    t0_wall = 1000.0
    rows1, state1 = sweep(root, proc, state=None, now_wall=t0_wall, now_mono=t0_mono)
    assert len(rows1) == 2

    # Second read 5 s later
    t1_mono = 105.0
    t1_wall = 1005.0
    make_cgroup(
        root,
        "leaf.slice",
        cgroup_events="populated 1\n",
        cpu_stat="usage_usec 15000000\nnr_periods 120\nnr_throttled 25\n",
        memory_current="60000000\n",
        io_stat="259:0 rbytes=150000 wbytes=300000 rios=2 wios=4 dbytes=0 dios=0\n",
        cpu_pressure="some avg10=1.50 avg60=0.60 avg300=0.30 total=150\n",
        memory_pressure="some avg10=2.50 avg60=1.20 avg300=0.60 total=250\n",
        io_pressure="some avg10=3.50 avg60=1.80 avg300=0.90 total=350\n",
        pids_current="50\n",
    )

    rows2, state2 = sweep(root, proc, state=state1, now_wall=t1_wall, now_mono=t1_mono)
    leaf_row = next(r for r in rows2 if r["cgroup"] == "/leaf.slice")

    # Exact rates:
    # Δusage_usec = 5,000,000 / 1e6 / 5.0 = 1.0 core
    assert leaf_row["cpu_cores"] == pytest.approx(1.0)
    # Δnr_throttled = 5, Δnr_periods = 20 -> 5 / 20 = 0.25
    assert leaf_row["throttled_ratio"] == pytest.approx(0.25)
    # Δrbytes = 50,000 / 5.0 = 10000.0
    assert leaf_row["io_rbps"] == pytest.approx(10000.0)
    # Δwbytes = 100,000 / 5.0 = 20000.0
    assert leaf_row["io_wbps"] == pytest.approx(20000.0)

    # Instantaneous gauges from the second read:
    assert leaf_row["mem_bytes"] == 60000000
    assert leaf_row["cpu_psi"] == pytest.approx(1.50)
    assert leaf_row["mem_psi"] == pytest.approx(2.50)
    assert leaf_row["io_psi"] == pytest.approx(3.50)
    assert leaf_row["pids"] == 50
    assert leaf_row["softirq_cores"] is None
    assert leaf_row["netrx_attrib_cores"] is None
    assert leaf_row["netrx_blamed_cores"] is None
    assert leaf_row["netrx_unattrib_cores"] is None


def test_2_throttled_ratio_zero_and_absent():
    # Δnr_periods == 0 -> throttled_ratio is 0.0
    prev = {"nr_periods": 100, "nr_throttled": 10, "usage_usec": 1000}
    cur = {"nr_periods": 100, "nr_throttled": 10, "usage_usec": 2000}
    gauges = leaf_gauges(prev, cur, dt=1.0)
    assert gauges["throttled_ratio"] == 0.0

    # Absent fields -> None
    prev_absent = {"usage_usec": 1000, "nr_periods": None, "nr_throttled": None}
    cur_absent = {"usage_usec": 2000, "nr_periods": None, "nr_throttled": None}
    gauges_absent = leaf_gauges(prev_absent, cur_absent, dt=1.0)
    assert gauges_absent["throttled_ratio"] is None


def test_3_counter_decreased_gives_none_for_that_rate_only():
    prev = {
        "usage_usec": 5000000,
        "nr_periods": 100,
        "nr_throttled": 10,
        "rbytes": 1000,
        "wbytes": 2000,
        "mem": 1000,
    }
    cur = {
        "usage_usec": 4000000,  # decreased!
        "nr_periods": 120,
        "nr_throttled": 12,
        "rbytes": 3000,  # increased
        "wbytes": 2000,  # unchanged
        "mem": 1000,
    }
    gauges = leaf_gauges(prev, cur, dt=2.0)
    assert gauges["cpu_cores"] is None
    assert gauges["throttled_ratio"] == pytest.approx(2 / 20)
    assert gauges["io_rbps"] == pytest.approx(1000.0)
    assert gauges["io_wbps"] == pytest.approx(0.0)


def test_4_first_sighting_rates_none_instantaneous_present():
    cur = {
        "usage_usec": 5000000,
        "nr_periods": 100,
        "nr_throttled": 10,
        "rbytes": 1000,
        "wbytes": 2000,
        "mem": 123456,
        "cpu_psi": 0.5,
        "mem_psi": 0.2,
        "io_psi": 0.1,
        "pids": 7,
    }
    gauges = leaf_gauges(prev=None, cur=cur, dt=5.0)
    assert gauges["cpu_cores"] is None
    assert gauges["throttled_ratio"] is None
    assert gauges["io_rbps"] is None
    assert gauges["io_wbps"] is None

    assert gauges["mem_bytes"] == 123456
    assert gauges["cpu_psi"] == pytest.approx(0.5)
    assert gauges["mem_psi"] == pytest.approx(0.2)
    assert gauges["io_psi"] == pytest.approx(0.1)
    assert gauges["pids"] == 7


def test_5_missing_memory_current_and_directory_removed_after_discover(
    tmp_path, make_cgroup, make_proc, monkeypatch
):
    root = tmp_path / "sys" / "fs" / "cgroup"
    proc = tmp_path / "proc"
    make_proc(
        proc,
        stat="cpu  100 0 50 800 0 10 20 0\n",
        meminfo="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n",
        cpu_psi="",
        mem_psi="",
        io_psi="",
    )
    make_cgroup(
        root,
        "leaf1.slice",
        cgroup_events="populated 1\n",
        cpu_stat="usage_usec 1000000\n",
    )
    leaf2_dir = make_cgroup(
        root,
        "leaf2.slice",
        cgroup_events="populated 1\n",
        cpu_stat="usage_usec 2000000\n",
        memory_current="55555\n",
    )

    # Missing memory.current -> mem_bytes is None
    raw1 = read_cgroup(root, "/leaf1.slice")
    assert raw1 is not None
    assert raw1["mem"] is None
    g1 = leaf_gauges(None, raw1, dt=1.0)
    assert g1["mem_bytes"] is None

    # A cgroup directory removed after discover is skipped
    orig_discover = monitor.discover

    def discover_and_remove(r):
        discovered = orig_discover(r)
        shutil.rmtree(leaf2_dir)
        return discovered

    monkeypatch.setattr(monitor, "discover", discover_and_remove)
    rows, state = sweep(root, proc, state=None, now_wall=1000.0, now_mono=100.0)

    cgroup_names = [r["cgroup"] for r in rows]
    assert "/leaf1.slice" in cgroup_names
    assert "host" in cgroup_names
    assert "/leaf2.slice" not in cgroup_names
    assert "/leaf2.slice" not in state["leaf"]


def test_6_discover_non_leaf_and_unpopulated_excluded(tmp_path, make_cgroup):
    root = tmp_path / "sys" / "fs" / "cgroup"
    make_cgroup(root, "parent.slice", cgroup_events="populated 1\n")
    make_cgroup(root, "parent.slice/child.slice", cgroup_events="populated 1\n")
    make_cgroup(root, "unpopulated.slice", cgroup_events="populated 0\n")
    make_cgroup(root, "active.slice", cgroup_events="populated 1\n")

    leaves = discover(root)
    assert leaves == ["/active.slice", "/parent.slice/child.slice"]


def test_7_host_row_from_proc(tmp_path, make_proc):
    proc = tmp_path / "proc"
    # user=100, nice=0, system=50, idle=800, iowait=0, irq=10, softirq=20, steal=0
    # busy = 180, softirq = 20
    make_proc(
        proc,
        stat="cpu  100 0 50 800 0 10 20 0\n",
        meminfo="MemTotal: 16000 kB\nMemAvailable: 6000 kB\n",
        cpu_psi="some avg10=1.20 avg60=0.00 avg300=0.00 total=10\n",
        mem_psi="some avg10=2.30 avg60=0.00 avg300=0.00 total=20\n",
        io_psi="some avg10=3.40 avg60=0.00 avg300=0.00 total=30\n",
    )
    raw1 = read_host(proc)
    assert raw1["busy_ticks"] == 180
    assert raw1["softirq_ticks"] == 20
    assert raw1["mem"] == (16000 - 6000) * 1024

    # 5s later: busy increases by 180 ticks, softirq increases by 20 ticks
    make_proc(
        proc,
        stat="cpu  200 0 100 1000 0 20 40 0\n",
        meminfo="MemTotal: 16000 kB\nMemAvailable: 6000 kB\n",
        cpu_psi="some avg10=1.50 avg60=0.00 avg300=0.00 total=15\n",
        mem_psi="some avg10=2.50 avg60=0.00 avg300=0.00 total=25\n",
        io_psi="some avg10=3.50 avg60=0.00 avg300=0.00 total=35\n",
    )
    raw2 = read_host(proc)
    # Δbusy = 180, Δsoftirq = 20, dt = 5.0, tck = 100
    # cpu_cores = 180 / 100 / 5.0 = 0.36
    # softirq_cores = 20 / 100 / 5.0 = 0.04
    gauges = host_gauges(raw1, raw2, dt=5.0, tck=100)
    assert gauges["cpu_cores"] == pytest.approx(0.36)
    assert gauges["softirq_cores"] == pytest.approx(0.04)
    assert gauges["mem_bytes"] == (16000 - 6000) * 1024
    assert gauges["cpu_psi"] == pytest.approx(1.50)
    assert gauges["mem_psi"] == pytest.approx(2.50)
    assert gauges["io_psi"] == pytest.approx(3.50)


def test_8_prune_retention_window(tmp_path):
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    now_wall = 10000.0
    retention_hours = 2.0  # window is 7200s, cutoff is 2800
    rows = [
        {"ts": 1000, "cgroup": "host", "cpu_cores": 1.0},
        {"ts": 2000, "cgroup": "host", "cpu_cores": 1.0},
        {"ts": 3000, "cgroup": "host", "cpu_cores": 1.0},
        {"ts": 9000, "cgroup": "host", "cpu_cores": 1.0},
    ]
    write_rows(conn, rows)
    deleted = prune(conn, now_wall=now_wall, retention_hours=retention_hours)
    assert deleted == 2

    remaining = conn.execute("select ts from samples order by ts").fetchall()
    assert remaining == [(3000,), (9000,)]
    conn.close()


def test_9_next_deadline_skips_missed_slots():
    deadline = 10.0
    interval = 5.0

    # Next deadline is in future
    assert next_deadline(deadline, interval, now_mono=11.0) == 15.0

    # Overrun: now_mono past deadline + interval
    assert next_deadline(deadline, interval, now_mono=26.0) == 30.0

    # Exact match on boundary: must return first future slot
    assert next_deadline(deadline, interval, now_mono=15.0) == 20.0


def test_10_open_db_wal_and_write_rows_upsert(tmp_path):
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    mode = conn.execute("pragma journal_mode").fetchone()[0]
    assert mode.lower() == "wal"

    row1 = {"ts": 1000, "cgroup": "/leaf.slice", "cpu_cores": 1.0}
    write_rows(conn, [row1])
    row2 = {"ts": 1000, "cgroup": "/leaf.slice", "cpu_cores": 2.5}
    write_rows(conn, [row2])

    cursor = conn.execute("select cgroup, ts, cpu_cores from samples")
    results = cursor.fetchall()
    assert len(results) == 1
    assert results[0] == ("/leaf.slice", 1000, 2.5)
    conn.close()


def test_bench_overhead(tmp_path, make_cgroup, make_proc):
    root = tmp_path / "sys" / "fs" / "cgroup"
    proc = tmp_path / "proc"
    make_proc(
        proc,
        stat="cpu  100 0 50 800 0 10 20 0\n",
        meminfo="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n",
        cpu_psi="",
        mem_psi="",
        io_psi="",
    )
    make_cgroup(
        root,
        "cg1.slice",
        cgroup_events="populated 1\n",
        cpu_stat="usage_usec 1000000\n",
    )
    res = bench(seconds=0.05, interval=0.01, root=root, proc=proc)
    assert "interval" in res
    assert "seconds" in res
    assert "cgroups" in res
    assert "cpu_seconds" in res
    assert "pct_of_one_core" in res
    assert "pct_of_host" in res
    assert res["cgroups"] == 1.0


def test_cli_subcommands(tmp_path, make_cgroup, make_proc, monkeypatch):
    root = tmp_path / "sys" / "fs" / "cgroup"
    proc = tmp_path / "proc"
    db = tmp_path / "test.db"
    make_proc(
        proc,
        stat="cpu  100 0 50 800 0 10 20 0\n",
        meminfo="MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n",
        cpu_psi="",
        mem_psi="",
        io_psi="",
    )
    make_cgroup(
        root,
        "cg.slice",
        cgroup_events="populated 1\n",
        cpu_stat="usage_usec 1000000\n",
    )

    # Monitor run with duration
    code = main([
        "monitor",
        "--interval",
        "0.01",
        "--duration",
        "0.05",
        "--db",
        str(db),
        "--root",
        str(root),
        "--proc",
        str(proc),
    ])
    assert code == 0
    assert db.is_file()

    # Bench overhead
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr("irm.cli.HOME", tmp_path)
    code = main([
        "bench",
        "overhead",
        "--seconds",
        "0.05",
        "--interval",
        "0.01",
        "--root",
        str(root),
        "--proc",
        str(proc),
    ])
    assert code == 0
    report_file = reports_dir / "overhead.json"
    assert report_file.is_file()
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert "pct_of_one_core" in data
