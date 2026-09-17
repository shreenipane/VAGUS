import argparse
import asyncio
import datetime
import gzip
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Sequence
import numpy as np

from irm import HOME


# --- Pure helpers ---


def rotation(reps: int, arms: Sequence[str] = ("A", "B", "C", "W", "K")) -> list[list[str]]:
    """Generate rotation order of conditions: rep r uses arms rotated left by r positions."""
    if reps <= 0 or not arms:
        return []
    arms_list = list(arms)
    n = len(arms_list)
    return [arms_list[i % n :] + arms_list[: i % n] for i in range(reps)]


def percentiles(lat_ms: Sequence[float]) -> dict[str, float]:
    """Calculate p50, p95, p99 percentiles for a sequence of latencies in ms."""
    if not lat_ms or len(lat_ms) == 0:
        return {"p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}
    arr = np.asarray(lat_ms, dtype=float)
    return {
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "p99_ms": float(np.percentile(arr, 99)),
    }


def window_p99(records: list[list[float]], window_s: float = 1.0) -> list[float]:
    """Compute p99 per window by scheduled offset. Windows with no records are skipped."""
    if not records:
        return []
    buckets: dict[int, list[float]] = {}
    for offset, lat in records:
        idx = int(offset // window_s)
        buckets.setdefault(idx, []).append(float(lat))
    return [float(np.percentile(buckets[k], 99)) for k in sorted(buckets.keys()) if buckets[k]]


def violation_rate(window_p99s: list[float], target_ms: float) -> float:
    """Calculate the fraction of windows whose p99 exceeds target_ms."""
    if not window_p99s:
        return 0.0
    violations = sum(1 for p in window_p99s if p > target_ms)
    return float(violations / len(window_p99s))


def threshold_sensitivity(window_p99s: list[float], a_p99: float) -> dict[str, float]:
    """Calculate the fraction of windows whose p99 exceeds 1.5x, 2x, and 3x the first A p99."""
    if not window_p99s or a_p99 <= 0:
        return {"1.5x": 0.0, "2x": 0.0, "3x": 0.0}
    return {
        "1.5x": violation_rate(window_p99s, 1.5 * a_p99),
        "2x": violation_rate(window_p99s, 2.0 * a_p99),
        "3x": violation_rate(window_p99s, 3.0 * a_p99),
    }


def arm_plan(arm: str, recs: dict | None, service_cg: str) -> dict:
    """Return a recs-shaped plan dict with a fresh generated_at timestamp."""
    now_ts = int(time.time())
    ncpu = recs.get("ncpu", os.cpu_count() or 1) if recs else (os.cpu_count() or 1)
    if arm == "C":
        items = list(recs.get("items", [])) if recs else []
        skipped = list(recs.get("skipped", [])) if recs else []
        pairs = list(recs.get("pairs", [])) if recs else []
    elif arm == "K":
        items = [it for it in (recs.get("items", []) if recs else []) if it.get("cgroup") != service_cg]
        skipped = list(recs.get("skipped", [])) if recs else []
        pairs = list(recs.get("pairs", [])) if recs else []
    elif arm == "W":
        items = [{
            "cgroup": service_cg,
            "cpu_max": None,
            "memory_high": None,
            "cpu_weight": 1000,
            "source": "manual",
            "peak_cores": None,
            "old": None,
            "reason": "service weight only",
        }]
        skipped = []
        pairs = []
    else:
        items = []
        skipped = []
        pairs = []
    return {
        "generated_at": now_ts,
        "ncpu": ncpu,
        "items": items,
        "skipped": skipped,
        "pairs": pairs,
    }


def save_raw(
    path: str | Path,
    records: list[list[float]],
    errors: int,
    meta: dict | None = None,
) -> None:
    """Save raw records, errors, and metadata as gzip-compressed JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "records": records,
        "errors": errors,
        "meta": meta if meta is not None else {},
    }
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def parse_cgroup(text: str) -> str:
    """Extract cgroup path from a /proc/<pid>/cgroup line (0::/path)."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("0::"):
            return line[3:]
    for line in text.splitlines():
        parts = line.strip().split(":", 2)
        if len(parts) == 3 and parts[2].startswith("/"):
            return parts[2]
    return ""


def summarize(
    runs: dict[str, list[dict] | dict],
    target_ms: float,
    first_a_p99: float | None = None,
) -> dict[str, dict]:
    """Summarize run records per condition."""
    if first_a_p99 is None:
        first_a_p99 = (target_ms / 2.0) if target_ms > 0 else 0.0

    summary: dict[str, dict] = {}
    for cond, rep_data in runs.items():
        reps = rep_data if isinstance(rep_data, list) else [rep_data]

        all_records = []
        all_lats = []
        rep_p99s = []
        all_windows = []
        rep_violation_rates = []
        total_errors = 0
        total_seconds = 0.0
        cpuhog_vals = []

        for rep in reps:
            recs = rep.get("records", [])
            sec = float(rep.get("seconds", rep.get("duration", 0.0)))
            err = int(rep.get("errors", 0))

            all_records.extend(recs)
            total_errors += err

            rep_lats = [float(r[1]) for r in recs]
            if rep_lats:
                all_lats.extend(rep_lats)
                rep_p99s.append(float(np.percentile(rep_lats, 99)))

            if sec <= 0.0 and recs:
                sec = max(1.0, math.ceil(max(r[0] for r in recs)))
            total_seconds += sec

            win_p99s = window_p99(recs, window_s=1.0)
            all_windows.extend(win_p99s)
            rep_violation_rates.append(violation_rate(win_p99s, target_ms))

            for key in ("cpuhog_ips", "iters_per_s"):
                if rep.get(key) is not None:
                    cpuhog_vals.append(float(rep[key]))
                    break

        pcts = percentiles(all_lats)

        if rep_p99s:
            rep_p99_ms = {
                "min": float(np.min(rep_p99s)),
                "mean": float(np.mean(rep_p99s)),
                "max": float(np.max(rep_p99s)),
                "list": rep_p99s,
            }
        else:
            rep_p99_ms = {"min": 0.0, "mean": 0.0, "max": 0.0, "list": []}

        v_rate = violation_rate(all_windows, target_ms)
        rps = float(len(all_records) / total_seconds) if total_seconds > 0 else 0.0
        cpuhog_ips = None if cond == "A" else (float(np.mean(cpuhog_vals)) if cpuhog_vals else None)

        summary[cond] = {
            "p50_ms": pcts["p50_ms"],
            "p95_ms": pcts["p95_ms"],
            "p99_ms": pcts["p99_ms"],
            "rep_p99_ms": rep_p99_ms,
            "violation_rate": v_rate,
            "rep_violation_rates": rep_violation_rates,
            "threshold_sensitivity": threshold_sensitivity(all_windows, first_a_p99),
            "rps": rps,
            "errors": total_errors,
            "cpuhog_ips": cpuhog_ips,
        }

    return summary


# --- Role implementations ---


def calibrate_iters() -> int:
    """Calibrate iters so that iters * sha256(64 KiB) takes ~2 ms based on 50 single iterations."""
    chunk = b"\x00" * 65536
    t0 = time.perf_counter()
    for _ in range(50):
        hashlib.sha256(chunk).digest()
    dt = time.perf_counter() - t0
    single_iter = dt / 50.0
    return max(1, int(round(0.002 / max(single_iter, 1e-9))))


async def _service_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    iters: int,
    chunk: bytes,
) -> None:
    try:
        while True:
            await reader.readuntil(b"\r\n\r\n")
            for _ in range(iters):
                hashlib.sha256(chunk).digest()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_service(
    port: int,
    ready_fn=None,
    stop_event=None,
    iters: int | None = None,
) -> None:
    """Service role: asyncio HTTP/1.1 keep-alive server on 127.0.0.1."""
    if iters is None:
        iters = calibrate_iters()
    chunk = b"\x00" * 65536

    server = await asyncio.start_server(
        lambda r, w: _service_connection(r, w, iters, chunk),
        host="127.0.0.1",
        port=port,
    )
    if ready_fn:
        bound_port = server.sockets[0].getsockname()[1]
        ready_fn(bound_port)

    # Setup termination future if running in main thread
    stop_fut = None
    try:
        loop = asyncio.get_running_loop()
        stop_fut = loop.create_future()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: stop_fut.set_result(None) if not stop_fut.done() else None)
            except (ValueError, RuntimeError, NotImplementedError):
                pass
    except Exception:
        stop_fut = None

    async with server:
        if stop_event is not None:
            while not stop_event.is_set():
                await asyncio.sleep(0.05)
        elif stop_fut is not None:
            await stop_fut
        else:
            await server.serve_forever()


async def run_loadgen(
    port: int,
    rate: float,
    seconds: float,
    seed: int = 0,
    concurrency: int = 32,
    warmup: float = 0.0,
) -> dict:
    """Loadgen role: open-loop Poisson arrivals over pooled keep-alive connections."""
    pool: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = asyncio.Queue()
    errors = 0
    records: list[list[float]] = []

    async def open_conn():
        nonlocal errors
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            pool.put_nowait((r, w))
        except Exception:
            errors += 1

    await asyncio.gather(*[open_conn() for _ in range(concurrency)])

    rng = random.Random(seed)
    schedule: list[float] = []
    t = 0.0
    total_seconds = warmup + seconds
    while True:
        gap = rng.expovariate(rate)
        t += gap
        if t >= total_seconds:
            break
        schedule.append(t)

    t_start = time.monotonic()

    async def send_one(offset_s: float, sched_mono: float):
        nonlocal errors
        w = None
        try:
            r, w = await pool.get()
        except Exception:
            if offset_s >= warmup:
                errors += 1
            return

        try:
            w.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await w.drain()
            await r.readuntil(b"\r\n\r\n")
            await r.readexactly(2)
            comp_mono = time.monotonic()
            lat_ms = (comp_mono - sched_mono) * 1000.0
            if offset_s >= warmup:
                records.append([offset_s - warmup, lat_ms])
            pool.put_nowait((r, w))
        except asyncio.CancelledError:
            if offset_s >= warmup:
                errors += 1
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
            raise
        except Exception:
            if offset_s >= warmup:
                errors += 1
            if w is not None:
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass
            try:
                new_r, new_w = await asyncio.open_connection("127.0.0.1", port)
                pool.put_nowait((new_r, new_w))
            except Exception:
                pass

    tasks: list[asyncio.Task] = []
    for offset_s in schedule:
        sched_mono = t_start + offset_s
        delay = sched_mono - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(send_one(offset_s, sched_mono)))

    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=5.0)
        for p in pending:
            p.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    while not pool.empty():
        try:
            r, w = pool.get_nowait()
            w.close()
            await w.wait_closed()
        except Exception:
            pass

    records.sort(key=lambda x: x[0])
    return {"records": records, "errors": errors}


def _cpu_worker(
    idx: int,
    counts,
    seconds: float,
    delay: float,
    start_flag: str | None,
    stop_event,
):
    if start_flag:
        while not stop_event.is_set() and not os.path.exists(start_flag):
            for _ in range(10000):
                pass
    elif delay > 0:
        t_delay_end = time.monotonic() + delay
        while not stop_event.is_set() and time.monotonic() < t_delay_end:
            for _ in range(10000):
                pass

    t_end = time.monotonic() + seconds
    local_count = 0
    while not stop_event.is_set() and time.monotonic() < t_end:
        for _ in range(10000):
            local_count += 1
        counts[idx] = local_count
    counts[idx] = local_count


def run_cpuhog(
    procs: int,
    seconds: float,
    out: str | Path,
    delay: float = 0.0,
    start_flag: str | Path | None = None,
) -> dict:
    """CPU hog role: N multiprocessing busy-loop workers counting iterations."""
    counts = multiprocessing.RawArray("q", procs)
    stop_event = multiprocessing.Event()
    flag_str = str(start_flag) if start_flag else None
    workers = []
    for i in range(procs):
        p = multiprocessing.Process(
            target=_cpu_worker,
            args=(i, counts, seconds, delay, flag_str, stop_event),
            daemon=True,
        )
        workers.append(p)

    for p in workers:
        p.start()

    def handle_signal(signum, frame):
        stop_event.set()

    old_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    old_sigint = signal.signal(signal.SIGINT, handle_signal)

    join_timeout = (delay + seconds + 120.0) if flag_str else max(0.1, delay + seconds + 5.0)
    try:
        for p in workers:
            p.join(timeout=join_timeout)
    finally:
        stop_event.set()
        for p in workers:
            if p.is_alive():
                p.terminate()
                p.join(timeout=1.0)
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)

    total_iters = sum(counts)
    iters_per_s = float(total_iters / seconds) if seconds > 0 else 0.0
    res = {"iters_per_s": iters_per_s}
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


def run_nethog_rx(port: int, seconds: float) -> None:
    """Network hog RX role: binds to UDP port and drains incoming datagrams."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    sock.settimeout(0.5)
    stop_event = threading.Event()

    def handle_signal(signum, frame):
        stop_event.set()

    old_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    old_sigint = signal.signal(signal.SIGINT, handle_signal)

    t_end = time.monotonic() + seconds
    try:
        while not stop_event.is_set() and time.monotonic() < t_end:
            try:
                sock.recv(65535)
            except (socket.timeout, OSError):
                pass
    finally:
        sock.close()
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)


def _nethog_tx_worker(port: int, seconds: float, stop_event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = b"\x00" * 1400
    t_end = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < t_end:
        try:
            sock.sendto(msg, ("127.0.0.1", port))
        except OSError:
            pass
    sock.close()


def run_nethog_tx(port: int, seconds: float, senders: int = 2) -> None:
    """Network hog TX role: N senders transmitting 1400B datagrams to UDP port."""
    stop_event = multiprocessing.Event()
    workers = [
        multiprocessing.Process(
            target=_nethog_tx_worker,
            args=(port, seconds, stop_event),
            daemon=True,
        )
        for _ in range(senders)
    ]
    for p in workers:
        p.start()

    def handle_signal(signum, frame):
        stop_event.set()

    old_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    old_sigint = signal.signal(signal.SIGINT, handle_signal)

    try:
        for p in workers:
            p.join(timeout=seconds + 5.0)
    finally:
        stop_event.set()
        for p in workers:
            if p.is_alive():
                p.terminate()
                p.join(timeout=1.0)
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)


def _nethog_receiver(port_queue, seconds: float, stop_event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    port_queue.put(port)
    sock.settimeout(0.5)
    t_end = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < t_end:
        try:
            sock.recv(65535)
        except (socket.timeout, OSError):
            pass
    sock.close()


def _nethog_sender(port: int, seconds: float, stop_event):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = b"\x00" * 1400
    t_end = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < t_end:
        try:
            sock.sendto(msg, ("127.0.0.1", port))
        except OSError:
            pass
    sock.close()


def run_nethog(seconds: float) -> None:
    """Network hog role: 1 receiver draining UDP, 2 senders transmitting 1400B datagrams on loopback."""
    port_queue = multiprocessing.Queue()
    stop_event = multiprocessing.Event()
    recv_proc = multiprocessing.Process(
        target=_nethog_receiver,
        args=(port_queue, seconds, stop_event),
        daemon=True,
    )
    recv_proc.start()
    port = port_queue.get()

    senders = [
        multiprocessing.Process(
            target=_nethog_sender,
            args=(port, seconds, stop_event),
            daemon=True,
        )
        for _ in range(2)
    ]
    for p in senders:
        p.start()

    def handle_signal(signum, frame):
        stop_event.set()

    old_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    old_sigint = signal.signal(signal.SIGINT, handle_signal)

    try:
        recv_proc.join(timeout=seconds + 5.0)
        for p in senders:
            p.join(timeout=seconds + 5.0)
    finally:
        stop_event.set()
        for p in [recv_proc, *senders]:
            if p.is_alive():
                p.terminate()
                p.join(timeout=1.0)
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)


# --- Driver helpers ---


def get_free_port() -> int:
    """Bind to 127.0.0.1:0 and close to obtain a free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Wait until 127.0.0.1:port accepts connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def wait_for_cgroup(pid: int, unit_name: str, timeout: float = 5.0) -> str:
    """Poll /proc/<pid>/cgroup until path ends with <unit_name>.scope."""
    target_suffix = f"{unit_name}.scope"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as f:
                cg = parse_cgroup(f.read())
            if cg.endswith(target_suffix):
                return cg
        except OSError:
            pass
        time.sleep(0.05)
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as f:
            return parse_cgroup(f.read())
    except OSError:
        return ""


def run_slo(
    out_json: str | Path,
    minutes: float | int = 3,
    reps: int = 3,
    rate: float | int = 200,
    arms: Sequence[str] = ("A", "B", "C", "W", "K"),
    raw_dir: str | Path | None = None,
    warmup: float = 70.0,
) -> dict:
    """SLO experiment driver managing transient scopes across rotation conditions."""
    if "A" not in arms:
        raise ValueError("arm 'A' must be in arms")

    if raw_dir is None:
        raw_path = HOME / "reports" / "slo_raw"
    else:
        raw_path = Path(raw_dir)
    try:
        raw_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Fixed request work: 5 calibrations, take median
    cal_samples = [calibrate_iters() for _ in range(5)]
    iters = int(np.median(cal_samples))

    warmup_s = float(warmup)
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    seconds = float(minutes * 60.0)
    try:
        plan = rotation(reps, arms=arms)
    except TypeError:
        plan = rotation(reps)
    runs: dict[str, list[dict]] = {arm: [] for arm in set(arms).union(set().union(*plan))}
    first_a_p99: float | None = None
    applied_plans: dict[str, list[dict]] = {}
    apply_late_runs: list[str] = []

    all_started_units: list[str] = []
    applied_dbs: list[Path] = []
    cleaned_units: set[str] = set()

    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        try:
            for rep_idx, arm_list in enumerate(plan):
                for arm in arm_list:
                    cond_units: list[str] = []
                    cond_apply_ran = False
                    tmp_db = tmp_dir / f"monitor_{rep_idx}_{arm}.db"
                    applied_dbs.append(tmp_db)

                    def start_role(role: str, extra_args: list[str]) -> tuple[subprocess.Popen, str, str]:
                        hex8 = os.urandom(4).hex()
                        unit = f"irm-exp-{role}-{hex8}"
                        cmd = [
                            "systemd-run",
                            "--user",
                            "--scope",
                            "--quiet",
                            f"--unit={unit}",
                            "--",
                            sys.executable,
                            "-m",
                            "irm.experiment",
                            role,
                            *extra_args,
                        ]
                        proc = subprocess.Popen(cmd, cwd=str(HOME))
                        cond_units.append(unit)
                        all_started_units.append(unit)
                        cg = wait_for_cgroup(proc.pid, unit)
                        return proc, unit, cg

                    try:
                        port = get_free_port()
                        seed = rep_idx * 100 + ord(arm)
                        loadgen_out = tmp_dir / f"loadgen_{rep_idx}_{arm}.json"
                        cpuhog_out = tmp_dir / f"cpuhog_{rep_idx}_{arm}.json"

                        # 1. Start service with --iters
                        srv_proc, srv_unit, srv_cg = start_role(
                            "service",
                            ["--port", str(port), "--iters", str(iters)],
                        )
                        wait_for_port(port, timeout=10.0)

                        # 2. Start load generator immediately with --warmup 70 --seconds <minutes*60>
                        lg_proc, lg_unit, lg_cg = start_role(
                            "loadgen",
                            [
                                "--port", str(port),
                                "--rate", str(rate),
                                "--seconds", str(seconds),
                                "--warmup", str(warmup_s),
                                "--out", str(loadgen_out),
                                "--seed", str(seed),
                            ],
                        )
                        t_lg_start = time.monotonic()
                        t_warmup_deadline = t_lg_start + warmup_s

                        # 3. Load generator protected in every arm: apply cpu.weight=1000
                        from irm.execute import apply
                        cond_apply_ran = True
                        lg_plan = {
                            "generated_at": int(time.time()),
                            "items": [{
                                "cgroup": lg_cg,
                                "cpu_max": None,
                                "memory_high": None,
                                "cpu_weight": 1000,
                            }],
                        }
                        apply(tmp_db, lg_plan, "/sys/fs/cgroup", allow=[], yes=True)

                        # 4. Start hogs for B, C, W, K
                        cpu_proc = None
                        rx_proc = None
                        tx_proc = None
                        cpu_cg = ""
                        rx_cg = ""
                        tx_cg = ""
                        if arm in ("B", "C", "W", "K"):
                            procs_count = os.cpu_count() or 1
                            cpu_proc, cpu_unit, cpu_cg = start_role(
                                "cpuhog",
                                [
                                    "--procs", str(procs_count),
                                    "--delay", str(warmup_s),
                                    "--seconds", str(seconds),
                                    "--out", str(cpuhog_out),
                                ],
                            )
                            net_port = get_free_port()
                            net_seconds = warmup_s + seconds + 10.0
                            rx_proc, rx_unit, rx_cg = start_role(
                                "nethog-rx",
                                ["--port", str(net_port), "--seconds", str(net_seconds)],
                            )
                            tx_proc, tx_unit, tx_cg = start_role(
                                "nethog-tx",
                                ["--port", str(net_port), "--seconds", str(net_seconds), "--senders", "2"],
                            )

                        # 5. Driver waits 60s (monitor.run in C/K, sleep in A/B/W)
                        if arm in ("C", "K"):
                            from irm.monitor import run as monitor_run
                            monitor_run(
                                tmp_db,
                                "/sys/fs/cgroup",
                                "/proc",
                                interval=1.0,
                                retention_hours=1.0,
                                duration=60.0,
                            )
                        else:
                            time.sleep(60.0)

                        # 6. Build and apply plan for C, W, K
                        apply_late = False
                        if arm in ("C", "W", "K"):
                            if arm in ("C", "K"):
                                from irm.recommend import recommend
                                recs = recommend(
                                    tmp_db,
                                    "/sys/fs/cgroup",
                                    protect=[srv_cg],
                                    only=[cpu_cg, tx_cg, rx_cg],
                                    min_samples=30,
                                    hours=1.0,
                                    min_cores=0.0,
                                )
                            else:
                                recs = None

                            plan_to_apply = arm_plan(arm, recs, srv_cg)
                            apply(tmp_db, plan_to_apply, "/sys/fs/cgroup", allow=[], yes=True)

                            if arm not in applied_plans:
                                applied_plans[arm] = plan_to_apply.get("items", [])

                            if time.monotonic() > t_warmup_deadline:
                                apply_late = True
                                apply_late_runs.append(f"rep{rep_idx}_{arm}")

                        # 7. Wait for loadgen and hogs to finish
                        lg_proc.wait()
                        if cpu_proc is not None:
                            try:
                                cpu_proc.wait(timeout=warmup_s + seconds + 15.0)
                            except subprocess.TimeoutExpired:
                                pass

                        # 8. Collect results and save raw data
                        with open(loadgen_out, "r", encoding="utf-8") as f:
                            lg_data = json.load(f)
                        records = lg_data.get("records", [])
                        errors = lg_data.get("errors", 0)

                        cpuhog_ips = None
                        if arm in ("B", "C", "W", "K") and cpuhog_out.is_file():
                            with open(cpuhog_out, "r", encoding="utf-8") as f:
                                cpuhog_ips = json.load(f).get("iters_per_s")

                        meta = {
                            "rep": rep_idx,
                            "arm": arm,
                            "rate": rate,
                            "seconds": seconds,
                            "warmup": warmup_s,
                            "apply_late": apply_late,
                            "cpuhog_ips": cpuhog_ips,
                        }
                        raw_file = raw_path / f"rep{rep_idx}_{arm}.json.gz"
                        try:
                            save_raw(raw_file, records, errors, meta)
                        except OSError:
                            pass

                        rep_data = {
                            "records": records,
                            "errors": errors,
                            "seconds": seconds,
                            "cpuhog_ips": cpuhog_ips,
                            "apply_late": apply_late,
                        }
                        runs[arm].append(rep_data)

                        if arm == "A" and first_a_p99 is None:
                            a_lats = [r[1] for r in records]
                            first_a_p99 = float(np.percentile(a_lats, 99)) if a_lats else 0.0

                        print(f"Rep {rep_idx + 1}/{reps} arm {arm} finished")

                    finally:
                        for u in cond_units:
                            try:
                                subprocess.run(
                                    ["systemctl", "--user", "stop", f"{u}.scope"],
                                    check=False,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                            except Exception:
                                pass
                            try:
                                subprocess.run(
                                    ["systemctl", "--user", "reset-failed", f"{u}.scope"],
                                    check=False,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                            except Exception:
                                pass
                            cleaned_units.add(u)

                        if cond_apply_ran:
                            try:
                                from irm.execute import revert
                                revert(tmp_db, "/sys/fs/cgroup")
                                if tmp_db.is_file():
                                    for _ in range(10):
                                        with sqlite3.connect(tmp_db) as conn:
                                            cur = conn.cursor()
                                            cur.execute("select count(*) from sqlite_master where type='table' and name='journal'")
                                            if cur.fetchone()[0] == 0:
                                                break
                                            cur.execute("select max(batch) from journal where status in ('applied', 'revert_skipped:changed')")
                                            row = cur.fetchone()
                                            if not row or row[0] is None:
                                                break
                                        revert(tmp_db, "/sys/fs/cgroup")
                            except Exception:
                                pass

        finally:
            for u in all_started_units:
                if u not in cleaned_units:
                    try:
                        subprocess.run(
                            ["systemctl", "--user", "stop", f"{u}.scope"],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception:
                        pass
                    try:
                        subprocess.run(
                            ["systemctl", "--user", "reset-failed", f"{u}.scope"],
                            check=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception:
                        pass
                    cleaned_units.add(u)

            for db in applied_dbs:
                try:
                    from irm.execute import revert
                    revert(db, "/sys/fs/cgroup")
                    if db.is_file():
                        for _ in range(10):
                            with sqlite3.connect(db) as conn:
                                cur = conn.cursor()
                                cur.execute("select count(*) from sqlite_master where type='table' and name='journal'")
                                if cur.fetchone()[0] == 0:
                                    break
                                cur.execute("select max(batch) from journal where status in ('applied', 'revert_skipped:changed')")
                                row = cur.fetchone()
                                if not row or row[0] is None:
                                    break
                            revert(db, "/sys/fs/cgroup")
                except Exception:
                    pass

    slo_target_ms = (2.0 * first_a_p99) if first_a_p99 is not None else 0.0
    summary = summarize(runs, slo_target_ms, first_a_p99=first_a_p99)
    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    minutes_val = int(minutes) if isinstance(minutes, float) and minutes.is_integer() else minutes
    rate_val = int(rate) if isinstance(rate, float) and rate.is_integer() else rate

    res = {
        "slo_target_ms": slo_target_ms,
        "iters": iters,
        "minutes": minutes_val,
        "reps": reps,
        "rate": rate_val,
        "warmup_s": int(round(warmup_s)),
        "arms": list(arms),
        "conditions": summary,
        "applied_plans": applied_plans,
        "applied_plan": applied_plans.get("C", []),
        "raw_dir": str(raw_path),
        "apply_late_runs": apply_late_runs,
        "started_at": started_at,
        "finished_at": finished_at,
        "metric": "fraction of 1-second windows whose end-to-end p99 exceeds slo_target_ms",
    }

    out_p = Path(out_json)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


# --- Main entrypoint for python -m irm.experiment ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="irm.experiment")
    subparsers = parser.add_subparsers(dest="role", required=True)

    service_p = subparsers.add_parser("service")
    service_p.add_argument("--port", type=int, required=True)
    service_p.add_argument("--iters", type=int, default=None)

    loadgen_p = subparsers.add_parser("loadgen")
    loadgen_p.add_argument("--port", type=int, required=True)
    loadgen_p.add_argument("--rate", type=float, required=True)
    loadgen_p.add_argument("--seconds", type=float, required=True)
    loadgen_p.add_argument("--warmup", type=float, default=0.0)
    loadgen_p.add_argument("--out", type=str, required=True)
    loadgen_p.add_argument("--seed", type=int, default=0)

    cpuhog_p = subparsers.add_parser("cpuhog")
    cpuhog_p.add_argument("--procs", type=int, default=os.cpu_count() or 1)
    cpuhog_p.add_argument("--delay", type=float, default=0.0)
    cpuhog_p.add_argument("--seconds", type=float, required=True)
    cpuhog_p.add_argument("--start-flag", "--flag", dest="start_flag", type=str, default=None)
    cpuhog_p.add_argument("--out", type=str, required=True)

    nethog_rx_p = subparsers.add_parser("nethog-rx")
    nethog_rx_p.add_argument("--port", type=int, required=True)
    nethog_rx_p.add_argument("--seconds", type=float, required=True)

    nethog_tx_p = subparsers.add_parser("nethog-tx")
    nethog_tx_p.add_argument("--port", type=int, required=True)
    nethog_tx_p.add_argument("--seconds", type=float, required=True)
    nethog_tx_p.add_argument("--senders", type=int, default=2)

    nethog_p = subparsers.add_parser("nethog")
    nethog_p.add_argument("--seconds", type=float, required=True)

    args = parser.parse_args(argv)

    if args.role == "service":
        asyncio.run(run_service(args.port, iters=args.iters))
    elif args.role == "loadgen":
        res = asyncio.run(
            run_loadgen(
                args.port,
                args.rate,
                args.seconds,
                seed=args.seed,
                warmup=args.warmup,
            )
        )
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(res, indent=2), encoding="utf-8")
    elif args.role == "cpuhog":
        run_cpuhog(
            args.procs,
            args.seconds,
            args.out,
            delay=args.delay,
            start_flag=args.start_flag,
        )
    elif args.role == "nethog-rx":
        run_nethog_rx(args.port, args.seconds)
    elif args.role == "nethog-tx":
        run_nethog_tx(args.port, args.seconds, senders=args.senders)
    elif args.role == "nethog":
        run_nethog(args.seconds)

    return 0


if __name__ == "__main__":
    sys.exit(main())
