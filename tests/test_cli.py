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
