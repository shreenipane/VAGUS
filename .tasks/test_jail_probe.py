# Architect-owned jail probe (SECURITY.md T2/T3). Re-run at every GATE: irm-test .tasks/test_jail_probe.py -q
import os, pathlib, socket, pytest

H = pathlib.Path("/home/shreenipane")
REPO = H / "Heavy Coding/Projects/intelligent-resource-manager"


@pytest.mark.parametrize("p", [H / "irm-sentinel.txt", H / ".ssh", H / ".gemini", H / ".claude",
                               H / ".local/share/irm/git", H / "Heavy Coding/Projects/Verdict",
                               pathlib.Path("/run/user/1000/bus"), pathlib.Path("/run/dbus/system_bus_socket")])
def test_hidden(p):
    assert not p.exists()


def test_env_clean():
    assert set(os.environ) - {"PYTEST_CURRENT_TEST", "PYTEST_VERSION"} <= {"HOME", "PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PWD"}


def test_no_network():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 443), timeout=3)


def test_no_host_pids():
    assert pathlib.Path("/proc/1/comm").read_text().strip() != "systemd"


@pytest.mark.parametrize("p", [REPO / "probe.txt", REPO / "data/probe.txt", H / ".local/share/irm/venv/probe",
                               pathlib.Path("/usr/probe"), pathlib.Path("/etc/probe"),
                               pathlib.Path("/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/cpu.weight")])
def test_write_denied(p):
    with pytest.raises(OSError):
        p.write_text("100")


def test_code_dir_writable_and_cgroupfs_readable():
    p = REPO / "tests/probe_w.txt"
    p.write_text("x")
    p.unlink()
    assert pathlib.Path("/sys/fs/cgroup/cgroup.controllers").read_text()
    assert pathlib.Path("/proc/stat").read_text().startswith("cpu ")
