# Task P5 — Recommender over-reach fix (fragment)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `irm/recommend.py`, `tests/test_recommend.py`, and the `recommend` branch of `irm/cli.py`.

## The bug (found in a live demo)
With 136 leaf cgroups, `irm recommend` emitted limits for **every** leaf (idle GNOME and app scopes got `cpu.max` of
0.1 core), and `avail = max(0.1 × n_others, ncpu − reserve)` used the floor term (13.6 cores) instead of the real
budget (9.0 cores). `irm apply --yes` then capped the whole desktop.

## New rules (replace TRD §11 steps 4–5 for non-protected cgroups)
1. **Targets** = protected cgroups (`--protect`), plus non-protected cgroups whose `peak ≥ min_cores`
   (new parameter, default 0.5), plus any cgroup named in `only` (new parameter, default none) regardless of its peak.
   When `only` is given, non-protected cgroups not in `only` are never targets.
2. Every other qualifying cgroup is **background**: it gets no item; it is listed in `skipped` with reason
   `"background: peak X.XX cores < 0.50"` (or `"background: not in --only"`), and its `peak` still counts as demand.
3. `reserve = Σ peak × headroom` over protected targets.
   `background = Σ peak` over background cgroups.
   `avail = max(0.0, ncpu − reserve − background)`. **No floor term in `avail`.**
4. Non-protected targets: `want = peak × headroom`; if `Σ want > avail`, scale each by `avail / Σ want`. Then apply the
   0.1-core floor **per item**; if the floor raised an item, its reason ends with `"; raised to the 0.1-core floor"`.
   The `"max"` cutoff (≥ 0.9 × ncpu) and `memory.high` rule are unchanged.
5. Reason for a squeezed item: `"q95 {peak:.2f} cores (empirical) × {headroom:.2f}; squeezed to fit {avail:.2f} free
   cores after {reserve:.2f} reserved and {background:.2f} background"`.
6. Pairs are computed over targets and background alike (unchanged).

## Files to touch
- `irm/recommend.py`: `recommend(..., min_cores=0.5, only=None)`.
- `irm/cli.py`: `recommend` gains `--min-cores` (float, 0.5) and `--only` (nargs `*`). Nothing else in `cli.py`.
- `tests/test_recommend.py`: add the tests below. You may change an existing assertion **only** where it encoded the old
  rules above (every leaf gets an item, or the floor in `avail`); say which in your summary.

## Tests to add
1. 50 idle leaves at 0.01 cores + `noisy` at 10 cores + protected `crit` at 5 cores, `ncpu = 16` (monkeypatch
   `os.cpu_count`), headroom 1.25 → items only for `noisy` and `crit`; the 50 are in `skipped` with a `background` reason;
   `noisy` `cpu_max == "925000 100000"` (avail = 16 − 6.25 − 0.5 = 9.25).
2. Budget smaller than the floors: 3 targets with `avail = 0` → each quota `"10000 100000"` and the floor note in its reason.
3. `only=[noisy]` with another busy leaf `busy2` at 3 cores → `busy2` is background (`not in --only`), not an item.

## Run
`/home/shreenipane/.local/bin/irm-test tests/test_recommend.py tests/test_execute.py -q`, then the full
`/home/shreenipane/.local/bin/irm-test -q`, until green. Print the `AGENTS.md` §6 summary.
