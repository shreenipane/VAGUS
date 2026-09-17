import sys
import types
from pathlib import Path
import pytest
import irm
from irm.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "irm 0.1.0"


def test_no_arguments(capsys):
    code = main([])
    assert code == 2
    captured = capsys.readouterr()
    assert "usage: irm" in captured.out


def test_home():
    assert isinstance(irm.HOME, Path)
    assert (irm.HOME / "pyproject.toml").is_file()


def test_control_cli(monkeypatch):
    called = []
    fake_module = types.ModuleType("irm.control")

    def fake_run(db, root, proc, protect, only, **kwargs):
        called.append((db, root, proc, protect, only, kwargs))

    fake_module.run = fake_run
    monkeypatch.setitem(sys.modules, "irm.control", fake_module)

    code = main([
        "control",
        "--protect", "/user.slice/p.scope",
        "--only", "/user.slice/o1.scope", "/user.slice/o2.scope",
        "--interval", "45",
        "--settle", "20",
        "--psi-margin", "4.0",
        "--iterations", "5",
        "--min-samples", "15",
        "--hours", "2.0",
    ])
    assert code == 0
    assert len(called) == 1
    db, root, proc, protect, only, kwargs = called[0]
    assert protect == ["/user.slice/p.scope"]
    assert only == ["/user.slice/o1.scope", "/user.slice/o2.scope"]
    assert kwargs["interval"] == 45.0
    assert kwargs["settle"] == 20.0
    assert kwargs["psi_margin"] == 4.0
    assert kwargs["max_iterations"] == 5
    assert kwargs["min_samples"] == 15
    assert kwargs["hours"] == 2.0


def test_control_cli_defaults(monkeypatch):
    called = []
    fake_module = types.ModuleType("irm.control")
    fake_module.run = lambda db, root, proc, protect, only, **kwargs: called.append((db, root, proc, protect, only, kwargs))
    monkeypatch.setitem(sys.modules, "irm.control", fake_module)

    code = main([
        "control",
        "--protect", "/p.scope",
        "--only", "/o.scope",
    ])
    assert code == 0
    assert len(called) == 1
    db, root, proc, protect, only, kwargs = called[0]
    assert root == "/sys/fs/cgroup"
    assert proc == "/proc"
    assert kwargs["interval"] == 60.0
    assert kwargs["settle"] == 30.0
    assert kwargs["psi_margin"] == 5.0
    assert kwargs["max_iterations"] is None
    assert kwargs["min_samples"] == 12
    assert kwargs["hours"] == 1.0


def test_experiment_slo_cli(monkeypatch, tmp_path):
    called = []
    fake_module = types.ModuleType("irm.experiment")
    fake_module.run_slo = lambda out, **kwargs: called.append((out, kwargs))
    monkeypatch.setitem(sys.modules, "irm.experiment", fake_module)

    code = main([
        "experiment", "slo",
        "--arms", "A,C",
        "--raw-dir", str(tmp_path / "raw"),
        "--minutes", "1.5",
        "--reps", "2",
        "--rate", "150",
    ])
    assert code == 0
    assert len(called) == 1
    out, kwargs = called[0]
    assert kwargs["arms"] == ["A", "C"]
    assert kwargs["raw_dir"] == tmp_path / "raw"
    assert kwargs["minutes"] == 1.5
    assert kwargs["reps"] == 2
    assert kwargs["rate"] == 150.0


def test_apply_cli_max_age_minutes(monkeypatch, tmp_path):
    called = []
    fake_module = types.ModuleType("irm.execute")
    fake_module.apply = lambda db_path, recs, root, allow, yes, max_age_minutes: (
        called.append(max_age_minutes) or 0
    )
    fake_module.validate = lambda *args, **kwargs: None
    fake_module.revert = lambda *args, **kwargs: 0
    monkeypatch.setitem(sys.modules, "irm.execute", fake_module)

    recs_file = tmp_path / "recs.json"
    recs_file.write_text("{}", encoding="utf-8")

    # default: 15.0
    main(["apply", "--from", str(recs_file)])
    assert called[-1] == 15.0

    # 0 means None (no check)
    main(["apply", "--from", str(recs_file), "--max-age-minutes", "0"])
    assert called[-1] is None

    # custom
    main(["apply", "--from", str(recs_file), "--max-age-minutes", "25.5"])
    assert called[-1] == 25.5


def test_revert_cli_force(monkeypatch):
    called = []
    fake_module = types.ModuleType("irm.execute")
    fake_module.revert = lambda db_path, root, batch, force: (
        called.append(force) or 0
    )
    fake_module.apply = lambda *args, **kwargs: 0
    fake_module.validate = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "irm.execute", fake_module)

    # default force is False
    main(["revert"])
    assert called[-1] is False

    # --force sets force to True
    main(["revert", "--force"])
    assert called[-1] is True
