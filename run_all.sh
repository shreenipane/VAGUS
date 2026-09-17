#!/usr/bin/env bash
# Runs the whole test suite, then makes sure the live monitor and the dashboard are running, and opens the dashboard.
#   ./run_all.sh              tests + dashboard on http://127.0.0.1:8765
#   PORT=8800 ./run_all.sh    dashboard on another port
# The exit code is the test suite's. Monitor and dashboard keep running in the background; stop them with:
#   pkill -f 'irm (monitor|dashboard)'
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
URL="http://127.0.0.1:$PORT"
VENV="${UV_PROJECT_ENVIRONMENT:-$HOME/.local/share/irm/venv}"
IRM="$VENV/bin/irm"
export PYTHONPYCACHEPREFIX="$HOME/.cache/irm-pycache"
mkdir -p data

echo "== Running tests (about 2–3 minutes)"
./run_tests.sh -q
status=$?

if ! pgrep -f "bin/irm monitor" >/dev/null; then
  echo "== Starting monitor: telemetry every 5 s into data/irm.db (log: data/monitor.log)"
  nohup "$IRM" monitor > data/monitor.log 2>&1 &
fi

if ! curl -s -o /dev/null "$URL/"; then
  echo "== Starting dashboard (log: data/dashboard.log)"
  nohup "$IRM" dashboard --port "$PORT" > data/dashboard.log 2>&1 &
  for _ in $(seq 40); do curl -s -o /dev/null "$URL/" && break; sleep 0.25; done
fi

if curl -s -o /dev/null "$URL/"; then
  echo "== Dashboard: $URL"
  xdg-open "$URL" >/dev/null 2>&1 || echo "Open $URL in your browser."
else
  echo "== Dashboard did not start; see data/dashboard.log"
fi

if [ "$status" -eq 0 ]; then echo "== Tests passed"; else echo "== Tests FAILED (exit $status)"; fi
echo "Stop background processes: pkill -f 'irm (monitor|dashboard)'"
exit "$status"
