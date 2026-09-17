import errno
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


def validate(
    root: str | Path,
    cgroup: str,
    file: str,
    value: str | int,
    allow: list[str] | set[str] | tuple[str, ...] | None = None,
) -> str | None:
    if not isinstance(cgroup, str) or not cgroup.startswith("/"):
        return f"cgroup '{cgroup}' must start with '/'"
    if "\0" in cgroup:
        return f"cgroup '{cgroup}' contains NUL character"
    if ".." in cgroup.split("/"):
        return f"cgroup '{cgroup}' contains '..' component"

    uid = os.getuid()
    root_resolved = Path(root).resolve()
    target_dir = (root_resolved / cgroup.lstrip("/")).resolve()

    allowed_prefixes = [(root_resolved / f"user.slice/user-{uid}.slice/user@{uid}.service").resolve()]
    if allow:
        for entry in allow:
            if not isinstance(entry, str) or not entry.startswith("/"):
                continue
            if ".." in entry.split("/"):
                continue
            pfx_path = (root_resolved / entry.lstrip("/")).resolve()
            try:
                cp = os.path.commonpath([str(root_resolved), str(pfx_path)])
                if cp == str(root_resolved) and str(pfx_path) != str(root_resolved):
                    allowed_prefixes.append(pfx_path)
            except ValueError:
                pass

    strictly_inside = False
    for pfx_path in allowed_prefixes:
        try:
            cp = os.path.commonpath([str(pfx_path), str(target_dir)])
            if cp == str(pfx_path) and str(target_dir) != str(pfx_path):
                strictly_inside = True
                break
        except ValueError:
            pass

    if not strictly_inside:
        return f"cgroup '{cgroup}' is not strictly inside any allowed prefix"

    controllers_file = target_dir / "cgroup.controllers"
    if not controllers_file.is_file():
        return f"cgroup.controllers does not exist in '{cgroup}'"

    if file not in ("cpu.max", "memory.high", "cpu.weight"):
        return f"disallowed file '{file}'"

    val_str = str(value) if value is not None else ""
    if file == "cpu.max":
        if val_str in ("max", "max 100000"):
            pass
        else:
            m = re.fullmatch(r"([0-9]+) 100000", val_str)
            if not m:
                return f"invalid cpu.max value '{val_str}'"
            quota = int(m.group(1))
            if quota < 1000:
                return f"cpu.max quota {quota} < 1000"
    elif file == "memory.high":
        if val_str == "max":
            pass
        elif re.fullmatch(r"[0-9]+", val_str):
            val_int = int(val_str)
            if val_int < 67108864:
                return f"memory.high value {val_int} < 67108864 (64 MiB)"
        else:
            return f"invalid memory.high value '{val_str}'"
    elif file == "cpu.weight":
        if re.fullmatch(r"[0-9]+", val_str):
            val_int = int(val_str)
            if not (1 <= val_int <= 10000):
                return f"cpu.weight value {val_int} not in 1-10000"
        else:
            return f"invalid cpu.weight value '{val_str}'"

    return None


def apply(
    db_path: str | Path,
    recs: dict,
    root: str | Path,
    allow: list[str] | set[str] | tuple[str, ...] | None = None,
    yes: bool = False,
) -> int:
    has_error = False
    planned_ops = []
    root_path = Path(root)

    items = recs.get("items", [])
    for it in items:
        cg = it["cgroup"]
        changes: list[tuple[str, str]] = []
        if "cpu_max" in it and it["cpu_max"] is not None:
            changes.append(("cpu.max", str(it["cpu_max"])))
        if "memory_high" in it and it["memory_high"] is not None:
            changes.append(("memory.high", str(it["memory_high"])))
        if "cpu_weight" in it and it["cpu_weight"] is not None:
            changes.append(("cpu.weight", str(it["cpu_weight"])))

        for file, val in changes:
            clamped = False
            if file == "memory.high" and val != "max" and re.fullmatch(r"[0-9]+", val):
                cur_path = root_path / cg.lstrip("/") / "memory.current"
                if cur_path.is_file():
                    try:
                        cur_mem = int(cur_path.read_text(encoding="utf-8").strip())
                        min_req = cur_mem * 1.1
                        if int(val) < min_req:
                            MIB = 1024 * 1024
                            val_clamped = int(max(64 * MIB, math.ceil(min_req / MIB) * MIB))
                            val = str(val_clamped)
                            clamped = True
                    except (ValueError, OSError):
                        pass

            err = validate(root, cg, file, val, allow)
            if err is not None:
                print(f"Rejected {cg} {file}: {err}", file=sys.stderr)
                has_error = True
                continue

            target_file = root_path / cg.lstrip("/") / file
            old: str | None = None
            if target_file.is_file():
                try:
                    old = target_file.read_text(encoding="utf-8")
                except OSError:
                    old = None

            if old is not None and old.strip() == val.strip():
                continue

            planned_ops.append((cg, file, old, val, clamped, target_file))

    for cg, file, old, val, clamped, _ in planned_ops:
        old_disp = old.strip() if old is not None else "null"
        new_disp = val.strip()
        note = " (clamped)" if clamped else ""
        print(f"{cg}  {file}  {old_disp} → {new_disp}{note}")

    if not yes:
        return 1 if has_error else 0

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "create table if not exists journal ("
            "id integer primary key, "
            "batch integer not null, "
            "ts integer not null, "
            "cgroup text not null, "
            "file text not null, "
            "old text, "
            "new text not null, "
            "status text not null"
            ")"
        )
        conn.commit()

        cur.execute("select max(batch) from journal")
        row = cur.fetchone()
        batch = (row[0] or 0) + 1
        now_ts = int(time.time())

        for cg, file, old, val, _, target_file in planned_ops:
            cur.execute(
                "insert into journal (batch, ts, cgroup, file, old, new, status) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (batch, now_ts, cg, file, old, val, "planned"),
            )
            conn.commit()
            row_id = cur.lastrowid

            try:
                write_val = f"{val}\n" if (old is not None and old.endswith("\n")) else val
                target_file.write_text(write_val, encoding="utf-8")
                status = "applied"
            except OSError as exc:
                has_error = True
                errno_name = errno.errorcode.get(exc.errno, str(exc.errno))
                status = f"failed:{errno_name}"

            cur.execute("update journal set status = ? where id = ?", (status, row_id))
            conn.commit()

    return 1 if has_error else 0


def revert(
    db_path: str | Path,
    root: str | Path,
    batch: int | None = None,
) -> int:
    if not Path(db_path).is_file():
        return 0

    root_path = Path(root)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("select count(*) from sqlite_master where type='table' and name='journal'")
        if cur.fetchone()[0] == 0:
            return 0

        if batch is None:
            cur.execute("select max(batch) from journal where status = 'applied'")
            row = cur.fetchone()
            if not row or row[0] is None:
                return 0
            batch = row[0]

        cur.execute(
            "select id, cgroup, file, old, new from journal "
            "where batch = ? and status = 'applied' order by id desc",
            (batch,),
        )
        rows = cur.fetchall()
        has_error = False

        for row_id, cg, file, old, new in rows:
            if old is None:
                print(f"Skipping revert of {cg} {file}: old value is NULL")
                continue

            old_disp = old.strip()
            new_disp = new.strip()
            print(f"{cg}  {file}  {new_disp} → {old_disp}")

            target_file = root_path / cg.lstrip("/") / file
            try:
                target_file.write_text(old, encoding="utf-8")
                status = "reverted"
            except OSError as exc:
                has_error = True
                errno_name = errno.errorcode.get(exc.errno, str(exc.errno))
                status = f"revert_failed:{errno_name}"

            cur.execute("update journal set status = ? where id = ?", (status, row_id))
            conn.commit()

    return 1 if has_error else 0
