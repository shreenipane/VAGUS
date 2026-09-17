import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_MAP: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)

METRIC_WHITELIST = frozenset((
    "cpu_cores",
    "throttled_ratio",
    "mem_bytes",
    "io_rbps",
    "io_wbps",
    "cpu_psi",
    "mem_psi",
    "io_psi",
    "pids",
    "softirq_cores",
    "netrx_attrib_cores",
    "netrx_blamed_cores",
    "netrx_unattrib_cores",
))


def make_handler(
    db_path: str | Path,
    reports_dir: str | Path,
    recs_path: str | Path,
    port: int = 8765,
) -> type[BaseHTTPRequestHandler]:
    resolved_db = Path(db_path)
    resolved_reports = Path(reports_dir)
    resolved_recs = Path(recs_path)

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default server access logging
            pass

        def _send_response_data(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for header, val in SECURITY_HEADERS:
                self.send_header(header, val)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, payload: Any) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send_response_data(code, "application/json; charset=utf-8", body)

        def _check_host(self) -> bool:
            host = self.headers.get("Host", "")
            allowed = set()
            server_port = getattr(getattr(self, "server", None), "server_port", None)
            for p in (port, server_port):
                if p:
                    allowed.add(f"127.0.0.1:{p}")
                    allowed.add(f"localhost:{p}")
                    if p == 80:
                        allowed.add("127.0.0.1")
                        allowed.add("localhost")
            if host not in allowed:
                self._send_json(421, {"error": "Misdirected Request"})
                return False
            return True

        def do_POST(self) -> None:
            if not self._check_host():
                return
            self._send_json(405, {"error": "Method Not Allowed"})

        def do_PUT(self) -> None:
            if not self._check_host():
                return
            self._send_json(405, {"error": "Method Not Allowed"})

        def do_DELETE(self) -> None:
            if not self._check_host():
                return
            self._send_json(405, {"error": "Method Not Allowed"})

        def do_HEAD(self) -> None:
            if not self._check_host():
                return
            self._send_json(405, {"error": "Method Not Allowed"})

        def do_PATCH(self) -> None:
            if not self._check_host():
                return
            self._send_json(405, {"error": "Method Not Allowed"})

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            if code == 501:
                self._send_json(405, {"error": "Method Not Allowed"})
            else:
                self._send_json(code, {"error": message or str(code)})

        def _get_db(self) -> sqlite3.Connection | None:
            if not resolved_db.is_file():
                return None
            try:
                uri = resolved_db.resolve().as_uri() + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True)
                conn.row_factory = sqlite3.Row
                return conn
            except (sqlite3.OperationalError, OSError):
                return None

        def do_GET(self) -> None:
            if not self._check_host():
                return

            parsed = urlparse(self.path)
            path = parsed.path

            if path in STATIC_MAP:
                fname, ctype = STATIC_MAP[path]
                fpath = STATIC_DIR / fname
                if fpath.is_file():
                    try:
                        self._send_response_data(200, ctype, fpath.read_bytes())
                    except OSError:
                        self._send_json(500, {"error": "Failed to read static file"})
                else:
                    self._send_json(404, {"error": "Static file not found"})
                return

            if path.startswith("/api/"):
                query = parse_qs(parsed.query)
                if path == "/api/cgroups":
                    self._handle_cgroups()
                elif path == "/api/series":
                    self._handle_series(query)
                elif path == "/api/attribution":
                    self._handle_attribution()
                elif path == "/api/recommendations":
                    self._handle_recommendations()
                elif path == "/api/reports":
                    self._handle_reports()
                else:
                    self._send_json(404, {"error": "API route not found"})
                return

            self._send_json(404, {"error": "Not found"})

        def _handle_cgroups(self) -> None:
            conn = self._get_db()
            if conn is None:
                self._send_json(200, [])
                return
            try:
                cur = conn.cursor()
                cur.execute("SELECT MAX(ts) FROM samples")
                row = cur.fetchone()
                max_ts = row[0] if row else None
                if max_ts is None:
                    self._send_json(200, [])
                    return

                now = time.time()
                ref_time = max_ts if max_ts < now - 300 else now
                min_ts = ref_time - 300

                cur.execute(
                    """
                    SELECT s.*
                    FROM samples s
                    JOIN (
                        SELECT cgroup, MAX(ts) AS max_ts
                        FROM samples
                        WHERE ts >= ?
                        GROUP BY cgroup
                    ) m ON s.cgroup = m.cgroup AND s.ts = m.max_ts
                    ORDER BY s.cgroup ASC
                    """,
                    (min_ts,),
                )
                rows = [dict(r) for r in cur.fetchall()]
                self._send_json(200, rows)
            except sqlite3.OperationalError:
                self._send_json(200, [])
            finally:
                conn.close()

        def _handle_series(self, query: dict[str, list[str]]) -> None:
            cgroup = query.get("cgroup", [""])[0]
            if not cgroup:
                self._send_json(400, {"error": "Missing cgroup parameter"})
                return

            metric = query.get("metric", [""])[0]
            if not metric or metric not in METRIC_WHITELIST:
                self._send_json(400, {"error": f"Invalid metric: {metric}"})
                return

            hours_val = query.get("hours", ["1"])[0]
            try:
                hours = int(hours_val)
                if hours < 1 or hours > 48:
                    raise ValueError()
            except ValueError:
                self._send_json(400, {"error": "Invalid hours parameter (must be an integer 1-48)"})
                return

            conn = self._get_db()
            if conn is None:
                self._send_json(200, [])
                return
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM samples WHERE cgroup = ? LIMIT 1", (cgroup,))
                if not cur.fetchone():
                    self._send_json(404, {"error": f"Cgroup '{cgroup}' not found"})
                    return

                cur.execute("SELECT MAX(ts) FROM samples WHERE cgroup = ?", (cgroup,))
                max_row = cur.fetchone()
                max_ts = max_row[0] if max_row else None
                if max_ts is None:
                    self._send_json(200, [])
                    return

                duration = hours * 3600
                now = time.time()
                ref_time = max_ts if max_ts < now - duration else now
                min_ts = ref_time - duration

                cur.execute(
                    f"SELECT COUNT(*) FROM samples WHERE cgroup = ? AND ts >= ? AND ts <= ? AND {metric} IS NOT NULL",
                    (cgroup, min_ts, ref_time),
                )
                count_row = cur.fetchone()
                total_points = count_row[0] if count_row else 0

                if total_points <= 2000:
                    cur.execute(
                        f"SELECT ts, {metric} FROM samples WHERE cgroup = ? AND ts >= ? AND ts <= ? AND {metric} IS NOT NULL ORDER BY ts ASC",
                        (cgroup, min_ts, ref_time),
                    )
                    points = [
                        {"ts": int(r[0]), "value": r[1], metric: r[1]}
                        for r in cur.fetchall()
                    ]
                else:
                    bucket_size = math.ceil(duration / 2000)
                    cur.execute(
                        f"""
                        SELECT (ts / ? * ?) AS b_ts, AVG({metric})
                        FROM samples
                        WHERE cgroup = ? AND ts >= ? AND ts <= ? AND {metric} IS NOT NULL
                        GROUP BY (ts / ?)
                        ORDER BY b_ts ASC
                        """,
                        (bucket_size, bucket_size, cgroup, min_ts, ref_time, bucket_size),
                    )
                    points = [
                        {"ts": int(r[0]), "value": r[1], metric: r[1]}
                        for r in cur.fetchall()
                    ]

                self._send_json(200, points)
            except sqlite3.OperationalError:
                self._send_json(200, [])
            finally:
                conn.close()

        def _handle_attribution(self) -> None:
            conn = self._get_db()
            if conn is None:
                self._send_json(200, [])
                return
            try:
                cur = conn.cursor()
                cur.execute("SELECT MAX(ts) FROM samples")
                row = cur.fetchone()
                max_ts = row[0] if row else None
                if max_ts is None:
                    self._send_json(200, [])
                    return

                now = time.time()
                ref_time = max_ts if max_ts < now - 3600 else now
                min_ts = ref_time - 3600

                cur.execute(
                    """
                    SELECT
                        cgroup,
                        AVG(netrx_blamed_cores) AS blamed,
                        AVG(netrx_attrib_cores) AS attrib
                    FROM samples
                    WHERE ts >= ? AND (netrx_blamed_cores IS NOT NULL OR netrx_attrib_cores IS NOT NULL)
                    GROUP BY cgroup
                    ORDER BY cgroup ASC
                    """,
                    (min_ts,),
                )
                rows = [
                    {
                        "cgroup": r[0],
                        "netrx_blamed_cores": r[1],
                        "netrx_attrib_cores": r[2],
                        "blamed_cores": r[1],
                        "attrib_cores": r[2],
                    }
                    for r in cur.fetchall()
                ]
                self._send_json(200, rows)
            except sqlite3.OperationalError:
                self._send_json(200, [])
            finally:
                conn.close()

        def _handle_recommendations(self) -> None:
            if not resolved_recs.is_file():
                self._send_json(200, None)
                return
            try:
                data = json.loads(resolved_recs.read_text(encoding="utf-8"))
                self._send_json(200, data)
            except Exception:
                self._send_json(200, None)

        def _handle_reports(self) -> None:
            keys = (
                "overhead",
                "forecast",
                "placement",
                "lifetime",
                "forecast_local",
                "slo",
                "placement_study",
            )
            result: dict[str, Any] = {}
            for k in keys:
                fpath = resolved_reports / f"{k}.json"
                if fpath.is_file():
                    try:
                        result[k] = json.loads(fpath.read_text(encoding="utf-8"))
                    except Exception:
                        result[k] = None
                else:
                    result[k] = None
            self._send_json(200, result)

    return DashboardHandler


def serve(
    port: int,
    db_path: str | Path,
    reports_dir: str | Path,
    recs_path: str | Path,
) -> None:
    handler_class = make_handler(db_path, reports_dir, recs_path, port)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
    actual_port = server.server_port
    print(f"http://127.0.0.1:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
