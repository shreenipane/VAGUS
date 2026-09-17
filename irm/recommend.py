import math
import os
from pathlib import Path
import sqlite3
import time
import numpy as np


def _read_old(root: str | Path, cgroup: str) -> dict[str, str | None]:
    cg_dir = Path(root) / cgroup.lstrip("/")
    old: dict[str, str | None] = {}
    for f in ("cpu.max", "memory.high", "cpu.weight"):
        p = cg_dir / f
        if p.is_file():
            try:
                old[f] = p.read_text(encoding="utf-8").strip()
            except OSError:
                old[f] = None
        else:
            old[f] = None
    return old


def recommend(
    db_path: str | Path,
    root: str | Path,
    protect: list[str] | set[str] | tuple[str, ...] | None = None,
    headroom: float = 1.25,
    min_samples: int = 360,
    hours: float = 24.0,
    now: int | float | None = None,
    min_cores: float = 0.5,
    only: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict:
    if now is None:
        now_ts = int(time.time())
    else:
        now_ts = int(now)

    t_min = now_ts - int(hours * 3600)
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"

    qualifying: dict[str, dict] = {}
    cgroup_buckets: dict[str, dict[int, float]] = {}
    skipped: list[dict[str, str]] = []

    with sqlite3.connect(uri, uri=True) as conn:
        cur = conn.cursor()
        cur.execute(
            "select distinct cgroup from samples where ts >= ? and ts <= ? and cgroup != 'host' order by cgroup",
            (t_min, now_ts),
        )
        cgroups = [row[0] for row in cur.fetchall()]

        for cg in cgroups:
            cur.execute(
                "select ts, cpu_cores, netrx_attrib_cores, mem_bytes from samples "
                "where cgroup = ? and ts >= ? and ts <= ? order by ts asc",
                (cg, t_min, now_ts),
            )
            rows = cur.fetchall()
            valid_cpu = [r for r in rows if r[1] is not None]

            if len(valid_cpu) < min_samples:
                skipped.append({
                    "cgroup": cg,
                    "reason": f"fewer than {min_samples} samples ({len(valid_cpu)} found)",
                })
                continue

            d_list = [r[1] + (r[2] or 0.0) for r in valid_cpu]
            peak = float(np.percentile(d_list, 95))
            mem_vals = [r[3] for r in rows if r[3] is not None]
            max_mem = max(mem_vals) if mem_vals else 0.0

            buckets: dict[int, float] = {}
            for r in valid_cpu:
                b = (r[0] // 30) * 30
                d_val = r[1] + (r[2] or 0.0)
                if b not in buckets or d_val > buckets[b]:
                    buckets[b] = d_val

            qualifying[cg] = {
                "peak": peak,
                "max_mem": max_mem,
            }
            cgroup_buckets[cg] = buckets

    if isinstance(protect, str):
        protect_set = {protect}
    else:
        protect_set = set(protect or [])

    if only is not None:
        only_set = {only} if isinstance(only, str) else set(only)
    else:
        only_set = None

    ncpu = os.cpu_count() or 1

    protected_cgs: list[str] = []
    other_cgs: list[str] = []
    background_cgs: list[str] = []

    for cg in qualifying:
        if cg in protect_set:
            protected_cgs.append(cg)
        elif only_set is not None:
            if cg in only_set:
                other_cgs.append(cg)
            else:
                background_cgs.append(cg)
                skipped.append({
                    "cgroup": cg,
                    "reason": "background: not in --only",
                })
        else:
            if qualifying[cg]["peak"] >= min_cores:
                other_cgs.append(cg)
            else:
                background_cgs.append(cg)
                skipped.append({
                    "cgroup": cg,
                    "reason": f"background: peak {qualifying[cg]['peak']:.2f} cores < {min_cores:.2f}",
                })

    reserve = sum(qualifying[cg]["peak"] * headroom for cg in protected_cgs)
    background = sum(qualifying[cg]["peak"] for cg in background_cgs)
    avail = max(0.0, ncpu - reserve - background)

    sum_want = sum(qualifying[cg]["peak"] * headroom for cg in other_cgs)
    scale = (avail / sum_want) if sum_want > avail else 1.0

    items: list[dict] = []
    MIB = 1024 * 1024

    for cg in protected_cgs:
        peak = qualifying[cg]["peak"]
        items.append({
            "cgroup": cg,
            "source": "empirical",
            "peak_cores": round(peak, 4),
            "cpu_max": "max",
            "memory_high": "max",
            "cpu_weight": 1000,
            "old": _read_old(root, cg),
            "reason": f"protected; peak {peak:.2f} cores (empirical)",
        })

    for cg in other_cgs:
        peak = qualifying[cg]["peak"]
        max_mem = qualifying[cg]["max_mem"]
        want = peak * headroom
        scaled = want * scale
        if scaled < 0.1:
            quota = 0.1
            raised = True
        else:
            quota = scaled
            raised = False

        if quota >= 0.9 * ncpu:
            cpu_max = "max"
        else:
            quota_us = int(round(quota * 100000))
            cpu_max = f"{quota_us} 100000"

        mem_scaled = max_mem * headroom
        memory_high = int(max(64 * MIB, math.ceil(mem_scaled / MIB) * MIB))

        if sum_want > avail:
            reason = (
                f"q95 {peak:.2f} cores (empirical) × {headroom:.2f}; "
                f"squeezed to fit {avail:.2f} free cores after {reserve:.2f} reserved and {background:.2f} background"
            )
        else:
            reason = f"q95 {peak:.2f} cores (empirical) × {headroom:.2f}"

        if raised:
            reason += "; raised to the 0.1-core floor"

        items.append({
            "cgroup": cg,
            "source": "empirical",
            "peak_cores": round(peak, 4),
            "cpu_max": cpu_max,
            "memory_high": memory_high,
            "cpu_weight": None,
            "old": _read_old(root, cg),
            "reason": reason,
        })

    items.sort(key=lambda it: it["cgroup"])
    skipped.sort(key=lambda s: s["cgroup"])

    pairs: list[dict] = []
    pair_cgs = sorted([cg for cg in qualifying if qualifying[cg]["peak"] >= 0.5])
    for i in range(len(pair_cgs)):
        for j in range(i + 1, len(pair_cgs)):
            cg_a, cg_b = pair_cgs[i], pair_cgs[j]
            b_a = cgroup_buckets[cg_a]
            b_b = cgroup_buckets[cg_b]
            common = sorted(set(b_a.keys()) & set(b_b.keys()))
            if len(common) >= 10:
                va = np.array([b_a[k] for k in common], dtype=float)
                vb = np.array([b_b[k] for k in common], dtype=float)
                vara = float(np.var(va))
                varb = float(np.var(vb))
                if vara > 1e-12 and varb > 1e-12:
                    rho = float(np.corrcoef(va, vb)[0, 1])
                    if not np.isnan(rho) and rho >= 0.7:
                        k = (1.0 - rho) / 2.0
                        pairs.append({
                            "a": cg_a,
                            "b": cg_b,
                            "rho": round(rho, 4),
                            "k": round(k, 4),
                        })

    pairs.sort(key=lambda p: (-p["rho"], p["a"], p["b"]))
    pairs = pairs[:10]

    return {
        "generated_at": now_ts,
        "ncpu": ncpu,
        "items": items,
        "pairs": pairs,
        "skipped": skipped,
    }
