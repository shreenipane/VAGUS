#!/usr/bin/env bash
# Runs the Intelligent Resource Manager test suite directly with pytest (no bubblewrap jail).
#   ./run_tests.sh                          whole suite (tests marked `live` are skipped)
#   ./run_tests.sh tests/test_monitor.py    one file; any pytest arguments work, e.g. -x -v -k recommend
# The coding agent keeps using the jailed `irm-test`; this is for humans running reviewed code.
set -euo pipefail
cd "$(dirname "$0")"

VENV="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/share/irm/venv}"
if [ ! -x "$VENV/bin/python" ]; then
  echo "No virtualenv at $VENV; creating it with uv sync..."
  UV_PROJECT_ENVIRONMENT="$VENV" uv sync
fi

# Bytecode goes outside the repository, so files in __pycache__/ are never trusted (SECURITY.md T3).
export PYTHONPYCACHEPREFIX="$HOME/.cache/irm-pycache"
exec "$VENV/bin/python" -m pytest -m "not live" -p no:cacheprovider "$@"
