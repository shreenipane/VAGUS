# Task 0 — CLI scaffold

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Paths below are relative
to it; pass absolute paths to your file tools.

Read and follow `AGENTS.md`, then `TRD.md` §1, §2, §3, §16.

## Goal
The smallest entry point that proves the toolchain: `python -m irm --version` and the `irm` console script print the
version, and the test suite runs in the jail.

## Files to touch
- `irm/__init__.py` — currently empty. Add `__version__ = "0.1.0"` and `HOME` exactly as TRD §3 defines it.
- `irm/__main__.py` (create) — calls `irm.cli.main()`.
- `irm/cli.py` (create) — `main(argv=None)`: an `argparse` parser with `prog="irm"` and `--version` (prints
  `irm 0.1.0`, exit 0). Subparsers are added by later tasks; do not add any now. With no arguments, print help and
  return exit code 2.
- `tests/conftest.py` (create) — empty except a module docstring saying fixture builders for cgroup and `/proc` trees
  will live here (TRD §16).
- `tests/test_cli.py` (create).

## Tests to write (`tests/test_cli.py`)
- `--version` prints `irm 0.1.0` and exits 0 (use `pytest.raises(SystemExit)` and `capsys`).
- No arguments → exit code 2.
- `irm.HOME` is the directory that contains `pyproject.toml`.

## Run
`/home/shreenipane/.local/bin/irm-test -q` until green.

## Do not touch
Everything else.

## Final summary
Print the block from `AGENTS.md` §6.
