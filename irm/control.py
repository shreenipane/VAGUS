from pathlib import Path
import time
from typing import Any


def default_read_psi(root: str | Path, cgroup: str) -> float | None:
    path = Path(root) / cgroup.lstrip("/") / "cpu.pressure"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("some "):
            for token in line.split():
                if token.startswith("avg10="):
                    try:
                        return float(token[6:])
                    except ValueError:
                        return None
    return None


def _normalize(file: str, val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if file == "cpu.max" and s == "max":
        return "max 100000"
    return s


def _item_has_change(root: str | Path, it: dict) -> bool:
    cg = it.get("cgroup", "")
    cg_dir = Path(root) / cg.lstrip("/")
    for key, file in (("cpu_max", "cpu.max"), ("memory_high", "memory.high"), ("cpu_weight", "cpu.weight")):
        if key in it and it[key] is not None:
            cur_file = cg_dir / file
            cur_val = None
            if cur_file.is_file():
                try:
                    cur_val = cur_file.read_text(encoding="utf-8").strip()
                except OSError:
                    cur_val = None
            else:
                old = it.get("old")
                if isinstance(old, dict) and file in old and old[file] is not None:
                    cur_val = str(old[file]).strip()

            if cur_val is None:
                return True
            if _normalize(file, it[key]) != _normalize(file, cur_val):
                return True
    return False


def _mean_psi(root: str | Path, cgroups: list[str], read_fn) -> float:
    vals = []
    for cg in cgroups:
        v = read_fn(root, cg)
        if v is not None:
            vals.append(v)
    return (sum(vals) / len(vals)) if vals else 0.0


def run(
    db_path: str | Path,
    root: str | Path,
    proc: str | Path,
    protect: list[str] | set[str] | tuple[str, ...] | str | None,
    only: list[str] | set[str] | tuple[str, ...] | str | None,
    interval: float = 60.0,
    settle: float = 30.0,
    psi_margin: float = 5.0,
    max_iterations: int | None = None,
    min_samples: int = 12,
    hours: float = 1.0,
    headroom: float = 1.25,
    min_cores: float = 0.0,
    recommend_fn=None,
    apply_fn=None,
    revert_fn=None,
    read_psi=None,
    sleep=time.sleep,
    log=print,
) -> int:
    if recommend_fn is None:
        from irm.recommend import recommend as recommend_fn
    if apply_fn is None:
        from irm.execute import apply as apply_fn
    if revert_fn is None:
        from irm.execute import revert as revert_fn
    if read_psi is None:
        read_psi = default_read_psi

    if isinstance(protect, str):
        protect_list = [protect]
    elif protect:
        protect_list = list(protect)
    else:
        protect_list = []

    applied_batches: list[int] = []
    had_failure = False
    iteration = 0

    if max_iterations is not None and max_iterations <= 0:
        return 0

    try:
        while True:
            iteration += 1
            recs = recommend_fn(
                db_path,
                root,
                protect=protect,
                only=only,
                min_samples=min_samples,
                hours=hours,
                headroom=headroom,
                min_cores=min_cores,
            )

            items = recs.get("items", []) if isinstance(recs, dict) else []
            has_change = any(_item_has_change(root, it) for it in items)

            if not items or not has_change:
                log(f"iteration {iteration}: no change")
                sleep(interval)
                if max_iterations is not None and iteration >= max_iterations:
                    break
                continue

            before = _mean_psi(root, protect_list, read_psi)
            apply_ret = apply_fn(db_path, recs, root, yes=True)
            if apply_ret != 0:
                had_failure = True
                log(f"iteration {iteration}: apply failed")
                sleep(interval)
                if max_iterations is not None and iteration >= max_iterations:
                    break
                continue

            applied_batches.append(iteration)
            sleep(settle)
            after = _mean_psi(root, protect_list, read_psi)

            if after > before + psi_margin:
                applied_batches.pop()
                rev_ret = revert_fn(db_path, root)
                if rev_ret != 0:
                    had_failure = True
                b_disp = round(before, 2) if isinstance(before, (int, float)) else before
                a_disp = round(after, 2) if isinstance(after, (int, float)) else after
                log(f"iteration {iteration}: rollback (protected PSI before {b_disp} → after {a_disp})")
            else:
                b_disp = round(before, 2) if isinstance(before, (int, float)) else before
                a_disp = round(after, 2) if isinstance(after, (int, float)) else after
                log(f"iteration {iteration}: kept (PSI {b_disp} → {a_disp})")

            rem_sleep = max(0.0, interval - settle)
            sleep(rem_sleep)
            if max_iterations is not None and iteration >= max_iterations:
                break
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        had_failure = True
        log(f"error: {exc}")
    finally:
        while applied_batches:
            applied_batches.pop()
            try:
                rev_ret = revert_fn(db_path, root)
                if rev_ret != 0:
                    had_failure = True
            except Exception:
                had_failure = True

    return 1 if had_failure else 0
