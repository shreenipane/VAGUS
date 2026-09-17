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
    (root / cg_o1.lstrip("/")).mkdir(parents=True)
    (root / cg_o2.lstrip("/")).mkdir(parents=True)

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

    # Protected keeps existing limits: cpu_max and memory_high are None, cpu_weight is 1000
    p_item = items[cg_p]
    assert p_item["cpu_max"] is None
    assert p_item["cpu_weight"] == 1000
    assert p_item["memory_high"] is None
    assert p_item["old"]["cpu.max"] == "max 100000"
    assert p_item["old"]["memory.high"] == "max"
    assert p_item["old"]["cpu.weight"] is None
    assert p_item["reason"] == "protected; peak 2.00 cores (empirical); CPU weight raised, existing limits kept"

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
    (root / cg_a.lstrip("/")).mkdir(parents=True)
    (root / cg_b.lstrip("/")).mkdir(parents=True)

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
    (root / cg.lstrip("/")).mkdir(parents=True)
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


def test_recommend_idle_leaves_are_background(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 16)

    db_path = tmp_path / "test1.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg_crit = "/user.slice/user-1000.slice/user@1000.service/crit.scope"
    cg_noisy = "/user.slice/user-1000.slice/user@1000.service/noisy.scope"
    idle_cgs = [
        f"/user.slice/user-1000.slice/user@1000.service/idle_{i:02d}.scope"
        for i in range(50)
    ]
    for cg in [cg_crit, cg_noisy] + idle_cgs:
        (root / cg.lstrip("/")).mkdir(parents=True)

    now = 1000000
    t_start = now - 120

    rows = []
    for i in range(24):
        ts = t_start + i * 5
        rows.append((ts, cg_crit, 5.0, 0.0, 10 * 1024 * 1024))
        rows.append((ts, cg_noisy, 10.0, 0.0, 10 * 1024 * 1024))
        for idle in idle_cgs:
            rows.append((ts, idle, 0.01, 0.0, 10 * 1024 * 1024))

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
            "values (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        protect=[cg_crit],
        headroom=1.25,
        min_samples=20,
        hours=1.0,
        now=now,
    )

    assert recs["ncpu"] == 16
    items = {it["cgroup"]: it for it in recs["items"]}
    assert set(items.keys()) == {cg_crit, cg_noisy}

    skipped = {s["cgroup"]: s["reason"] for s in recs["skipped"]}
    assert len(skipped) == 50
    for idle in idle_cgs:
        assert idle in skipped
        assert skipped[idle] == "background: peak 0.01 cores < 0.50"

    assert items[cg_noisy]["cpu_max"] == "925000 100000"
    assert (
        items[cg_noisy]["reason"]
        == "q95 10.00 cores (empirical) × 1.25; squeezed to fit 9.25 free cores after 6.25 reserved and 0.50 background"
    )
    assert items[cg_crit]["cpu_max"] is None
    assert items[cg_crit]["memory_high"] is None
    assert items[cg_crit]["cpu_weight"] == 1000
    assert items[cg_crit]["reason"] == "protected; peak 5.00 cores (empirical); CPU weight raised, existing limits kept"


def test_recommend_budget_smaller_than_floors(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    db_path = tmp_path / "test2.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg_crit = "/user.slice/user-1000.slice/user@1000.service/crit.scope"
    cg_t1 = "/user.slice/user-1000.slice/user@1000.service/t1.scope"
    cg_t2 = "/user.slice/user-1000.slice/user@1000.service/t2.scope"
    cg_t3 = "/user.slice/user-1000.slice/user@1000.service/t3.scope"
    for cg in (cg_crit, cg_t1, cg_t2, cg_t3):
        (root / cg.lstrip("/")).mkdir(parents=True)

    now = 1000000
    t_start = now - 120

    rows = []
    for i in range(24):
        ts = t_start + i * 5
        rows.append((ts, cg_crit, 4.0, 0.0, 10 * 1024 * 1024))
        rows.append((ts, cg_t1, 1.0, 0.0, 10 * 1024 * 1024))
        rows.append((ts, cg_t2, 1.0, 0.0, 10 * 1024 * 1024))
        rows.append((ts, cg_t3, 1.0, 0.0, 10 * 1024 * 1024))

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
            "values (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        protect=[cg_crit],
        headroom=1.25,
        min_samples=20,
        hours=1.0,
        now=now,
    )

    items = {it["cgroup"]: it for it in recs["items"]}
    for cg in (cg_t1, cg_t2, cg_t3):
        assert items[cg]["cpu_max"] == "10000 100000"
        assert items[cg]["reason"].endswith("; raised to the 0.1-core floor")
        assert "squeezed to fit 0.00 free cores" in items[cg]["reason"]


def test_recommend_only_filters_non_protected_targets(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 8)

    db_path = tmp_path / "test3.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg_noisy = "/user.slice/user-1000.slice/user@1000.service/noisy.scope"
    cg_busy2 = "/user.slice/user-1000.slice/user@1000.service/busy2.scope"
    for cg in (cg_noisy, cg_busy2):
        (root / cg.lstrip("/")).mkdir(parents=True)

    now = 1000000
    t_start = now - 120

    rows = []
    for i in range(24):
        ts = t_start + i * 5
        rows.append((ts, cg_noisy, 5.0, 0.0, 10 * 1024 * 1024))
        rows.append((ts, cg_busy2, 3.0, 0.0, 10 * 1024 * 1024))

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
            "values (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        only=[cg_noisy],
        headroom=1.25,
        min_samples=20,
        hours=1.0,
        now=now,
    )

    items = {it["cgroup"]: it for it in recs["items"]}
    assert cg_noisy in items
    assert cg_busy2 not in items

    skipped = {s["cgroup"]: s["reason"] for s in recs["skipped"]}
    assert cg_busy2 in skipped
    assert skipped[cg_busy2] == "background: not in --only"


def test_recommend_cli_min_cores_and_only(tmp_path, monkeypatch, capsys):
    import time
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    db_path = tmp_path / "cli_opts.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg1 = "/user.slice/user-1000.slice/user@1000.service/app1.scope"
    cg2 = "/user.slice/user-1000.slice/user@1000.service/app2.scope"
    for cg in (cg1, cg2):
        (root / cg.lstrip("/")).mkdir(parents=True)
    now = int(time.time())
    with sqlite3.connect(db_path) as conn:
        for i in range(10):
            ts = now - 50 + i * 5
            conn.execute(
                "insert into samples (ts, cgroup, cpu_cores, mem_bytes) values (?, ?, ?, ?)",
                (ts, cg1, 1.0, 50 * 1024 * 1024),
            )
            conn.execute(
                "insert into samples (ts, cgroup, cpu_cores, mem_bytes) values (?, ?, ?, ?)",
                (ts, cg2, 1.0, 50 * 1024 * 1024),
            )
        conn.commit()

    out_json = tmp_path / "out.json"
    code = main([
        "recommend",
        "--db", str(db_path),
        "--root", str(root),
        "--min-samples", "10",
        "--only", cg1,
        "--min-cores", "0.5",
        "--out", str(out_json),
    ])
    assert code == 0
    with open(out_json, "r", encoding="utf-8") as f:
        recs = json.load(f)
    items = {it["cgroup"] for it in recs["items"]}
    assert cg1 in items
    assert cg2 not in items
    skipped = {s["cgroup"]: s["reason"] for s in recs["skipped"]}
    assert skipped.get(cg2) == "background: not in --only"


def test_recommend_skips_gone_cgroups(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)

    db_path = tmp_path / "gone.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg_a = "/a.scope"
    cg_b = "/b.scope"

    # Only a.scope exists in fixture root; b.scope does not exist
    (root / cg_a.lstrip("/")).mkdir(parents=True)

    now = 1000000
    t_start = now - 120

    with sqlite3.connect(db_path) as conn:
        for i in range(24):
            ts = t_start + i * 5
            conn.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_a, 1.0, 0.0, 50 * 1024 * 1024),
            )
            conn.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_b, 1.0, 0.0, 50 * 1024 * 1024),
            )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        min_samples=20,
        hours=1.0,
        now=now,
    )

    items = [it["cgroup"] for it in recs["items"]]
    assert items == [cg_a]

    skipped = {s["cgroup"]: s["reason"] for s in recs["skipped"]}
    assert cg_b in skipped
    assert skipped[cg_b] == "gone: cgroup no longer exists"
    assert cg_a not in skipped


def test_protected_cgroup_keeps_existing_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 4)
    db_path = tmp_path / "prot.db"
    create_test_db(db_path)
    root = tmp_path / "cgroup"

    cg_p = "/user.slice/user-1000.slice/user@1000.service/p.scope"
    p_dir = root / cg_p.lstrip("/")
    p_dir.mkdir(parents=True)
    (p_dir / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
    (p_dir / "memory.high").write_text("500000000\n", encoding="utf-8")

    now = 1000000
    t_start = now - 120

    with sqlite3.connect(db_path) as conn:
        for i in range(24):
            ts = t_start + i * 5
            conn.execute(
                "insert into samples (ts, cgroup, cpu_cores, netrx_attrib_cores, mem_bytes) "
                "values (?, ?, ?, ?, ?)",
                (ts, cg_p, 1.5, 0.0, 50 * 1024 * 1024),
            )
        conn.commit()

    recs = recommend(
        db_path=db_path,
        root=root,
        protect=[cg_p],
        min_samples=20,
        now=now,
    )

    assert "generated_at" in recs
    assert recs["generated_at"] == now
    items = {it["cgroup"]: it for it in recs["items"]}
    assert cg_p in items
    p = items[cg_p]
    assert p["cpu_max"] is None
    assert p["memory_high"] is None
    assert p["cpu_weight"] == 1000
    assert p["old"]["cpu.max"] == "200000 100000"
    assert p["old"]["memory.high"] == "500000000"
    assert p["reason"] == "protected; peak 1.50 cores (empirical); CPU weight raised, existing limits kept"


