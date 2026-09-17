# AGENTS.md — Standing rules for the coding agent

You are the coding agent for the Intelligent Resource Manager. The architect writes each task in
`.tasks/NN-name.md`, reviews your diff, and records the result. Your job: implement exactly the task, in the least
code that is correct and safe, **and run the tests until they pass**.

Read before every task: this file, then the TRD.md sections the task names.

## 1. Scope

- Repository root: `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Paths in specs are relative
  to it. Pass **absolute** paths to your file tools.
- You can write only inside `irm/`, `tests/`, and `bpf/`. Anything else is refused and ends your run. Never list, view,
  or search outside the repository.
- Touch **only** the files the task lists. Need another file? Stop and say so in your final summary.
- Do not delete or weaken existing tests. Do not rewrite a file that already passes: change the smallest region that
  does the job.
- If the task conflicts with TRD.md or SECURITY.md, stop and report the conflict. Don't pick one silently.

## 2. Ponytail — the code standard

Stop at the first rung that holds:

1. **Does this need to exist?** If the task doesn't require it, don't build it.
2. **Already in this codebase?** Reuse it.
3. **Standard library does it?** Use it (`argparse`, `sqlite3`, `http.server`, `json`, `gzip`, `csv`, `threading`).
4. **Native platform feature?** cgroup files, SQLite constraints, HTML/CSS over JS.
5. **Dependency already installed?** numpy, pandas, scikit-learn, statsmodels, torch, matplotlib. **Never add one.**
6. **One line?** One line.
7. **Only then:** the minimum code that works.

- No interface with one implementation, no factory, no config for a value that never changes, no "for later" code, no
  class where a function will do.
- Fewest files. Boring over clever.
- A deliberate shortcut with a known limit gets a `ponytail:` comment naming the limit and the upgrade path.
- Comments explain **why**, never what. Never delete an existing comment that justifies a decision.
- Two options of equal size: pick the one that is correct on edge cases.

**Never simplify away:** validation at trust boundaries, error handling that prevents damage or data loss, safety
checks on cgroup writes, anything the task explicitly requires.

## 3. Safety rules (non-negotiable)

- Never use `pickle`, `joblib`, `eval`, `exec`, `shell=True`, or `os.system`. `torch.load` always uses
  `weights_only=True`; `np.load` always uses `allow_pickle=False`.
- Network code appears only where TRD names it: `data.py` (download), `dashboard.py` (127.0.0.1 server),
  `experiment.py` (loopback). `subprocess` appears only in `experiment.py`.
- SQL values are always parameters. A column name is used only after it is checked against a fixed whitelist.
- cgroup writes happen only in `execute.py`, only after TRD §12 validation.
- The eBPF ingress program returns 1 on every path. The loader never opens a file for writing.
- Dashboard JavaScript sets text with `textContent` only.

## 4. Tests — you run them

- Non-trivial logic (a branch, a loop, a parser, a formula, a safety check) gets **one** test that fails if the logic
  breaks. Trivial one-liners need none.
- Every acceptance criterion in the task gets a test.
- Build fixture cgroup and `/proc` trees in `tmp_path`; never depend on this machine's real state, except in tests
  marked `@pytest.mark.live`, which you write but do not run.
- Tiny data and fixed seeds. The whole suite must stay under 3 minutes.
- **Run `/home/shreenipane/.local/bin/irm-test` after writing code** (always this absolute path). Fix failures and run
  it again until it is green. You may pass pytest arguments, e.g.
  `/home/shreenipane/.local/bin/irm-test tests/test_monitor.py -x -q`. For Phase 2 C code, also run
  `/home/shreenipane/.local/bin/irm-test bpf`.
- A task is not done while any test fails. If you cannot make it pass, stop and report the failing output.

## 5. Commands

- The **only** command you may run is `/home/shreenipane/.local/bin/irm-test` (with arguments). Nothing else: no
  bare `irm-test`, `python`, `uv`, `pip`, `git`, `ls`, `cat`, shell chaining, redirects, or environment-variable
  prefixes. Any other command is refused and ends your run.
- Your working directory is an empty scratch folder, not the repository. Always use absolute paths.
- `irm-test` runs inside a jail with no network and no home directory. That is expected: tests must not need either.
- The architect installs dependencies, downloads data, runs long training jobs and live tests, and runs `git`.

## 6. Final summary (print this at the end)

```
FILES CHANGED: <path — one line on what changed>
FILES READ: <paths>
TESTS: <names written or updated>
IRM-TEST: <the final summary line of your last irm-test run, e.g. "42 passed in 12.3s">
SKIPPED: <X — add when Y>   (or none)
CONFLICTS / QUESTIONS: <none | details>
```
