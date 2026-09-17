import asyncio
import json
from pathlib import Path
import subprocess
import threading
import time
import numpy as np
import pytest

from irm.cli import main
from irm.experiment import (
    calibrate_iters,
    main as exp_main,
    parse_cgroup,
    percentiles,
    rotation,
    run_cpuhog,
    run_loadgen,
    run_service,
    run_slo,
    summarize,
    violation_rate,
    window_p99,
)


def test_rotation():
    assert rotation(4) == [
        ["A", "B", "C"],
        ["B", "C", "A"],
        ["C", "A", "B"],
        ["A", "B", "C"],
    ]
    assert rotation(1) == [["A", "B", "C"]]
    assert rotation(3) == [
        ["A", "B", "C"],
        ["B", "C", "A"],
        ["C", "A", "B"],
    ]
    assert rotation(0) == []


def test_percentiles_1_to_100():
    lat = list(range(1, 101))
    p = percentiles(lat)
    assert p["p50_ms"] == pytest.approx(50.5)
    assert p["p95_ms"] == pytest.approx(95.05)
    assert p["p99_ms"] == pytest.approx(99.01)


def test_percentiles_empty():
    p = percentiles([])
    assert p["p50_ms"] == 0.0
    assert p["p95_ms"] == 0.0
    assert p["p99_ms"] == 0.0


def test_window_p99_groups_by_second():
    # Window 0: seconds [0, 1)
    # Window 2: seconds [2, 3) (window 1 is empty and skipped)
    records = [
        [0.1, 10.0],
        [0.2, 20.0],
        [0.9, 100.0],
        [2.1, 50.0],
        [2.8, 60.0],
    ]
    w = window_p99(records, window_s=1.0)
    assert len(w) == 2
    assert w[0] == pytest.approx(np.percentile([10.0, 20.0, 100.0], 99))
    assert w[1] == pytest.approx(np.percentile([50.0, 60.0], 99))


def test_violation_rate():
    p99s = [10.0, 20.0, 30.0, 40.0]
    assert violation_rate(p99s, target_ms=25.0) == pytest.approx(0.5)
    assert violation_rate([], target_ms=25.0) == 0.0
    assert violation_rate([5.0, 10.0], target_ms=20.0) == 0.0
    assert violation_rate([30.0, 40.0], target_ms=20.0) == 1.0


def test_parse_cgroup():
    sample = (
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/irm-exp-service-12345678.scope\n"
    )
    assert (
        parse_cgroup(sample)
        == "/user.slice/user-1000.slice/user@1000.service/app.slice/irm-exp-service-12345678.scope"
    )

    sample_multi = (
        "1:cpu:/other\n"
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/irm-exp-cpuhog-abcdef01.scope\n"
    )
    assert (
        parse_cgroup(sample_multi)
        == "/user.slice/user-1000.slice/user@1000.service/app.slice/irm-exp-cpuhog-abcdef01.scope"
    )


def test_summarize_hand_made_runs():
    runs = {
        "A": [
            {"records": [[0.1, 10.0], [0.5, 20.0], [1.2, 30.0]], "errors": 0, "seconds": 2.0},
            {"records": [[0.2, 15.0], [1.5, 35.0]], "errors": 1, "seconds": 2.0},
        ],
        "B": [
            {"records": [[0.1, 50.0], [0.5, 60.0]], "errors": 0, "seconds": 1.0, "cpuhog_ips": 1000.0},
            {"records": [[0.2, 55.0], [0.8, 65.0]], "errors": 0, "seconds": 1.0, "cpuhog_ips": 1200.0},
        ],
        "C": [
            {"records": [[0.1, 20.0], [0.5, 25.0]], "errors": 0, "seconds": 1.0, "cpuhog_ips": 500.0},
        ],
    }
    res = summarize(runs, target_ms=25.0)
    assert "A" in res and "B" in res and "C" in res

    # Condition A checks
    assert res["A"]["errors"] == 1
    assert res["A"]["rps"] == pytest.approx(5.0 / 4.0)
    assert res["A"]["cpuhog_ips"] is None
    assert "p50_ms" in res["A"]
    assert "p95_ms" in res["A"]
    assert "p99_ms" in res["A"]
    assert res["A"]["rep_p99_ms"]["min"] <= res["A"]["rep_p99_ms"]["mean"] <= res["A"]["rep_p99_ms"]["max"]
    assert 0.0 <= res["A"]["violation_rate"] <= 1.0

    # Condition B checks
    assert res["B"]["cpuhog_ips"] == pytest.approx(1100.0)
    assert res["B"]["errors"] == 0
    assert res["B"]["rps"] == pytest.approx(4.0 / 2.0)

    # Condition C checks
    assert res["C"]["cpuhog_ips"] == pytest.approx(500.0)
    assert res["C"]["errors"] == 0


def test_calibrate_iters():
    iters = calibrate_iters()
    assert isinstance(iters, int)
    assert iters >= 1


def test_cpuhog_smoke(tmp_path):
    out_file = tmp_path / "cpu.json"
    res = run_cpuhog(procs=1, seconds=0.05, out=out_file)
    assert out_file.is_file()
    assert "iters_per_s" in res
    assert res["iters_per_s"] >= 0.0


def test_loopback_smoke():
    ready_event = threading.Event()
    stop_event = threading.Event()
    server_port: list[int] = []

    def run_server_thread():
        def on_ready(p):
            server_port.append(p)
            ready_event.set()

        asyncio.run(run_service(port=0, ready_fn=on_ready, stop_event=stop_event))

    t = threading.Thread(target=run_server_thread, daemon=True)
    t.start()
    try:
        assert ready_event.wait(timeout=5.0), "Service did not start in time"
        port = server_port[0]
        result = asyncio.run(run_loadgen(port=port, rate=50.0, seconds=1.0, seed=42))
        assert result["errors"] == 0
        assert len(result["records"]) >= 30
    finally:
        stop_event.set()
        t.join(timeout=3.0)


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["experiment", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "slo" in captured.out

    with pytest.raises(SystemExit) as exc_info:
        main(["evaluate", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "study" in captured.out


@pytest.mark.live
def test_run_slo_live(tmp_path):
    out_file = tmp_path / "slo.json"
    res = run_slo(out_file, minutes=0.1, reps=1, rate=20)
    assert out_file.is_file()
    assert "slo_target_ms" in res
    assert "conditions" in res


def test_run_slo_revert_on_apply_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("irm.experiment.rotation", lambda reps: [["C"]])

    class DummyProc:
        pid = 1234

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: DummyProc())
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("irm.experiment.wait_for_cgroup", lambda pid, unit: f"/user.slice/{unit}.scope")
    monkeypatch.setattr("irm.experiment.wait_for_port", lambda port, timeout=10.0: None)
    monkeypatch.setattr("irm.monitor.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("irm.recommend.recommend", lambda *args, **kwargs: {"items": []})
    monkeypatch.setattr("time.sleep", lambda *args: None)

    def failing_apply(*args, **kwargs):
        raise RuntimeError("apply error")

    revert_calls = []
    monkeypatch.setattr("irm.execute.apply", failing_apply)
    monkeypatch.setattr("irm.execute.revert", lambda db, root: revert_calls.append((db, root)))

    out_file = tmp_path / "slo.json"
    with pytest.raises(RuntimeError, match="apply error"):
        run_slo(out_file, minutes=0.1, reps=1)

    assert len(revert_calls) >= 1


def test_cpuhog_delay(tmp_path):
    out_file = tmp_path / "cpu_delay.json"
    t0 = time.monotonic()
    rc = exp_main(["cpuhog", "--delay", "0.2", "--seconds", "0.5", "--procs", "1", "--out", str(out_file)])
    elapsed = time.monotonic() - t0
    assert rc == 0
    assert elapsed >= 0.7
    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["iters_per_s"] > 0.0


def test_cleanup_resets_failed_units(monkeypatch, tmp_path):
    monkeypatch.setattr("irm.experiment.rotation", lambda reps: [["B", "C"]])

    class DummyProc:
        pid = 1234

        def wait(self, timeout=None):
            return 0

    subprocess_calls = []

    def fake_run(cmd, *args, **kwargs):
        subprocess_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("irm.experiment.wait_for_cgroup", lambda pid, unit: f"/user.slice/{unit}.scope")
    monkeypatch.setattr("irm.experiment.wait_for_port", lambda port, timeout=10.0: None)
    monkeypatch.setattr("irm.monitor.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("irm.recommend.recommend", lambda *args, **kwargs: {"items": []})
    monkeypatch.setattr("irm.execute.apply", lambda *args, **kwargs: None)
    monkeypatch.setattr("irm.execute.revert", lambda *args, **kwargs: None)
    monkeypatch.setattr("time.sleep", lambda *args: None)

    def fake_popen(cmd, *args, **kwargs):
        if "loadgen" in cmd:
            out_idx = cmd.index("--out") + 1
            Path(cmd[out_idx]).write_text(json.dumps({"records": [[0.1, 10.0]], "errors": 0}))
        elif "cpuhog" in cmd:
            out_idx = cmd.index("--out") + 1
            Path(cmd[out_idx]).write_text(json.dumps({"iters_per_s": 5000.0}))
        return DummyProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    out_file = tmp_path / "slo.json"
    res = run_slo(out_file, minutes=0.1, reps=1, rate=20)

    assert res["conditions"]["C"]["cpuhog_ips"] == pytest.approx(5000.0)

    stop_units = [cmd[3] for cmd in subprocess_calls if cmd[:3] == ["systemctl", "--user", "stop"]]
    reset_units = [cmd[3] for cmd in subprocess_calls if cmd[:3] == ["systemctl", "--user", "reset-failed"]]

    assert len(stop_units) > 0
    assert len(reset_units) > 0
    assert set(stop_units) == set(reset_units)
    for i, cmd in enumerate(subprocess_calls[:-1]):
        if cmd[:3] == ["systemctl", "--user", "stop"]:
            assert subprocess_calls[i + 1] == ["systemctl", "--user", "reset-failed", cmd[3]]


