# Task Q7 — Test hygiene: no writes into the repository, no wall-clock assertions (fragment)

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your file
tools. Read `AGENTS.md` (§4, §5: the only command you may run is `/home/shreenipane/.local/bin/irm-test …`), then
`tests/test_experiment.py` and `tests/test_dqn.py`.

Touch **only** `tests/test_experiment.py` and `tests/test_dqn.py`.

## Problems
1. A test in `tests/test_experiment.py` calls `run_slo` (with faked collaborators) without `raw_dir`, so it wrote
   `reports/slo_raw/rep0_B.json.gz` and `rep0_C.json.gz` into the real repository when run outside the jail. Tests must
   never write outside `tmp_path`.
2. `tests/test_dqn.py::test_demo_full_20_episodes` asserts `res["train_seconds"] < 180.0`. Wall-clock time depends on
   machine load (it measured 863 s while other jobs ran), so the test fails for reasons unrelated to correctness.

## Do
1. Pass `raw_dir=tmp_path / "raw"` (and any other output path) to every `run_slo` call in the tests, and add one
   assertion that the raw files land under `tmp_path`.
2. Replace the wall-clock assertion with `res["train_seconds"] > 0`. Keep every other assertion.

Run `/home/shreenipane/.local/bin/irm-test tests/test_experiment.py -q` until green (do not run `tests/test_dqn.py`; it is
slow and other jobs are using the CPU). Print the `AGENTS.md` §6 summary.
