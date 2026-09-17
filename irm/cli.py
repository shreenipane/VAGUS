import argparse
import json
import os
from pathlib import Path
import sys
from irm import HOME, __version__
from irm.monitor import bench, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="irm")
    parser.add_argument("--version", action="version", version=f"irm {__version__}")

    subparsers = parser.add_subparsers(dest="subcommand")

    # irm monitor
    monitor_parser = subparsers.add_parser("monitor")
    monitor_parser.add_argument("--interval", type=float, default=5.0)
    monitor_parser.add_argument(
        "--db",
        nargs="?",
        const=str(HOME / "data" / "irm.db"),
        default=str(HOME / "data" / "irm.db"),
    )
    monitor_parser.add_argument("--root", default="/sys/fs/cgroup")
    monitor_parser.add_argument("--proc", default="/proc")
    monitor_parser.add_argument("--attrib", choices=["-"], nargs="?", const="-", default=None)
    monitor_parser.add_argument("--retention-hours", type=float, default=48.0)
    monitor_parser.add_argument("--duration", type=float, default=None)

    # irm bench overhead
    bench_parser = subparsers.add_parser("bench")
    bench_subparsers = bench_parser.add_subparsers(dest="bench_subcommand")
    overhead_parser = bench_subparsers.add_parser("overhead")
    overhead_parser.add_argument("--seconds", type=float, default=60.0)
    overhead_parser.add_argument("--interval", type=float, default=5.0)
    overhead_parser.add_argument("--root", default="/sys/fs/cgroup")
    overhead_parser.add_argument("--proc", default="/proc")

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 2

    args = parser.parse_args(argv)

    try:
        tck = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        tck = 100

    try:
        if args.subcommand == "monitor":
            attrib_stream = sys.stdin if args.attrib == "-" else None
            run(
                db_path=args.db,
                root=args.root,
                proc=args.proc,
                interval=args.interval,
                retention_hours=args.retention_hours,
                duration=args.duration,
                tck=tck,
                attrib_stream=attrib_stream,
            )
            return 0
        elif args.subcommand == "bench":
            if getattr(args, "bench_subcommand", None) == "overhead":
                res = bench(
                    seconds=args.seconds,
                    interval=args.interval,
                    root=args.root,
                    proc=args.proc,
                    tck=tck,
                )
                out_path = HOME / "reports" / "overhead.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(res, f, indent=2)
                print(json.dumps(res, indent=2))
                return 0
            else:
                bench_parser.print_help()
                return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0
