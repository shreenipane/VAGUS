# Prototype task P2 — Dashboard

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, then `TRD.md` §3, §4.4, §13. Read `irm/monitor.py` for the schema.

**Deadline: a faculty demo in 25 minutes.** Three other agents are writing `irm/recommend.py`, `irm/execute.py`,
`irm/cli.py`, `irm/forecast.py`, `irm/sim.py`, `irm/dqn.py` at the same time: never touch those files.

## Files to touch
- `irm/dashboard.py`, `irm/static/index.html`, `irm/static/app.js`, `irm/static/style.css`, `tests/test_dashboard.py`

## `irm/dashboard.py` — TRD §13
- `make_handler(db_path, reports_dir, recs_path, port)` returns a `BaseHTTPRequestHandler` subclass;
  `serve(port, db_path, reports_dir, recs_path)` runs `ThreadingHTTPServer(("127.0.0.1", port))` and prints the URL.
- Everything in TRD §13: GET only (405), `Host` check (421), fixed static map, security headers on every response,
  read-only SQLite per request, metric whitelist, `hours` 1–48, ≤ 2,000 points, JSON errors. A missing DB or file →
  empty lists / `null`, never a crash.
- `/api/reports` returns `{"overhead", "forecast", "placement"}` from `reports/*.json` (null when missing).

## UI (one page, no external resources, no inline script or style, text via `textContent` only)
Title "Intelligent Resource Manager" with subtitle "Monitor → Analyse → Plan → Execute". Four cards in a responsive
grid, refreshed every 5 s:
1. **Monitor** — host CPU cores, memory GiB, CPU PSI from the latest `host` row; table of the top 12 leaf cgroups by
   `cpu_cores` (short name = last path component; full path in `title`), columns CPU cores, memory MiB, throttled %,
   PSI. Clicking a row selects it.
2. **Live chart** — inline SVG line chart of `cpu_cores` for the selected cgroup over the last hour, with a second line
   for `throttled_ratio × max(cpu)`; axis labels; legend. Default selection: the busiest cgroup.
3. **Plan / Execute** — `/api/recommendations` items: cgroup, source, peak cores, `cpu.max`, `memory.high`, reason;
   pairs below. Placeholder text "Run: irm recommend" when empty.
4. **Analyse / Research** — from `/api/reports`: forecast table (model, pinball, coverage, mean under-prediction) from
   `forecast.test_all` plus `forecast.test_arima_subset.arima`; placement table (policy, energy kWh, SLA overload %,
   migrations, mean active hosts) from `placement`; overhead line "Monitor overhead: X% of one core (N cgroups)".
   If `forecast.example` exists, a small SVG chart of `history` + `actual` + `lstm` + `last_window`.
Clean look: system font stack, CSS variables for colours with a `prefers-color-scheme: dark` variant, cards with
subtle borders, tabular numbers.

## Tests (fast; start the server on port 0 in a thread against a temp DB with a few rows)
Bad `Host` → 421; POST → 405; unknown metric → 400; `/api/series` returns points; `/` has the CSP header; missing
reports → nulls.

Run `/home/shreenipane/.local/bin/irm-test -q` until green. Print the `AGENTS.md` §6 summary.
