# Task 3 — Datasets

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Paths below are relative
to it; pass absolute paths to your file tools.

Read and follow `AGENTS.md`, then `TRD.md` §2, §3, §6, §16. Read `irm/cli.py` before changing it.

## Goal
Download a deterministic ~2% sample of the Azure 2019 VM trace without keeping the raw 856 MB files, turn it into
dense arrays for the models, and provide synthetic data with the same shape for tests. Your jail has no network: tests
use a fake opener. The architect runs the real download.

## Files to touch
- `irm/data.py` (create)
- `irm/cli.py` — subcommands `data fetch` and `data prepare`, options exactly as TRD §2; paths from `irm.HOME`
- `tests/test_data.py` (create)

## Interfaces (`irm/data.py`)
- `BASE_URL`, `VMTABLE = "trace_data_vmtable_vmtable.csv.gz"`, `readings_name(i)` →
  `f"trace_data_vm_cpu_readings_vm_cpu_readings-file-{i}-of-195.csv.gz"`.
- `keep(vmid: str, per_mille: int) -> bool`: `zlib.crc32(vmid.encode()) % 1000 < per_mille`.
- `fetch_vmtable(raw_dir, opener=urllib.request.urlopen) -> Path`: TRD §6.2. If the final file exists, return it
  without calling `opener`. Stream to `vmtable.csv.gz.part` in 1 MiB chunks while hashing; rename only if the byte count
  equals the response's `Content-Length`, otherwise raise `IOError` (the `.part` stays). Append `"<sha256>  <name>\n"`
  to `SHA256SUMS`. `opener(url, timeout=60)`.
- `fetch_readings(raw_dir, i, per_mille, opener=...) -> dict | None`: `None` (and no `opener` call) if
  `readings/{i:03d}.csv.gz` exists. Otherwise stream through `gzip.GzipFile(fileobj=response)` and `csv.reader`, write
  kept rows unchanged with `csv.writer` into `readings/{i:03d}.csv.gz.part` (gzip), rename when complete. Return
  `{"file": i, "rows", "kept", "ts_min", "ts_max"}` (over all rows).
- `fetch(raw_dir, files, last_file, per_mille, opener=...)`: vmtable first, then files `last_file − files + 1 …
  last_file` ascending; print `file i: rows=… kept=… ts_min=… ts_max=…` for each fetched file.
- `parse_bucket(s: str) -> float`: integer string → that value; `">N"` → `2 × N` with a `ponytail:` comment; anything
  else raises `ValueError` whose message contains `s`.
- `parse_category(s: str) -> int`: `Unknown` 0, `Delay-insensitive` 1, `Interactive` 2; anything else raises
  `ValueError` containing `s`.
- `prepare(raw_dir, out_path, max_vms, seed) -> dict`: TRD §6.3. Readings files have no header: timestamp, vmid,
  mincpu, maxcpu, avgcpu. Read vmtable in chunks (`pandas.read_csv(..., header=None, chunksize=1_000_000)`), keeping only
  VMs present in the readings. Drop VMs with fewer than 12 readings or missing from vmtable. If more than `max_vms`
  remain, choose with `numpy.random.default_rng(seed)`. Forward-fill: inside each VM's first-to-last reading span, a run of
  1 or 2 missing steps takes the last observed value; a run of 3 or more stays NaN. If `t_split <= t_start`, raise
  `ValueError("need more than 24 h of readings")`. Save with `np.savez_compressed`. Return `{"vms", "steps", "t_start",
  "t_split"}`.
- `synthetic_series(V, T, seed) -> dict`: TRD §6.4, the same keys and dtypes as `prepare`'s file, `t_start = 0`,
  `step = 300`, `t_split` by the TRD formula, `vmid = "vm<i>"`, `sub = "sub<i % 7>"`.
- `load_series(path) -> dict`: `np.load(path, allow_pickle=False)` into a plain dict.

## Tests to write (`tests/test_data.py`)
Fake opener: a function returning an `io.BytesIO` subclass with a `headers` dict (it already works as a context
manager). No network.
1. `keep` is deterministic and keeps 1.5–2.5% of 10,000 generated ids at `per_mille = 20`.
2. `fetch_vmtable`: matching length → final file, correct SHA-256 line; wrong length → `IOError`, no final file; final
   file present → opener never called.
3. `fetch_readings`: kept rows are exactly those `keep` selects; stats are right; a second call returns `None` without
   calling the opener; a leftover `.part` from an earlier failure does not count as complete.
4. `parse_bucket("2") == 2`, `parse_bucket(">24") == 48`, unknown strings raise with the string in the message; same
   for `parse_category`.
5. `prepare` on a fixture written by the test (raw gz files in `tmp_path`, 2 days + 1 hour at 5 min, 4 VMs): output
   shapes; a 2-step gap filled; a 3-step gap left NaN; a VM with < 12 readings dropped; a VM absent from vmtable
   dropped; `t_split` matches the formula; `max_vms = 2` gives the same choice twice for one seed; the file loads with
   `allow_pickle=False`. A fixture with < 24 h raises `ValueError`.
6. `synthetic_series`: same keys as `prepare`'s output, shapes `[V, T]`, values within 0–100, identical for the same seed.

## Run
`/home/shreenipane/.local/bin/irm-test -q` until green.

## Do not touch
Everything not listed above.

## The architect does these after your run (do not do them)
`irm data fetch` (network, ~17 GB streamed) and `irm-test run data prepare`.

## Final summary
Print the block from `AGENTS.md` §6.
