import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import types
from datetime import datetime, timezone
import pytest
from irm.cli import main
from irm.execute import apply, revert, validate


@pytest.fixture
def fake_tree(tmp_path, monkeypatch):
    monkeypatch.setattr("os.getuid", lambda: 1000)
    root = tmp_path / "cgroup"
    app_dir = root / "user.slice" / "user-1000.slice" / "user@1000.service" / "app.slice"
    app_dir.mkdir(parents=True)
    (app_dir / "cgroup.controllers").write_text("cpu memory io\n", encoding="utf-8")
    (app_dir / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    (app_dir / "memory.high").write_text("max\n", encoding="utf-8")
    (app_dir / "memory.current").write_text("100000000\n", encoding="utf-8")
    return root, app_dir


def test_validate_rules(fake_tree, tmp_path):
    root, app_dir = fake_tree
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    # Valid items return None
    assert validate(root, cg, "cpu.max", "max 100000") is None
    assert validate(root, cg, "cpu.max", "50000 100000") is None
    assert validate(root, cg, "memory.high", "max") is None
    assert validate(root, cg, "memory.high", "67108864") is None
    assert validate(root, cg, "cpu.weight", "100") is None

    # '..' component rejected
    err = validate(root, "/user.slice/user-1000.slice/../user-1000.slice/user@1000.service/app.slice", "cpu.max", "max")
    assert err is not None and ".." in err

    # Symlink out of prefix rejected
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    sym = root / "user.slice" / "user-1000.slice" / "user@1000.service" / "escaped"
    sym.symlink_to(outside)
    err = validate(root, "/user.slice/user-1000.slice/user@1000.service/escaped", "cpu.max", "max 100000")
    assert err is not None and "not strictly inside" in err

    # Sibling prefix user@1000.service-x rejected
    sibling = root / "user.slice" / "user-1000.slice" / "user@1000.service-x" / "app.slice"
    sibling.mkdir(parents=True)
    (sibling / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    err = validate(root, "/user.slice/user-1000.slice/user@1000.service-x/app.slice", "cpu.max", "max 100000")
    assert err is not None and "not strictly inside" in err

    # Disallowed file rejected
    err = validate(root, cg, "cgroup.procs", "1234")
    assert err is not None and "disallowed file" in err

    # Disallowed values
    assert validate(root, cg, "cpu.max", "500 100000") is not None
    assert validate(root, cg, "memory.high", "1000") is not None
    assert validate(root, cg, "cpu.weight", "0") is not None
    assert validate(root, cg, "cpu.weight", "10001") is not None


def test_dry_run_writes_nothing(fake_tree, tmp_path):
    root, app_dir = fake_tree
    db_path = tmp_path / "journal.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    orig_cpu = (app_dir / "cpu.max").read_bytes()
    orig_mem = (app_dir / "memory.high").read_bytes()

    recs = {
        "generated_at": time.time(),
        "items": [{
            "cgroup": cg,
            "cpu_max": "50000 100000",
            "memory_high": "70000000",
            "cpu_weight": None,
        }]
    }

    code = apply(db_path, recs, root, yes=False)
    assert code == 0
    # Files untouched
    assert (app_dir / "cpu.max").read_bytes() == orig_cpu
    assert (app_dir / "memory.high").read_bytes() == orig_mem
    # No journal DB created or table empty
    if db_path.is_file():
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("select count(*) from sqlite_master where type='table' and name='journal'")
            assert cur.fetchone()[0] == 0


def test_apply_and_revert_restores_exact_bytes(fake_tree, tmp_path):
    root, app_dir = fake_tree
    db_path = tmp_path / "journal.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    orig_cpu_bytes = b"100000 100000\n"
    orig_mem_bytes = b"max\n"
    (app_dir / "cpu.max").write_bytes(orig_cpu_bytes)
    (app_dir / "memory.high").write_bytes(orig_mem_bytes)

    recs = {
        "generated_at": time.time(),
        "items": [{
            "cgroup": cg,
            "cpu_max": "200000 100000",
            "memory_high": "150000000",
            "cpu_weight": 500,
        }]
    }

    # Apply
    code = apply(db_path, recs, root, yes=True)
    assert code == 0
    assert (app_dir / "cpu.max").read_text().strip() == "200000 100000"
    assert (app_dir / "memory.high").read_text().strip() == "150000000"

    # Journal check
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("select file, status from journal where batch = 1").fetchall()
        assert len(rows) >= 2
        for _, status in rows:
            assert status == "applied"

    # Revert
    rev_code = revert(db_path, root)
    assert rev_code == 0

    # Restores exact original bytes
    assert (app_dir / "cpu.max").read_bytes() == orig_cpu_bytes
    assert (app_dir / "memory.high").read_bytes() == orig_mem_bytes


def test_memory_clamp(fake_tree, tmp_path):
    root, app_dir = fake_tree
    db_path = tmp_path / "journal.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    # memory.current is 100,000,000 (~95.37 MiB)
    # 100,000,000 * 1.1 = 110,000,000
    # ceil_MiB(110,000,000) = 110,100,480 bytes (105 MiB)
    recs = {
        "generated_at": time.time(),
        "items": [{
            "cgroup": cg,
            "cpu_max": "max 100000",
            "memory_high": "70000000",  # below 1.1 * current
            "cpu_weight": None,
        }]
    }

    code = apply(db_path, recs, root, yes=True)
    assert code == 0

    applied_mem = (app_dir / "memory.high").read_text().strip()
    expected_clamped = str(int(math.ceil((100000000 * 1.1) / (1024 * 1024)) * 1024 * 1024))
    assert applied_mem == expected_clamped
    assert applied_mem == "110100480"


def test_execute_cli(fake_tree, tmp_path, capsys):
    root, app_dir = fake_tree
    db_path = tmp_path / "cli.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    recs = {
        "generated_at": time.time(),
        "items": [{
            "cgroup": cg,
            "cpu_max": "300000 100000",
            "memory_high": "150000000",
            "cpu_weight": None,
        }]
    }

    recs_file = tmp_path / "recs.json"
    recs_file.write_text(json.dumps(recs), encoding="utf-8")

    # Dry run CLI
    code = main(["apply", "--from", str(recs_file), "--root", str(root), "--db", str(db_path)])
    assert code == 0
    captured = capsys.readouterr()
    assert f"{cg}  cpu.max" in captured.out

    # Apply CLI
    code = main(["apply", "--from", str(recs_file), "--root", str(root), "--db", str(db_path), "--yes"])
    assert code == 0
    assert (app_dir / "cpu.max").read_text().strip() == "300000 100000"

    # Revert CLI
    code = main(["revert", "--root", str(root), "--db", str(db_path)])
    assert code == 0
    assert (app_dir / "cpu.max").read_text().strip() == "max 100000"


def test_dashboard_cli(monkeypatch):
    called = []
    fake_module = types.ModuleType("irm.dashboard")
    fake_module.serve = lambda port, db, reports, recs: called.append((port, db, reports, recs))
    monkeypatch.setitem(sys.modules, "irm.dashboard", fake_module)

    code = main(["dashboard", "--port", "9000"])
    assert code == 0
    assert len(called) == 1
    assert called[0][0] == 9000


def test_train_forecast_cli(monkeypatch):
    called = []
    fake_module = types.ModuleType("irm.forecast")
    fake_module.demo = lambda out_path, seed, epochs: called.append((out_path, seed, epochs))
    monkeypatch.setitem(sys.modules, "irm.forecast", fake_module)

    code = main(["train", "forecast", "--epochs", "3", "--seed", "42"])
    assert code == 0
    assert len(called) == 1
    assert called[0][1:] == (42, 3)


def test_evaluate_placement_cli(monkeypatch):
    called = []
    fake_module = types.ModuleType("irm.dqn")
    fake_module.demo = lambda out_path, seed, episodes: called.append((out_path, seed, episodes))
    monkeypatch.setitem(sys.modules, "irm.dqn", fake_module)

    code = main(["evaluate", "placement", "--episodes", "10", "--seed", "7"])
    assert code == 0
    assert len(called) == 1
    assert called[0][1:] == (7, 10)


def test_validate_allow_sanitization(tmp_path):
    root = tmp_path / "cgroup"
    service_dir = root / "system.slice" / "x.service"
    service_dir.mkdir(parents=True)
    (service_dir / "cgroup.controllers").write_text("cpu memory\n", encoding="utf-8")
    cg = "/system.slice/x.service"

    for bad in (["/"], [".."], ["../.."], ["relative"]):
        err = validate(root, cg, "cpu.max", "max 100000", allow=bad)
        assert err is not None
        assert "not strictly inside" in err

    err = validate(root, cg, "cpu.max", "max 100000", allow=["/system.slice/"])
    assert err is None


def test_validate_non_ascii_and_whitespace_numbers(fake_tree):
    root, app_dir = fake_tree
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"
    bad_values = ["²", "٥٠٠", " 5", "5\n"]

    for bad in bad_values:
        err_mem = validate(root, cg, "memory.high", bad)
        assert err_mem is not None and isinstance(err_mem, str)

        err_weight = validate(root, cg, "cpu.weight", bad)
        assert err_weight is not None and isinstance(err_weight, str)

        err_quota = validate(root, cg, "cpu.max", f"{bad} 100000")
        assert err_quota is not None and isinstance(err_quota, str)


def test_validate_controllers_as_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("os.getuid", lambda: 1000)
    root = tmp_path / "cgroup"
    app_dir = root / "user.slice" / "user-1000.slice" / "user@1000.service" / "app.slice"
    controllers = app_dir / "cgroup.controllers"
    controllers.mkdir(parents=True)
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"

    err = validate(root, cg, "cpu.max", "max 100000")
    assert err is not None
    assert "cgroup.controllers does not exist" in err


def test_apply_stale_plan(fake_tree, tmp_path):
    root, app_dir = fake_tree
    db_path = tmp_path / "stale.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"
    target_file = app_dir / "cpu.max"
    orig_cpu = target_file.read_text(encoding="utf-8")

    now = 1000000.0

    # 1. 20 min old -> return 1, no file written, no journal rows
    recs_20m = {
        "generated_at": now - 20 * 60,
        "items": [{
            "cgroup": cg,
            "cpu_max": "300000 100000",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    code = apply(db_path, recs_20m, root, yes=True, now=now, max_age_minutes=15.0)
    assert code == 1
    assert target_file.read_text(encoding="utf-8") == orig_cpu
    if db_path.is_file():
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("select count(*) from sqlite_master where type='table' and name='journal'")
            assert cur.fetchone()[0] == 0

    # 2. missing generated_at -> refused (return 1, no file written)
    recs_missing = {
        "items": [{
            "cgroup": cg,
            "cpu_max": "300000 100000",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    code = apply(db_path, recs_missing, root, yes=True, now=now, max_age_minutes=15.0)
    assert code == 1
    assert target_file.read_text(encoding="utf-8") == orig_cpu

    # 3. 1 min old -> applies
    recs_1m = {
        "generated_at": now - 60,
        "items": [{
            "cgroup": cg,
            "cpu_max": "300000 100000",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    code = apply(db_path, recs_1m, root, yes=True, now=now, max_age_minutes=15.0)
    assert code == 0
    assert target_file.read_text(encoding="utf-8").strip() == "300000 100000"

    # 4. max_age_minutes=None -> applies even if 20m old
    recs_old = {
        "generated_at": now - 3600,
        "items": [{
            "cgroup": cg,
            "cpu_max": "400000 100000",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    code = apply(db_path, recs_old, root, yes=True, now=now, max_age_minutes=None)
    assert code == 0
    assert target_file.read_text(encoding="utf-8").strip() == "400000 100000"

    # 5. ISO-8601 string support
    iso_str = datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat()
    recs_iso = {
        "generated_at": iso_str,
        "items": [{
            "cgroup": cg,
            "cpu_max": "500000 100000",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    code = apply(db_path, recs_iso, root, yes=True, now=now, max_age_minutes=15.0)
    assert code == 0
    assert target_file.read_text(encoding="utf-8").strip() == "500000 100000"


def test_revert_skips_when_value_changed(fake_tree, tmp_path, capsys):
    root, app_dir = fake_tree
    db_path = tmp_path / "revert_change.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"
    target_file = app_dir / "cpu.max"
    orig_val = "100000 100000\n"
    target_file.write_text(orig_val, encoding="utf-8")

    now = time.time()
    recs = {
        "generated_at": now,
        "items": [{
            "cgroup": cg,
            "cpu_max": "200000 100000",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    # Apply
    assert apply(db_path, recs, root, yes=True) == 0
    assert target_file.read_text(encoding="utf-8").strip() == "200000 100000"

    # Change file by hand
    target_file.write_text("700000 100000\n", encoding="utf-8")

    # Revert without force
    code = revert(db_path, root, force=False)
    assert code == 1
    # Value kept
    assert target_file.read_text(encoding="utf-8").strip() == "700000 100000"
    # Status in journal is revert_skipped:changed
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select status from journal where cgroup = ? and file = 'cpu.max'", (cg,)).fetchone()
        assert row is not None
        assert row[0] == "revert_skipped:changed"

    captured = capsys.readouterr()
    assert f"skipping {cg} cpu.max: current value 700000 100000 differs from applied 200000 100000 (use --force)" in captured.out

    # Revert with force=True restores
    code_force = revert(db_path, root, force=True)
    assert code_force == 0
    assert target_file.read_text(encoding="utf-8") == orig_val
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select status from journal where cgroup = ? and file = 'cpu.max'", (cg,)).fetchone()
        assert row[0] == "reverted"


def test_revert_normalization_cpu_max(fake_tree, tmp_path):
    root, app_dir = fake_tree
    db_path = tmp_path / "norm.db"
    cg = "/user.slice/user-1000.slice/user@1000.service/app.slice"
    target_file = app_dir / "cpu.max"
    orig_val = "50000 100000\n"
    target_file.write_text(orig_val, encoding="utf-8")

    now = time.time()
    recs = {
        "generated_at": now,
        "items": [{
            "cgroup": cg,
            "cpu_max": "max",
            "memory_high": None,
            "cpu_weight": None,
        }],
    }
    # Apply "max"
    assert apply(db_path, recs, root, yes=True) == 0

    # Kernel returns "max 100000\n" when reading cpu.max
    target_file.write_text("max 100000\n", encoding="utf-8")

    # Revert should recognize "max 100000" == "max" through normalization and restore
    code = revert(db_path, root, force=False)
    assert code == 0
    assert target_file.read_text(encoding="utf-8") == orig_val
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select status from journal where cgroup = ? and file = 'cpu.max'", (cg,)).fetchone()
        assert row[0] == "reverted"


