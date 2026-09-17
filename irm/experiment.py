import argparse
import asyncio
import datetime
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import random
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Sequence
import numpy as np

from irm import HOME


# --- Pure helpers ---


def rotation(reps: int) -> list[list[str]]:
    """Generate rotation order of conditions: ["A", "B", "C"], ["B", "C", "A"], ["C", "A", "B"], then repeat."""
    base = [
        ["A", "B", "C"],
        ["B", "C", "A"],
        ["C", "A", "B"],
    ]
    return [list(base[i % 3]) for i in range(reps)]


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


def summarize(runs: dict[str, list[dict] | dict], target_ms: float) -> dict[str, dict]:
    """Summarize run records per condition."""
    summary: dict[str, dict] = {}
    for cond, rep_data in runs.items():
        reps = rep_data if isinstance(rep_data, list) else [rep_data]

        all_records = []
        all_lats = []
        rep_p99s = []
        all_windows = []
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
            }
        else:
            rep_p99_ms = {"min": 0.0, "mean": 0.0, "max": 0.0}

        v_rate = violation_rate(all_windows, target_ms)
        rps = float(len(all_records) / total_seconds) if total_seconds > 0 else 0.0
        cpuhog_ips = None if cond == "A" else (float(np.mean(cpuhog_vals)) if cpuhog_vals else None)

        summary[cond] = {
            "p50_ms": pcts["p50_ms"],
            "p95_ms": pcts["p95_ms"],
            "p99_ms": pcts["p99_ms"],
            "rep_p99_ms": rep_p99_ms,
            "violation_rate": v_rate,
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


async def run_service(port: int, ready_fn=None, stop_event=None) -> None:
    """Service role: asyncio HTTP/1.1 keep-alive server on 127.0.0.1."""
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
    while True:
        gap = rng.expovariate(rate)
        t += gap
        if t >= seconds:
            break
        schedule.append(t)

    t_start = time.monotonic()

    async def send_one(offset_s: float, sched_mono: float):
        nonlocal errors
        w = None
        try:
            r, w = await pool.get()
        except Exception:
            errors += 1
            return

        try:
            w.write(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await w.drain()
            await r.readuntil(b"\r\n\r\n")
            await r.readexactly(2)
            comp_mono = time.monotonic()
            lat_ms = (comp_mono - sched_mono) * 1000.0
            records.append([offset_s, lat_ms])
            pool.put_nowait((r, w))
        except asyncio.CancelledError:
            errors += 1
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
            raise
        except Exception:
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


def _cpu_worker(idx: int, counts, seconds: float, stop_event):
    t_end = time.monotonic() + seconds
    local_count = 0
    while not stop_event.is_set() and time.monotonic() < t_end:
        for _ in range(10000):
            local_count += 1
        counts[idx] = local_count
    counts[idx] = local_count


def run_cpuhog(procs: int, seconds: float, out: str | Path) -> dict:
    """CPU hog role: N multiprocessing busy-loop workers counting iterations."""
    counts = multiprocessing.RawArray("q", procs)
    stop_event = multiprocessing.Event()
    workers = []
    for i in range(procs):
        p = multiprocessing.Process(
            target=_cpu_worker,
            args=(i, counts, seconds, stop_event),
            daemon=True,
        )
        workers.append(p)

    for p in workers:
        p.start()

    def handle_signal(signum, frame):
        stop_event.set()

    old_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    old_sigint = signal.signal(signal.SIGINT, handle_signal)

    try:
        for p in workers:
            p.join(timeout=max(0.1, seconds + 5.0))
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
) -> dict:
    """SLO experiment driver managing transient scopes across rotation conditions."""
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    seconds = float(minutes * 60.0)
    plan = rotation(reps)
    runs: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    first_a_p99: float | None = None
    applied_plan: list[dict] = []

    all_started_units: list[str] = []
    last_applied_db: Path | None = None

    try:
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            for rep_idx, cond_list in enumerate(plan):
                for cond in cond_list:
                    cond_units: list[str] = []
                    cond_apply_ran = False
                    tmp_db = tmp_dir / f"monitor_{rep_idx}_{cond}.db"

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
                        seed = rep_idx * 100 + ord(cond)
                        loadgen_out = tmp_dir / f"loadgen_{rep_idx}_{cond}.json"
                        cpuhog_out = tmp_dir / f"cpuhog_{rep_idx}_{cond}.json"

                        if cond == "A":
                            srv_proc, srv_unit, srv_cg = start_role("service", ["--port", str(port)])
                            wait_for_port(port, timeout=10.0)
                            lg_proc, lg_unit, lg_cg = start_role(
                                "loadgen",
                                [
                                    "--port", str(port),
                                    "--rate", str(rate),
                                    "--seconds", str(seconds),
                                    "--out", str(loadgen_out),
                                    "--seed", str(seed),
                                ],
                            )
                            lg_proc.wait()
                            with open(loadgen_out, "r", encoding="utf-8") as f:
                                lg_data = json.load(f)
                            rep_data = {
                                "records": lg_data["records"],
                                "errors": lg_data["errors"],
                                "seconds": seconds,
                                "cpuhog_ips": None,
                            }
                            runs["A"].append(rep_data)
                            if first_a_p99 is None:
                                a_lats = [r[1] for r in lg_data.get("records", [])]
                                if a_lats:
                                    first_a_p99 = float(np.percentile(a_lats, 99))

                        elif cond == "B":
                            srv_proc, srv_unit, srv_cg = start_role("service", ["--port", str(port)])
                            wait_for_port(port, timeout=10.0)
                            procs_count = os.cpu_count() or 1
                            cpu_seconds = 10.0 + seconds
                            cpu_proc, cpu_unit, cpu_cg = start_role(
                                "cpuhog",
                                ["--procs", str(procs_count), "--seconds", str(cpu_seconds), "--out", str(cpuhog_out)],
                            )
                            net_proc, net_unit, net_cg = start_role(
                                "nethog",
                                ["--seconds", str(cpu_seconds + 5.0)],
                            )
                            time.sleep(10.0)
                            lg_proc, lg_unit, lg_cg = start_role(
                                "loadgen",
                                [
                                    "--port", str(port),
                                    "--rate", str(rate),
                                    "--seconds", str(seconds),
                                    "--out", str(loadgen_out),
                                    "--seed", str(seed),
                                ],
                            )
                            lg_proc.wait()
                            try:
                                cpu_proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                pass
                            with open(loadgen_out, "r", encoding="utf-8") as f:
                                lg_data = json.load(f)
                            cpuhog_ips = None
                            if cpuhog_out.is_file():
                                with open(cpuhog_out, "r", encoding="utf-8") as f:
                                    cpuhog_ips = json.load(f).get("iters_per_s")
                            rep_data = {
                                "records": lg_data["records"],
                                "errors": lg_data["errors"],
                                "seconds": seconds,
                                "cpuhog_ips": cpuhog_ips,
                            }
                            runs["B"].append(rep_data)

                        elif cond == "C":
                            srv_proc, srv_unit, srv_cg = start_role("service", ["--port", str(port)])
                            wait_for_port(port, timeout=10.0)
                            procs_count = os.cpu_count() or 1
                            cpu_seconds = 65.0 + seconds
                            cpu_proc, cpu_unit, cpu_cg = start_role(
                                "cpuhog",
                                ["--procs", str(procs_count), "--seconds", str(cpu_seconds), "--out", str(cpuhog_out)],
                            )
                            net_proc, net_unit, net_cg = start_role(
                                "nethog",
                                ["--seconds", str(cpu_seconds + 5.0)],
                            )
                            from irm.monitor import run as monitor_run
                            monitor_run(tmp_db, "/sys/fs/cgroup", "/proc", interval=1.0, retention_hours=1.0, duration=60.0)
                            from irm.recommend import recommend
                            recs = recommend(
                                tmp_db,
                                "/sys/fs/cgroup",
                                protect=[srv_cg],
                                only=[cpu_cg, net_cg],
                                min_samples=30,
                                hours=1.0,
                                min_cores=0.0,
                            )
                            from irm.execute import apply
                            apply(tmp_db, recs, "/sys/fs/cgroup", allow=[], yes=True)
                            cond_apply_ran = True
                            last_applied_db = tmp_db
                            if not applied_plan:
                                applied_plan = recs.get("items", [])
                            lg_proc, lg_unit, lg_cg = start_role(
                                "loadgen",
                                [
                                    "--port", str(port),
                                    "--rate", str(rate),
                                    "--seconds", str(seconds),
                                    "--out", str(loadgen_out),
                                    "--seed", str(seed),
                                ],
                            )
                            lg_proc.wait()
                            try:
                                cpu_proc.wait(timeout=5.0)
                            except subprocess.TimeoutExpired:
                                pass
                            with open(loadgen_out, "r", encoding="utf-8") as f:
                                lg_data = json.load(f)
                            cpuhog_ips = None
                            if cpuhog_out.is_file():
                                with open(cpuhog_out, "r", encoding="utf-8") as f:
                                    cpuhog_ips = json.load(f).get("iters_per_s")
                            rep_data = {
                                "records": lg_data["records"],
                                "errors": lg_data["errors"],
                                "seconds": seconds,
                                "cpuhog_ips": cpuhog_ips,
                            }
                            runs["C"].append(rep_data)

                        print(f"Rep {rep_idx + 1}/{reps} condition {cond} finished")

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
                        if cond_apply_ran:
                            try:
                                from irm.execute import revert
                                revert(tmp_db, "/sys/fs/cgroup")
                            except Exception:
                                pass

    finally:
        for u in all_started_units:
            try:
                subprocess.run(
                    ["systemctl", "--user", "stop", f"{u}.scope"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        if last_applied_db is not None and last_applied_db.is_file():
            try:
                from irm.execute import revert
                revert(last_applied_db, "/sys/fs/cgroup")
            except Exception:
                pass

    slo_target_ms = (2.0 * first_a_p99) if first_a_p99 is not None else 0.0
    summary = summarize(runs, slo_target_ms)
    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    minutes_val = int(minutes) if isinstance(minutes, float) and minutes.is_integer() else minutes
    rate_val = int(rate) if isinstance(rate, float) and rate.is_integer() else rate

    res = {
        "slo_target_ms": slo_target_ms,
        "minutes": minutes_val,
        "reps": reps,
        "rate": rate_val,
        "conditions": summary,
        "applied_plan": applied_plan,
        "started_at": started_at,
        "finished_at": finished_at,
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

    loadgen_p = subparsers.add_parser("loadgen")
    loadgen_p.add_argument("--port", type=int, required=True)
    loadgen_p.add_argument("--rate", type=float, required=True)
    loadgen_p.add_argument("--seconds", type=float, required=True)
    loadgen_p.add_argument("--out", type=str, required=True)
    loadgen_p.add_argument("--seed", type=int, default=0)

    cpuhog_p = subparsers.add_parser("cpuhog")
    cpuhog_p.add_argument("--procs", type=int, default=os.cpu_count() or 1)
    cpuhog_p.add_argument("--seconds", type=float, required=True)
    cpuhog_p.add_argument("--out", type=str, required=True)

    nethog_p = subparsers.add_parser("nethog")
    nethog_p.add_argument("--seconds", type=float, required=True)

    args = parser.parse_args(argv)

    if args.role == "service":
        asyncio.run(run_service(args.port))
    elif args.role == "loadgen":
        res = asyncio.run(run_loadgen(args.port, args.rate, args.seconds, seed=args.seed))
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(res, indent=2), encoding="utf-8")
    elif args.role == "cpuhog":
        run_cpuhog(args.procs, args.seconds, args.out)
    elif args.role == "nethog":
        run_nethog(args.seconds)

    return 0


if __name__ == "__main__":
    sys.exit(main())
