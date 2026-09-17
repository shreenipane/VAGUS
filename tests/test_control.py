from pathlib import Path
import pytest
from irm.control import default_read_psi, run


def test_default_read_psi(tmp_path):
    cg = tmp_path / "app.slice"
    cg.mkdir(parents=True, exist_ok=True)
    pressure_file = cg / "cpu.pressure"
    pressure_file.write_text(
        "some avg10=2.34 avg60=1.50 avg300=0.80 total=1000\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        encoding="utf-8",
    )

    val = default_read_psi(tmp_path, "/app.slice")
    assert val == pytest.approx(2.34)

    # Missing file returns None
    assert default_read_psi(tmp_path, "/nonexistent.slice") is None


def test_psi_rising_rollback(tmp_path):
    """PSI rising beyond the margin -> exactly one revert and a rollback log."""
    logs = []
    reverts = []
    applies = []

    cg_dir = tmp_path / "app.slice"
    cg_dir.mkdir(parents=True, exist_ok=True)
    (cg_dir / "cpu.max").write_text("50000 100000", encoding="utf-8")

    plan = {
        "items": [
            {
                "cgroup": "/app.slice",
                "cpu_max": "100000 100000",
                "memory_high": None,
                "cpu_weight": None,
            }
        ]
    }

    psi_read_count = 0

    def fake_read_psi(root, cgroup):
        nonlocal psi_read_count
        psi_read_count += 1
        # before is 1.0, after is 10.0 -> diff 9.0 > psi_margin 5.0
        return 1.0 if psi_read_count == 1 else 10.0

    def fake_apply(db_path, recs, root, yes=True):
        applies.append((db_path, recs, root, yes))
        return 0

    def fake_revert(db_path, root):
        reverts.append((db_path, root))
        return 0

    ret = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        psi_margin=5.0,
        max_iterations=1,
        recommend_fn=lambda *args, **kwargs: plan,
        apply_fn=fake_apply,
        revert_fn=fake_revert,
        read_psi=fake_read_psi,
        sleep=lambda s: None,
        log=logs.append,
    )

    assert ret == 0
    assert len(applies) == 1
    # Exactly one revert called (the rollback); not reverted again on exit
    assert len(reverts) == 1
    assert any("rollback" in m and "1.0" in m and "10.0" in m for m in logs)


def test_stable_psi_no_rollback_revert(tmp_path):
    """Stable PSI -> no revert during the loop iteration (kept)."""
    logs = []
    reverts = []
    reverts_at_sleep = []

    cg_dir = tmp_path / "app.slice"
    cg_dir.mkdir(parents=True, exist_ok=True)
    (cg_dir / "cpu.max").write_text("50000 100000", encoding="utf-8")

    plan = {
        "items": [
            {
                "cgroup": "/app.slice",
                "cpu_max": "100000 100000",
                "memory_high": None,
                "cpu_weight": None,
            }
        ]
    }

    psi_read_count = 0

    def fake_read_psi(root, cgroup):
        nonlocal psi_read_count
        psi_read_count += 1
        # before is 1.0, after is 2.0 -> diff 1.0 <= psi_margin 5.0
        return 1.0 if psi_read_count == 1 else 2.0

    def fake_sleep(secs):
        reverts_at_sleep.append(len(reverts))

    def fake_apply(db_path, recs, root, yes=True):
        return 0

    def fake_revert(db_path, root):
        reverts.append((db_path, root))
        return 0

    ret = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        psi_margin=5.0,
        max_iterations=1,
        recommend_fn=lambda *args, **kwargs: plan,
        apply_fn=fake_apply,
        revert_fn=fake_revert,
        read_psi=fake_read_psi,
        sleep=fake_sleep,
        log=logs.append,
    )

    assert ret == 0
    # During sleep (both settle and interval-settle), no rollback revert occurred
    assert reverts_at_sleep == [0, 0]
    assert any("kept" in m and "1.0" in m and "2.0" in m for m in logs)
    assert not any("rollback" in m for m in logs)
    # The batch is reverted on exit
    assert len(reverts) == 1


def test_unchanged_plan_apply_not_called(tmp_path):
    """An unchanged plan -> apply_fn not called."""
    logs = []
    applies = []
    reverts = []

    cg_dir = tmp_path / "app.slice"
    cg_dir.mkdir(parents=True, exist_ok=True)
    # File already has "max 100000"
    (cg_dir / "cpu.max").write_text("max 100000\n", encoding="utf-8")

    # Plan proposes cpu_max = "max", which normalizes to "max 100000"
    plan = {
        "items": [
            {
                "cgroup": "/app.slice",
                "cpu_max": "max",
                "memory_high": None,
                "cpu_weight": None,
            }
        ]
    }

    ret = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        max_iterations=1,
        recommend_fn=lambda *args, **kwargs: plan,
        apply_fn=lambda *args, **kwargs: applies.append(1) or 0,
        revert_fn=lambda *args, **kwargs: reverts.append(1) or 0,
        sleep=lambda s: None,
        log=logs.append,
    )

    assert ret == 0
    assert len(applies) == 0
    assert len(reverts) == 0
    assert any("iteration 1: no change" in m for m in logs)


def test_exit_after_max_iterations_reverts_kept_batch(tmp_path):
    """Exiting after max_iterations=2 with one kept batch -> that batch reverted on exit."""
    logs = []
    applies = []
    reverts = []

    cg_dir = tmp_path / "app.slice"
    cg_dir.mkdir(parents=True, exist_ok=True)
    (cg_dir / "cpu.max").write_text("50000 100000", encoding="utf-8")

    iter_count = 0

    def fake_recommend(*args, **kwargs):
        nonlocal iter_count
        iter_count += 1
        if iter_count == 1:
            return {
                "items": [
                    {
                        "cgroup": "/app.slice",
                        "cpu_max": "100000 100000",
                        "memory_high": None,
                        "cpu_weight": None,
                    }
                ]
            }
        # Iteration 2: no change
        return {"items": []}

    ret = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        psi_margin=5.0,
        max_iterations=2,
        recommend_fn=fake_recommend,
        apply_fn=lambda *args, **kwargs: applies.append(1) or 0,
        revert_fn=lambda *args, **kwargs: reverts.append(1) or 0,
        read_psi=lambda root, cg: 1.0,  # stable PSI
        sleep=lambda s: None,
        log=logs.append,
    )

    assert ret == 0
    assert len(applies) == 1
    assert any("iteration 1: kept" in m for m in logs)
    assert any("iteration 2: no change" in m for m in logs)
    # The one kept batch is reverted on exit
    assert len(reverts) == 1


def test_keyboard_interrupt_reverts_applied_batches(tmp_path):
    """KeyboardInterrupt from sleep -> applied batches reverted."""
    logs = []
    applies = []
    reverts = []

    cg_dir = tmp_path / "app.slice"
    cg_dir.mkdir(parents=True, exist_ok=True)
    (cg_dir / "cpu.max").write_text("50000 100000", encoding="utf-8")

    plan = {
        "items": [
            {
                "cgroup": "/app.slice",
                "cpu_max": "100000 100000",
                "memory_high": None,
                "cpu_weight": None,
            }
        ]
    }

    sleep_count = 0

    def fake_sleep(secs):
        nonlocal sleep_count
        sleep_count += 1
        # Raise KeyboardInterrupt on second sleep (interval - settle)
        if sleep_count == 2:
            raise KeyboardInterrupt()

    ret = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        psi_margin=5.0,
        max_iterations=None,
        recommend_fn=lambda *args, **kwargs: plan,
        apply_fn=lambda *args, **kwargs: applies.append(1) or 0,
        revert_fn=lambda *args, **kwargs: reverts.append(1) or 0,
        read_psi=lambda root, cg: 1.0,
        sleep=fake_sleep,
        log=logs.append,
    )

    assert ret == 0
    assert len(applies) == 1
    # Batch was reverted in finally block
    assert len(reverts) == 1


def test_failure_return_code(tmp_path):
    """Apply or revert failure causes run to return 1."""
    cg_dir = tmp_path / "app.slice"
    cg_dir.mkdir(parents=True, exist_ok=True)
    (cg_dir / "cpu.max").write_text("50000 100000", encoding="utf-8")

    plan = {
        "items": [
            {
                "cgroup": "/app.slice",
                "cpu_max": "100000 100000",
                "memory_high": None,
                "cpu_weight": None,
            }
        ]
    }

    # Apply returns non-zero -> return 1
    ret_apply_fail = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        max_iterations=1,
        recommend_fn=lambda *args, **kwargs: plan,
        apply_fn=lambda *args, **kwargs: 1,  # fails
        revert_fn=lambda *args, **kwargs: 0,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    assert ret_apply_fail == 1

    # Revert in rollback returns non-zero -> return 1
    psi_reads = 0

    def fake_read_psi(root, cg):
        nonlocal psi_reads
        psi_reads += 1
        return 1.0 if psi_reads == 1 else 10.0

    ret_revert_fail = run(
        db_path=tmp_path / "test.db",
        root=tmp_path,
        proc=tmp_path / "proc",
        protect=["/app.slice"],
        only=["/app.slice"],
        psi_margin=5.0,
        max_iterations=1,
        recommend_fn=lambda *args, **kwargs: plan,
        apply_fn=lambda *args, **kwargs: 0,
        revert_fn=lambda *args, **kwargs: 1,  # fails
        read_psi=fake_read_psi,
        sleep=lambda s: None,
        log=lambda m: None,
    )
    assert ret_revert_fail == 1
