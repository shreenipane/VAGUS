import http.client
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
import pytest
from irm import monitor
from irm.dashboard import make_handler


@pytest.fixture
def test_setup(tmp_path: Path):
    db_path = tmp_path / "test.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    recs_path = tmp_path / "recommendations.json"

    # Seed test database with host and leaf cgroups
    conn = monitor.open_db(db_path)
    now = int(time.time())
    monitor.write_rows(
        conn,
        [
            {
                "ts": now - 30,
                "cgroup": "host",
                "cpu_cores": 4.5,
                "mem_bytes": 16 * 1024 * 1024 * 1024,
                "cpu_psi": 0.12,
            },
            {
                "ts": now - 30,
                "cgroup": "/leaf1",
                "cpu_cores": 1.2,
                "throttled_ratio": 0.05,
                "mem_bytes": 256 * 1024 * 1024,
                "cpu_psi": 0.08,
                "netrx_attrib_cores": 0.04,
                "netrx_blamed_cores": 0.06,
            },
            {
                "ts": now - 15,
                "cgroup": "/leaf1",
                "cpu_cores": 1.8,
                "throttled_ratio": 0.10,
                "mem_bytes": 260 * 1024 * 1024,
                "cpu_psi": 0.09,
                "netrx_attrib_cores": 0.05,
                "netrx_blamed_cores": 0.07,
            },
        ],
    )
    conn.close()

    # Start server on ephemeral port 0
    handler_cls = make_handler(db_path, reports_dir, recs_path, port=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_port

    class Client:
        def request(self, method: str, path: str, host: str | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Host": host if host is not None else f"127.0.0.1:{port}"}
            c.request(method, path, body=body, headers=headers)
            resp = c.getresponse()
            status = resp.status
            resp_headers = {k: v for k, v in resp.getheaders()}
            data = resp.read()
            c.close()
            return status, resp_headers, data

        def get_json(self, path: str, host: str | None = None) -> tuple[int, Any]:
            status, _, data = self.request("GET", path, host=host)
            return status, json.loads(data.decode("utf-8"))

    yield Client(), port, tmp_path

    server.shutdown()
    server.server_close()


def test_bad_host_returns_421(test_setup):
    client, port, _ = test_setup
    status, _, data = client.request("GET", "/", host="attacker.example.com")
    assert status == 421
    res = json.loads(data.decode("utf-8"))
    assert "error" in res


def test_post_returns_405(test_setup):
    client, port, _ = test_setup
    status, _, data = client.request("POST", "/api/cgroups")
    assert status == 405
    res = json.loads(data.decode("utf-8"))
    assert "error" in res


def test_unknown_metric_returns_400(test_setup):
    client, port, _ = test_setup
    status, res = client.get_json("/api/series?cgroup=%2Fleaf1&metric=forbidden_metric&hours=1")
    assert status == 400
    assert "error" in res


def test_hours_validation(test_setup):
    client, port, _ = test_setup
    status, _ = client.get_json("/api/series?cgroup=%2Fleaf1&metric=cpu_cores&hours=0")
    assert status == 400

    status, _ = client.get_json("/api/series?cgroup=%2Fleaf1&metric=cpu_cores&hours=49")
    assert status == 400

    status, _ = client.get_json("/api/series?cgroup=%2Fleaf1&metric=cpu_cores&hours=abc")
    assert status == 400


def test_missing_cgroup_or_unknown_cgroup(test_setup):
    client, port, _ = test_setup
    # Missing cgroup param -> 400
    status, _ = client.get_json("/api/series?metric=cpu_cores&hours=1")
    assert status == 400

    # Nonexistent cgroup in DB -> 404
    status, res = client.get_json("/api/series?cgroup=%2Fnot_found&metric=cpu_cores&hours=1")
    assert status == 404
    assert "error" in res


def test_series_returns_points(test_setup):
    client, port, _ = test_setup
    status, points = client.get_json("/api/series?cgroup=%2Fleaf1&metric=cpu_cores&hours=1")
    assert status == 200
    assert isinstance(points, list)
    assert len(points) == 2
    assert "ts" in points[0]
    assert points[0]["cpu_cores"] == pytest.approx(1.2)
    assert points[1]["cpu_cores"] == pytest.approx(1.8)
    assert points[0]["value"] == pytest.approx(1.2)


def test_root_has_csp_and_security_headers(test_setup):
    client, port, _ = test_setup
    status, headers, body = client.request("GET", "/")
    assert status == 200
    assert headers.get("Content-Security-Policy") == "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("Referrer-Policy") == "no-referrer"
    assert headers.get("Cache-Control") == "no-store"
    assert "text/html" in headers.get("Content-Type", "")
    content = body.decode("utf-8")
    assert "Intelligent Resource Manager" in content
    assert "Monitor" in content


def test_static_files(test_setup):
    client, port, _ = test_setup
    # app.js
    status, headers, body = client.request("GET", "/app.js")
    assert status == 200
    assert "javascript" in headers.get("Content-Type", "")
    assert b"textContent" in body

    # style.css
    status, headers, body = client.request("GET", "/style.css")
    assert status == 200
    assert "text/css" in headers.get("Content-Type", "")
    assert b":root" in body

    # nonexistent
    status, _, _ = client.request("GET", "/other.txt")
    assert status == 404


def test_missing_reports_returns_nulls(test_setup):
    client, port, _ = test_setup
    status, reports = client.get_json("/api/reports")
    assert status == 200
    assert reports["overhead"] is None
    assert reports["forecast"] is None
    assert reports["placement"] is None


def test_reports_with_data(test_setup):
    client, port, tmp_path = test_setup
    reports_dir = tmp_path / "reports"
    (reports_dir / "overhead.json").write_text(
        json.dumps({"pct_of_one_core": 0.42, "cgroups": 10.0}),
        encoding="utf-8",
    )
    status, reports = client.get_json("/api/reports")
    assert status == 200
    assert reports["overhead"]["pct_of_one_core"] == pytest.approx(0.42)
    assert reports["overhead"]["cgroups"] == pytest.approx(10.0)


def test_cgroups_api(test_setup):
    client, port, _ = test_setup
    status, rows = client.get_json("/api/cgroups")
    assert status == 200
    assert isinstance(rows, list)
    cgroup_names = {r["cgroup"] for r in rows}
    assert "host" in cgroup_names
    assert "/leaf1" in cgroup_names
    leaf1 = next(r for r in rows if r["cgroup"] == "/leaf1")
    assert leaf1["cpu_cores"] == pytest.approx(1.8)


def test_attribution_api(test_setup):
    client, port, _ = test_setup
    status, rows = client.get_json("/api/attribution")
    assert status == 200
    assert isinstance(rows, list)
    leaf1 = next(r for r in rows if r["cgroup"] == "/leaf1")
    assert leaf1["netrx_attrib_cores"] == pytest.approx((0.04 + 0.05) / 2)


def test_missing_db_returns_empty_lists_and_no_crash(tmp_path):
    missing_db = tmp_path / "nonexistent.db"
    rep_dir = tmp_path / "reports"
    rep_dir.mkdir()
    recs = tmp_path / "recs.json"

    handler_cls = make_handler(missing_db, rep_dir, recs, port=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_port

    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        # cgroups
        conn.request("GET", "/api/cgroups", headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read()) == []

        # series
        conn.request("GET", "/api/series?cgroup=%2Fleaf&metric=cpu_cores&hours=1", headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read()) == []

        # attribution
        conn.request("GET", "/api/attribution", headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        assert r.status == 200
        assert json.loads(r.read()) == []
    finally:
        server.shutdown()
        server.server_close()


def test_missing_recommendations_returns_null(test_setup):
    client, port, _ = test_setup
    status, data = client.get_json("/api/recommendations")
    assert status == 200
    assert data is None


def test_present_recommendations(test_setup):
    client, port, tmp_path = test_setup
    recs_path = tmp_path / "recommendations.json"
    recs_path.write_text(json.dumps({"items": [{"cgroup": "/leaf1", "cpu_max": "max"}], "pairs": []}))

    status, data = client.get_json("/api/recommendations")
    assert status == 200
    assert len(data["items"]) == 1
    assert data["items"][0]["cgroup"] == "/leaf1"
