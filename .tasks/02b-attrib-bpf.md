# Task 2b — Softirq attribution: BPF program and loader

**Repository root:** `/home/shreenipane/Heavy Coding/Projects/intelligent-resource-manager`. Absolute paths for your
file tools. Read `AGENTS.md` (§3 safety rules), `TRD.md` §5.1–§5.3, and `irm/attrib.py` (the consumer of your output).

Touch **only** `bpf/Makefile`, `bpf/attrib.bpf.c`, `bpf/attrib.c`. Do not touch Python files.

## Goal
Measure softirq time per CPU and vector, and count packets delivered to each cgroup's sockets during NET_RX, printing
cumulative counters as JSON lines that `irm/attrib.py` already parses. You compile it in the jail (no root, cannot
load it); the architect and the user load it with `sudo`.

## Requirements (TRD §5.1–§5.3 exactly)
- `bpf/Makefile`: targets `all` (default: `attrib`), `vmlinux.h`, `attrib.bpf.o`, `attrib.skel.h`, `attrib`, `clean`,
  with the exact commands of TRD §5.1. Use `bpftool` and `clang` from `/usr/sbin` or `/usr/bin` without hard-coding
  (plain names; the jail's `PATH` is `/usr/bin`, so if `bpftool` lives in `/usr/sbin`, set `BPFTOOL ?= $(shell command -v
  bpftool || echo /usr/sbin/bpftool)`).
- `bpf/attrib.bpf.c`: `#include "vmlinux.h"`, `<bpf/bpf_helpers.h>`, `<bpf/bpf_tracing.h>`; license `GPL`; the five maps
  of TRD §5.2 with those types, key/value layouts, and sizes; programs `tp_btf/softirq_entry`, `tp_btf/softirq_exit`,
  `cgroup_skb/ingress` exactly as described. **The `cgroup_skb/ingress` program returns 1 on every path.** No loops.
- `bpf/attrib.c`: `attrib [--interval N]` (1–60, default 5; anything else → usage on stderr, exit 2). Open/load the
  skeleton; attach the two tracepoints; attach ingress with `bpf_program__attach_cgroup` on an
  `open("/sys/fs/cgroup", O_RDONLY | O_DIRECTORY)` fd (keep the `bpf_link`). Every interval print exactly one JSON line in
  the TRD §5.3 format to stdout and `fflush(stdout)`: `vec_ns` as 10 arrays of `ncpu` values
  (`libbpf_num_possible_cpus()`); `rx_pkts` object keyed by decimal cgid with `ncpu` values each; `blamed_ns` object
  keyed by decimal cgid with 10 values each, summed over CPUs and over map entries for that cgid. Iterate hash maps with
  `bpf_map_get_next_key` + `bpf_map_lookup_elem` using per-CPU value buffers. `ts` from `clock_gettime(CLOCK_REALTIME)`
  with 3 decimals. SIGINT/SIGTERM set a flag; exit cleanly with `attrib_bpf__destroy` (links released), exit 0. Errors →
  stderr, exit 1. **Never open a file for writing.**
- `-Wall -Wextra` clean.

## Verify
Run `/home/shreenipane/.local/bin/irm-test bpf` until it builds with no warnings. Do not try to run `bpf/attrib` (it needs
root; the jail refuses it). Print the `AGENTS.md` §6 summary, including the final build output lines.
