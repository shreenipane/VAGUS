# Task P6 — Recommender skips cgroups that no longer exist (fragment)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md`, `irm/recommend.py`, `tests/test_recommend.py`.

Other agents are editing `irm/cli.py`, `irm/experiment.py`, `irm/sim.py`, `irm/dqn.py`, `irm/dashboard.py`,
`irm/static/*` right now. Touch **only** `irm/recommend.py` and `tests/test_recommend.py`.

## Bug
A cgroup that stopped within `--hours` still has samples, so it gets an item; `apply` then rejects it and exits 1.

## Change
In `recommend()`, before targets and background are decided: a qualifying cgroup whose directory `root + cgroup` does not
exist is removed from consideration entirely (no item, no background demand, no pairs) and appended to `skipped` with
reason `"gone: cgroup no longer exists"`.

## Test to add
A fixture DB with samples for `/a.scope` and `/b.scope`, and a fixture root where only `a.scope` exists → items only for
`a`, and `b` in `skipped` with the `gone` reason. Existing tests must stay green; if an existing test's fixture root lacks
directories for its cgroups, create them in that test's setup rather than changing its assertions, and say so.

Run `/home/shreenipane/.local/bin/irm-test tests/test_recommend.py -q` until green. Print the `AGENTS.md` §6 summary.
