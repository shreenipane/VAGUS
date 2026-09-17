import errno
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from irm.attrib import Reader, cgroup_ids

COLUMNS: tuple[str, ...] = (
    "cpu_cores", "throttled_ratio", "mem_bytes", "io_rbps", "io_wbps", "cpu_psi", "mem_psi",
    "io_psi", "pids", "softirq_cores", "netrx_attrib_cores", "netrx_blamed_cores", "netrx_unattrib_cores",
)

SCHEMA = (
    "create table if not exists samples (ts integer not null, cgroup text not null, "
    "cpu_cores real, throttled_ratio real, mem_bytes real, io_rbps real, io_wbps real, "
    "cpu_psi real, mem_psi real, io_psi real, pids real, softirq_cores real, "
    "netrx_attrib_cores real, netrx_blamed_cores real, netrx_unattrib_cores real, "
    "primary key (cgroup, ts)) without rowid;\ncreate index if not exists samples_ts on samples(ts);"
)

_COLS = ("ts", "cgroup", *COLUMNS)
_INSERT_SQL = f"insert or replace into samples ({', '.join(_COLS)}) values ({', '.join('?' for _ in _COLS)})"


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        # Controllers can be disabled (ENODEV) or files unexposed (ENOENT)
        if e.errno in (errno.ENOENT, errno.ENODEV):
            return None
        raise


def _to_int(text: str | None) -> int | None:
    try:
        return int(text.strip()) if text else None
    except ValueError:
        return None


def _parse_cpu_stat(text: str | None) -> dict[str, int]:
    return {
        p[0]: int(p[1])
        for line in (text or "").splitlines()
        if len(p := line.split()) >= 2 and p[1].isdigit()
    }


def _parse_io_stat(text: str | None) -> tuple[int | None, int | None]:
    if text is None:
        return None, None
    rbytes = wbytes = 0
    for token in text.split():
        try:
            if token.startswith("rbytes="):
                rbytes += int(token[7:])
            elif token.startswith("wbytes="):
                wbytes += int(token[7:])
        except ValueError:
            pass
    return rbytes, wbytes


def _parse_pressure(text: str | None) -> float | None:
    for line in (text or "").splitlines():
        if line.startswith("some "):
            for token in line.split():
                if token.startswith("avg10="):
                    try:
                        return float(token[6:])
                    except ValueError:
                        return None
    return None


def discover(root: str | Path) -> list[str]:
    root_str = str(root)
    leaves: list[str] = []
    for dirpath, dirnames, _ in os.walk(root_str):
        if not dirnames and (content := _read(os.path.join(dirpath, "cgroup.events"))):
            if any(line.strip() == "populated 1" for line in content.splitlines()):
                rel = os.path.relpath(dirpath, root_str)
                leaves.append("/" if rel == "." else f"/{rel}")
    return sorted(leaves)


def read_cgroup(root: str | Path, name: str) -> dict | None:
    cg_dir = os.path.join(str(root), name.lstrip("/"))
    files = ("cpu.stat", "memory.current", "io.stat", "cpu.pressure", "memory.pressure", "io.pressure", "pids.current")
    txts = [_read(os.path.join(cg_dir, f)) for f in files]

    # Single isdir check only if a read failed, to detect vanished cgroup.
    if any(t is None for t in txts) and not os.path.isdir(cg_dir):
        return None

    cpu = _parse_cpu_stat(txts[0])
    rbytes, wbytes = _parse_io_stat(txts[2])
    return {
        "usage_usec": cpu.get("usage_usec"), "nr_periods": cpu.get("nr_periods"),
        "nr_throttled": cpu.get("nr_throttled"), "mem": _to_int(txts[1]),
        "rbytes": rbytes, "wbytes": wbytes,
        "cpu_psi": _parse_pressure(txts[3]), "mem_psi": _parse_pressure(txts[4]),
        "io_psi": _parse_pressure(txts[5]), "pids": _to_int(txts[6]),
    }


def read_host(proc: str | Path) -> dict:
    proc_str = str(proc)
    busy_ticks = softirq_ticks = mem = None

    if stat_txt := _read(os.path.join(proc_str, "stat")):
        for line in stat_txt.splitlines():
            if line.startswith("cpu ") and len(parts := line.split()) >= 9:
                busy_ticks = sum(int(parts[i]) for i in (1, 2, 3, 6, 7, 8))
                softirq_ticks = int(parts[7])
                break

    if mem_txt := _read(os.path.join(proc_str, "meminfo")):
        vals = {p[0]: int(p[1]) for l in mem_txt.splitlines() if len(p := l.split()) >= 2 and p[1].isdigit()}
        if "MemTotal:" in vals and "MemAvailable:" in vals:
            mem = (vals["MemTotal:"] - vals["MemAvailable:"]) * 1024

    return {
        "busy_ticks": busy_ticks,
        "softirq_ticks": softirq_ticks,
        "mem": mem,
        "cpu_psi": _parse_pressure(_read(os.path.join(proc_str, "pressure", "cpu"))),
        "mem_psi": _parse_pressure(_read(os.path.join(proc_str, "pressure", "memory"))),
        "io_psi": _parse_pressure(_read(os.path.join(proc_str, "pressure", "io"))),
    }


def _delta(prev: float | int | None, cur: float | int | None) -> float | int | None:
    return (cur - prev) if (prev is not None and cur is not None and cur >= prev) else None


def _rate(prev: float | int | None, cur: float | int | None, dt: float | None, scale: float = 1.0) -> float | None:
    d = _delta(prev, cur)
    return (d / scale / dt) if (d is not None and dt is not None and dt > 0 and scale > 0) else None


def leaf_gauges(prev: dict | None, cur: dict, dt: float | None) -> dict:
    p = prev or {}
    res = {col: None for col in COLUMNS}
    res["cpu_cores"] = _rate(p.get("usage_usec"), cur.get("usage_usec"), dt, 1e6)
    if prev is not None:
        dp = _delta(prev.get("nr_periods"), cur.get("nr_periods"))
        dt_th = _delta(prev.get("nr_throttled"), cur.get("nr_throttled"))
        if dp is not None and dt_th is not None:
            res["throttled_ratio"] = (dt_th / dp) if dp > 0 else 0.0
    res["mem_bytes"] = cur.get("mem")
    res["io_rbps"] = _rate(p.get("rbytes"), cur.get("rbytes"), dt)
    res["io_wbps"] = _rate(p.get("wbytes"), cur.get("wbytes"), dt)
    res["cpu_psi"], res["mem_psi"], res["io_psi"] = cur.get("cpu_psi"), cur.get("mem_psi"), cur.get("io_psi")
    res["pids"] = cur.get("pids")
    return res


def host_gauges(prev: dict | None, cur: dict, dt: float | None, tck: float) -> dict:
    p = prev or {}
    res = {col: None for col in COLUMNS}
    res["cpu_cores"] = _rate(p.get("busy_ticks"), cur.get("busy_ticks"), dt, tck)
    res["softirq_cores"] = _rate(p.get("softirq_ticks"), cur.get("softirq_ticks"), dt, tck)
    res["mem_bytes"] = cur.get("mem")
    res["cpu_psi"], res["mem_psi"], res["io_psi"] = cur.get("cpu_psi"), cur.get("mem_psi"), cur.get("io_psi")
    return res


def sweep(
    root: str | Path, proc: str | Path, state: dict | None,
    now_wall: float, now_mono: float, tck: float | None = None,
    attrib: dict | None = None,
) -> tuple[list[dict], dict]:
    if tck is None:
        try:
            tck = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            tck = 100.0

    prev_mono = state.get("mono") if state else None
    prev_leaf = state.get("leaf", {}) if state else {}
    prev_host = state.get("host") if state else None
    dt = (now_mono - prev_mono) if (prev_mono is not None) else None

    if attrib is not None:
        name_to_id = {name: ino for ino, name in cgroup_ids(root).items()}
        attrib_cores = attrib.get("attrib_cores", {})
        blamed_cores = attrib.get("blamed_cores", {})
        unattrib_cores = attrib.get("unattrib_cores")
        root_ino = name_to_id.get("/")
    else:
        name_to_id = attrib_cores = blamed_cores = unattrib_cores = root_ino = None

    rows, new_leaf, ts = [], {}, int(now_wall)
    for name in discover(root):
        raw = read_cgroup(root, name)
        if raw is not None:
            gauges = leaf_gauges(prev_leaf.get(name), raw, dt)
            if attrib is not None:
                ino = name_to_id.get(name)
                gauges["netrx_attrib_cores"] = attrib_cores.get(ino, 0.0) if ino is not None else 0.0
                gauges["netrx_blamed_cores"] = blamed_cores.get(ino, 0.0) if ino is not None else 0.0
            rows.append({"ts": ts, "cgroup": name, **gauges})
            new_leaf[name] = raw

    raw_host = read_host(proc)
    h_gauges = host_gauges(prev_host, raw_host, dt, tck)
    if attrib is not None:
        h_gauges["netrx_blamed_cores"] = blamed_cores.get(root_ino, 0.0) if root_ino is not None else 0.0
        h_gauges["netrx_unattrib_cores"] = unattrib_cores
    rows.append({"ts": ts, "cgroup": "host", **h_gauges})
    return rows, {"mono": now_mono, "leaf": new_leaf, "host": raw_host}


def open_db(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript("pragma journal_mode=wal;\n" + SCHEMA)
    return conn


def write_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if rows:
        with conn:
            conn.executemany(_INSERT_SQL, [[r.get(c) for c in _COLS] for r in rows])


def prune(conn: sqlite3.Connection, now_wall: float, retention_hours: float) -> int:
    with conn:
        return conn.execute("delete from samples where ts < ?", (now_wall - retention_hours * 3600,)).rowcount


def next_deadline(deadline: float, interval: float, now_mono: float) -> float:
    nxt = deadline + interval
    return nxt if nxt > now_mono else deadline + (int(math.floor((now_mono - deadline) / interval)) + 1) * interval


def run(
    db_path: str | Path, root: str | Path, proc: str | Path,
    interval: float, retention_hours: float,
    duration: float | None = None, tck: float | None = None, on_sweep=None,
    attrib_stream=None,
) -> int:
    reader = Reader(attrib_stream) if attrib_stream is not None else None
    conn = open_db(db_path)
    try:
        now_mono = time.monotonic()
        prune(conn, time.time(), retention_hours)
        last_prune = start_mono = deadline = now_mono
        state, sweeps = None, 0

        while True:
            now_mono, now_wall = time.monotonic(), time.time()
            if duration is not None and (now_mono - start_mono) >= duration:
                break
            if now_mono - last_prune >= 3600.0:
                prune(conn, now_wall, retention_hours)
                last_prune = now_mono

            attrib = reader.latest(3 * interval) if reader is not None else None
            rows, state = sweep(root, proc, state, now_wall, now_mono, tck=tck, attrib=attrib)
            write_rows(conn, rows)
            sweeps += 1
            if on_sweep is not None:
                on_sweep(rows)

            now_mono = time.monotonic()
            if duration is not None and (now_mono - start_mono) >= duration:
                break

            deadline = next_deadline(deadline, interval, now_mono)
            sleep_secs = deadline - now_mono
            if duration is not None:
                if (now_mono - start_mono) >= duration:
                    break
                sleep_secs = min(sleep_secs, duration - (now_mono - start_mono))
            if sleep_secs > 0:
                time.sleep(sleep_secs)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
    return sweeps


def bench(
    seconds: float, interval: float, root: str | Path, proc: str | Path, tck: float | None = None,
) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        leaf_counts, t0_wall, t0_cpu = [], time.time(), time.process_time()
        run(
            db_path=Path(tmpdir) / "bench.db", root=root, proc=proc, interval=interval,
            retention_hours=48.0, duration=seconds, tck=tck,
            on_sweep=lambda rows: leaf_counts.append(sum(1 for r in rows if r["cgroup"] != "host")),
        )
        cpu_seconds, wall = time.process_time() - t0_cpu, time.time() - t0_wall
        cgroups = float(sum(leaf_counts) / len(leaf_counts)) if leaf_counts else 0.0
        pct_of_one_core = (100.0 * cpu_seconds / wall) if wall > 0 else 0.0
        return {
            "interval": interval, "seconds": seconds, "cgroups": cgroups,
            "cpu_seconds": cpu_seconds, "pct_of_one_core": pct_of_one_core,
            "pct_of_host": pct_of_one_core / (os.cpu_count() or 1),
        }
