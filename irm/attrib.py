import json
import os
from pathlib import Path
import threading
import time

NET_RX = 3


def attribute(prev: dict, cur: dict) -> dict | None:
    try:
        cur_ts = float(cur["ts"])
        prev_ts = float(prev["ts"])
        dt = cur_ts - prev_ts
        if dt <= 0:
            return None

        ncpu = int(cur["ncpu"])
        if ncpu != int(prev["ncpu"]) or ncpu <= 0:
            return None

        cur_vec = cur["vec_ns"]
        prev_vec = prev["vec_ns"]
        if len(cur_vec) != 10 or len(prev_vec) != 10:
            return None

        for v in range(10):
            cv = cur_vec[v]
            pv = prev_vec[v]
            if len(cv) != ncpu or len(pv) != ncpu:
                return None
            for c in range(ncpu):
                if cv[c] < pv[c]:
                    return None

        cur_rx = cur.get("rx_pkts", {})
        prev_rx = prev.get("rx_pkts", {})
        for g in prev_rx:
            if g not in cur_rx:
                return None

        for g, pkts_cur in cur_rx.items():
            if len(pkts_cur) != ncpu:
                return None
            pkts_prev = prev_rx.get(g)
            if pkts_prev is not None:
                if len(pkts_prev) != ncpu:
                    return None
                for c in range(ncpu):
                    if pkts_cur[c] < pkts_prev[c]:
                        return None
            else:
                for c in range(ncpu):
                    if pkts_cur[c] < 0:
                        return None

        cur_b = cur.get("blamed_ns", {})
        prev_b = prev.get("blamed_ns", {})
        for g in prev_b:
            if g not in cur_b:
                return None

        for g, b_cur in cur_b.items():
            if len(b_cur) != 10:
                return None
            b_prev = prev_b.get(g)
            if b_prev is not None:
                if len(b_prev) != 10:
                    return None
                for v in range(10):
                    if b_cur[v] < b_prev[v]:
                        return None
            else:
                for v in range(10):
                    if b_cur[v] < 0:
                        return None

        attrib_ns: dict[int, float] = {int(g): 0.0 for g in cur_rx}
        unattrib_ns = 0.0

        for c in range(ncpu):
            r_c = cur_vec[NET_RX][c] - prev_vec[NET_RX][c]
            p_c = 0
            deltas: dict[int, int] = {}
            for g, pkts_cur in cur_rx.items():
                prev_p = prev_rx[g][c] if g in prev_rx else 0
                delta = pkts_cur[c] - prev_p
                deltas[int(g)] = delta
                p_c += delta

            if p_c > 0:
                for int_g, delta in deltas.items():
                    if delta > 0:
                        attrib_ns[int_g] += r_c * delta / p_c
            else:
                unattrib_ns += r_c

        attrib_cores = {g: ns / 1e9 / dt for g, ns in attrib_ns.items()}
        unattrib_cores = unattrib_ns / 1e9 / dt

        blamed_cores: dict[int, float] = {}
        for g, b_cur in cur_b.items():
            prev_b3 = prev_b[g][NET_RX] if g in prev_b else 0
            blamed_cores[int(g)] = (b_cur[NET_RX] - prev_b3) / 1e9 / dt

        vec_cores = [
            sum(cur_vec[v][c] - prev_vec[v][c] for c in range(ncpu)) / 1e9 / dt
            for v in range(10)
        ]

        return {
            "dt": dt,
            "attrib_cores": attrib_cores,
            "blamed_cores": blamed_cores,
            "unattrib_cores": unattrib_cores,
            "vec_cores": vec_cores,
        }
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def cgroup_ids(root: str | Path) -> dict[int, str]:
    root_str = str(root)
    res: dict[int, str] = {}
    try:
        res[os.stat(root_str).st_ino] = "/"
    except OSError:
        return res

    for dirpath, dirnames, _ in os.walk(root_str):
        rel = os.path.relpath(dirpath, root_str)
        if rel == ".":
            continue
        try:
            res[os.stat(dirpath).st_ino] = f"/{rel}"
        except OSError:
            pass
    return res


class Reader:
    def __init__(self, stream, clock=time.monotonic):
        self.stream = stream
        self.clock = clock
        self.invalid = 0
        self._lock = threading.Lock()
        self._prev: dict | None = None
        self._latest_time: float | None = None
        self._latest_result: dict | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for line in self.stream:
                try:
                    cur = json.loads(line)
                    if not isinstance(cur, dict):
                        self.invalid += 1
                        continue
                except Exception:
                    self.invalid += 1
                    continue

                if self._prev is not None:
                    res = attribute(self._prev, cur)
                    if res is not None:
                        now = self.clock()
                        with self._lock:
                            self._latest_time = now
                            self._latest_result = res
                self._prev = cur
        except Exception:
            pass

    def latest(self, max_age: float | None = None) -> dict | None:
        with self._lock:
            if self._latest_result is None or self._latest_time is None:
                return None
            if max_age is not None:
                if (self.clock() - self._latest_time) > max_age:
                    return None
            return self._latest_result

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)
