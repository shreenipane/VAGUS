# Task P10 — Security review fixes (fragment)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `SECURITY.md` T9, `TRD.md` §12, `irm/execute.py`, `irm/experiment.py` (`run_slo`),
`tests/test_execute.py`, `tests/test_experiment.py`.

Another agent is editing `bpf/*`. Touch **only** `irm/execute.py`, `irm/experiment.py`, `tests/test_execute.py`,
`tests/test_experiment.py`. Change the smallest regions that fix each finding; do not restructure.

## Findings from the security gate (all verified by probes)
1. **Critical — `validate()` does not sanitize `allow` entries.** `allow=["/"]` makes the prefix equal to root, so any
   cgroup passes; `allow=[".."]` or `["../.."]` resolves outside root. Fix: an `allow` entry is used only if it starts with
   `/`, has no `..` component, and its resolved path is **strictly inside** the resolved root (not equal to it). Invalid
   entries are ignored (they never widen the allowlist). The default user prefix is unaffected.
2. **High — `run_slo()` skips `revert` if `apply()` raises.** `cond_apply_ran = True` and `last_applied_db = tmp_db` are
   set only after `apply()` returns. Fix: set both immediately **before** calling `apply()` (revert is safe when nothing was
   applied).
3. **Medium — `str.isdigit()` accepts non-ASCII digits.** `"²"` passes `isdigit()` then crashes `int()`; `"٥٠٠"` is accepted.
   Fix: use `re.fullmatch(r"[0-9]+", …)` (ASCII) everywhere a number is validated (`cpu.max` quota, `memory.high`,
   `cpu.weight`), so bad values return a rejection reason and never raise.
4. **Low — weak cgroup check.** `if not controllers_file.is_file() and not controllers_file.exists()` passes when
   `cgroup.controllers` is a directory. Fix: `if not controllers_file.is_file():`.

## Tests to add (one per finding; each must fail if its fix is removed)
1. With a fake tree containing `/system.slice/x.service/cgroup.controllers`: `allow=["/"]`, `[".."]`, `["../.."]`, and
   `["relative"]` all still reject `/system.slice/x.service`; `allow=["/system.slice/"]` accepts it.
2. `run_slo` revert on apply failure: monkeypatch `start_role`-level collaborators is hard, so test the ordering directly:
   monkeypatch `irm.execute.apply` (as imported inside `run_slo`) to raise after being called, and monkeypatch the other
   collaborators (`subprocess.Popen`, `wait_for_cgroup`, `wait_for_port`, `irm.monitor.run`, `irm.recommend.recommend`,
   `subprocess.run`, `time.sleep`) with fakes so only condition C of one rep runs; assert `irm.execute.revert` was called.
   If `run_slo`'s structure makes this impossible without refactoring, extract the C-condition apply step into a small
   helper `apply_and_track(state, tmp_db, recs)` that sets the tracking fields before calling `apply`, test that helper,
   and say so.
3. `"²"`, `"٥٠٠"`, `" 5"`, `"5\n"` for `memory.high`, `cpu.weight`, and the quota of `cpu.max` → rejected with a reason, no
   exception.
4. `cgroup.controllers` as a directory → rejected.

Run `/home/shreenipane/.local/bin/irm-test tests/test_execute.py tests/test_experiment.py -q` until green. Print the
`AGENTS.md` §6 summary.
