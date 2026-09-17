from pathlib import Path
import pytest


def make_cgroup(root: str | Path, name: str, **files: str) -> Path:
    """Create a cgroup directory under root and write each file.

    Maps keyword arguments by replacing the first '_' with '.',
    e.g. cpu_stat -> cpu.stat, memory_current -> memory.current.
    """
    clean_name = name.lstrip("/")
    cg_dir = Path(root) / clean_name
    cg_dir.mkdir(parents=True, exist_ok=True)
    for key, text in files.items():
        fname = key.replace("_", ".", 1)
        file_path = cg_dir / fname
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(text, encoding="utf-8")
    return cg_dir


def make_proc(
    proc: str | Path,
    stat: str,
    meminfo: str,
    cpu_psi: str,
    mem_psi: str,
    io_psi: str,
) -> Path:
    """Create a fixture /proc tree with stat, meminfo, and pressure files."""
    proc_path = Path(proc)
    proc_path.mkdir(parents=True, exist_ok=True)
    (proc_path / "pressure").mkdir(parents=True, exist_ok=True)

    (proc_path / "stat").write_text(stat, encoding="utf-8")
    (proc_path / "meminfo").write_text(meminfo, encoding="utf-8")
    (proc_path / "pressure" / "cpu").write_text(cpu_psi, encoding="utf-8")
    (proc_path / "pressure" / "memory").write_text(mem_psi, encoding="utf-8")
    (proc_path / "pressure" / "io").write_text(io_psi, encoding="utf-8")
    return proc_path


@pytest.fixture(name="make_cgroup")
def _make_cgroup_fixture():
    return make_cgroup


@pytest.fixture(name="make_proc")
def _make_proc_fixture():
    return make_proc
