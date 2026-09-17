import json
import math
import os
from pathlib import Path
import sqlite3
import pytest
from irm.cli import main
from irm.monitor import SCHEMA
from irm.recommend import recommend


def create_test_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)


def test_recommend_proportional_squeeze_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    db_path = tmp_path / "test.db"
    create_test_db(db_path)

    root = tmp_path / "cgroup"
    cg_p = "/user.slice/user-1000.slice/user@1000.service/p.scope"
    cg_o1 = "/user.slice/user-1000.slice/user@1000.service/o1.scope"
    cg_o2 = "/user.slice/user-1000.slice/user@1000.service/o2.scope"
    cg_short = "/user.slice/user-1000.slice/user@1000.service/short.scope"

    # Create root files for reading old values
    p_dir = root / cg_p.lstrip("/")
    p_dir.mkdir(parents=True)
    (p_dir / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    (p_dir / "memory.high").write_text("max\n", encoding="utf-8")

    now = 1000000
    t_start = now - 120  # 2 minutes ago

    # Insert hand rows
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        # 24 samples (one every 5s for 120s) for p, o1, o2
        for i in range(24):
            ts = t_start + i * 5
            # Protected: peak demand around 2.0
            cur.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_p, 2.0, 0.0, 50 * 1024 * 1024),
            )
            # Other 1: peak demand around 2.0; memory mostly 100 MiB, one spike at 200 MiB
            mem_o1 = (200 * 1024 * 1024) if i == 23 else (100 * 1024 * 1024)
            cur.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_o1, 2.0, 0.0, mem_o1),
            )
            # Other 2: peak demand around 2.0
            cur.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_o2, 2.0, 0.0, 40 * 1024 * 1024),
            )

        # cg_short has only 5 samples (< min_samples 20)
        for i in range(5):
            ts = t_start + i * 5
            cur.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_short, 1.0, 0.0, 10 * 1024 * 1024),
            )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        protect=[cg_p],
        headroom=1.25,
        min_samples=20,
        hours=1.0,
        now=now,
    )

    assert recs["ncpu"] == 4
    items = {it["cgroup"]: it for it in recs["items"]}
    assert set(items.keys()) == {cg_p, cg_o1, cg_o2}

    # Protected gets max/1000/max
    p_item = items[cg_p]
    assert p_item["cpu_max"] == "max"
    assert p_item["cpu_weight"] == 1000
    assert p_item["memory_high"] == "max"
    assert p_item["old"]["cpu.max"] == "max 100000"
    assert p_item["old"]["memory.high"] == "max"
    assert p_item["old"]["cpu.weight"] is None

    # Others squeezed so Σ quota ≤ avail
    # ncpu=4, reserve = 2.0 * 1.25 = 2.5
    # avail = max(0.2, 4 - 2.5) = 1.5
    # want1 = 2.0 * 1.25 = 2.5; want2 = 2.5; sum_want = 5.0
    # scale = 1.5 / 5.0 = 0.3
    # quota = 2.5 * 0.3 = 0.75 each
    # Σ quota = 0.75 + 0.75 = 1.5 <= avail (1.5)
    o1_item = items[cg_o1]
    o2_item = items[cg_o2]
    assert o1_item["cpu_max"] == "75000 100000"
    assert o2_item["cpu_max"] == "75000 100000"

    # memory.high from max, not P95:
    # max for o1 is 200 MiB; headroom 1.25 -> 250 MiB.
    # P95 would have been ~100 MiB -> 125 MiB.
    expected_mem = 250 * 1024 * 1024
    assert o1_item["memory_high"] == expected_mem

    # Too few samples -> skipped with a reason
    skipped_cgs = {s["cgroup"]: s["reason"] for s in recs["skipped"]}
    assert cg_short in skipped_cgs
    assert "fewer than 20 samples" in skipped_cgs[cg_short]


def test_recommend_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    db_path = tmp_path / "pairs.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg_a = "/user.slice/user-1000.slice/user@1000.service/a.scope"
    cg_b = "/user.slice/user-1000.slice/user@1000.service/b.scope"

    now = 1000000
    # 12 buckets of 30s = 360s
    t_start = now - 360
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for i in range(12):
            ts = t_start + i * 30 + 5
            val_a = 0.8 + i * 0.1
            val_b = 1.6 + i * 0.2  # Perfectly correlated, ρ = 1.0
            cur.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_a, val_a, 0.0, 10 * 1024 * 1024),
            )
            cur.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_b, val_b, 0.0, 10 * 1024 * 1024),
            )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        min_samples=10,
        now=now,
    )
    assert len(recs["pairs"]) == 1
    p = recs["pairs"][0]
    assert p["a"] == cg_a
    assert p["b"] == cg_b
    assert p["rho"] >= 0.99
    assert math.isclose(p["k"], (1.0 - p["rho"]) / 2.0, abs_tol=1e-3)


def test_recommend_cli(tmp_path, monkeypatch, capsys):
    import time
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    db_path = tmp_path / "cli.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg = "/user.slice/user-1000.slice/user@1000.service/app.scope"
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        for i in range(10):
            conn.execute(
                "insert into samples (ts, cgroup, cpu_cores, mem_bytes) values (?, ?, ?, ?)",
                (now - 50 + i * 5, cg, 1.0, 50 * 1024 * 1024),
            )
        conn.commit()

    out_json = tmp_path / "out.json"
    code = main([
        "recommend",
        "--db", str(db_path),
        "--root", str(root),
        "--min-samples", "10",
        "--out", str(out_json),
    ])
    assert code == 0
    assert out_json.is_file()
    captured = capsys.readouterr()
    assert cg in captured.out
