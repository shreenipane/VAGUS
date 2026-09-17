import argparse
import json
import os
from pathlib import Path
import sys
from irm import HOME, __version__


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

    # irm recommend
    rec_parser = subparsers.add_parser("recommend")
    rec_parser.add_argument("--db", default=str(HOME / "data" / "irm.db"))
    rec_parser.add_argument("--protect", nargs="*", default=[])
    rec_parser.add_argument("--headroom", type=float, default=1.25)
    rec_parser.add_argument("--min-samples", type=int, default=360)
    rec_parser.add_argument("--hours", type=float, default=24.0)
    rec_parser.add_argument("--out", default=str(HOME / "data" / "recommendations.json"))
    rec_parser.add_argument("--root", default="/sys/fs/cgroup")
    rec_parser.add_argument("--min-cores", type=float, default=0.5)
    rec_parser.add_argument("--only", nargs="*", default=None)

    # irm apply
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--from", dest="from_file", default=str(HOME / "data" / "recommendations.json"))
    apply_parser.add_argument("--yes", action="store_true", default=False)
    apply_parser.add_argument("--allow", nargs="*", default=[])
    apply_parser.add_argument("--root", default="/sys/fs/cgroup")
    apply_parser.add_argument("--db", default=str(HOME / "data" / "irm.db"))
    apply_parser.add_argument("--max-age-minutes", type=float, default=15.0)

    # irm revert
    revert_parser = subparsers.add_parser("revert")
    revert_parser.add_argument("--batch", type=int, default=None)
    revert_parser.add_argument("--root", default="/sys/fs/cgroup")
    revert_parser.add_argument("--db", default=str(HOME / "data" / "irm.db"))
    revert_parser.add_argument("--force", action="store_true", default=False)

    # irm control
    control_parser = subparsers.add_parser("control")
    control_parser.add_argument("--db", default=str(HOME / "data" / "irm.db"))
    control_parser.add_argument("--root", default="/sys/fs/cgroup")
    control_parser.add_argument("--proc", default="/proc")
    control_parser.add_argument("--protect", nargs="+", required=True)
    control_parser.add_argument("--only", nargs="+", required=True)
    control_parser.add_argument("--interval", type=float, default=60.0)
    control_parser.add_argument("--settle", type=float, default=30.0)
    control_parser.add_argument("--psi-margin", type=float, default=5.0)
    control_parser.add_argument("--iterations", type=int, default=None)
    control_parser.add_argument("--min-samples", type=int, default=12)
    control_parser.add_argument("--hours", type=float, default=1.0)

    # irm dashboard
    dash_parser = subparsers.add_parser("dashboard")
    dash_parser.add_argument("--port", type=int, default=8765)
    dash_parser.add_argument("--db", default=str(HOME / "data" / "irm.db"))

    # irm train
    train_parser = subparsers.add_parser("train")
    train_subparsers = train_parser.add_subparsers(dest="train_subcommand")
    train_forecast = train_subparsers.add_parser("forecast")
    train_forecast.add_argument("--epochs", type=int, default=5)
    train_forecast.add_argument("--seed", type=int, default=0)

    # irm evaluate
    eval_parser = subparsers.add_parser("evaluate")
    eval_subparsers = eval_parser.add_subparsers(dest="evaluate_subcommand")
    eval_placement = eval_subparsers.add_parser("placement")
    eval_placement.add_argument("--episodes", type=int, default=20)
    eval_placement.add_argument("--seed", type=int, default=0)
    eval_study = eval_subparsers.add_parser("study")
    eval_study.add_argument("--seeds", type=int, default=5)
    eval_study.add_argument("--episodes", type=int, default=20)

    # irm experiment
    exp_parser = subparsers.add_parser("experiment")
    exp_subparsers = exp_parser.add_subparsers(dest="experiment_subcommand")
    exp_slo = exp_subparsers.add_parser("slo")
    exp_slo.add_argument("--minutes", type=float, default=3.0)
    exp_slo.add_argument("--reps", type=int, default=3)
    exp_slo.add_argument("--rate", type=float, default=200.0)
    exp_slo.add_argument("--arms", default="A,B,C,W,K")
    exp_slo.add_argument("--raw-dir", default=str(HOME / "reports" / "slo_raw"))

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
            from irm.monitor import run
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
                from irm.monitor import bench
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
        elif args.subcommand == "recommend":
            from irm.recommend import recommend
            res = recommend(
                db_path=args.db,
                root=args.root,
                protect=args.protect,
                headroom=args.headroom,
                min_samples=args.min_samples,
                hours=args.hours,
                min_cores=args.min_cores,
                only=args.only,
            )
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2)
            for item in res.get("items", []):
                print(f"{item['cgroup']}  {item['cpu_max']}  {item['memory_high']}  {item['reason']}")
            return 0
        elif args.subcommand == "apply":
            from irm.execute import apply
            with open(args.from_file, "r", encoding="utf-8") as f:
                recs = json.load(f)
            max_age = None if args.max_age_minutes <= 0 else args.max_age_minutes
            return apply(
                db_path=args.db,
                recs=recs,
                root=args.root,
                allow=args.allow,
                yes=args.yes,
                max_age_minutes=max_age,
            )
        elif args.subcommand == "revert":
            from irm.execute import revert
            return revert(
                db_path=args.db,
                root=args.root,
                batch=args.batch,
                force=args.force,
            )
        elif args.subcommand == "control":
            from irm.control import run as control_run
            control_run(
                args.db,
                args.root,
                args.proc,
                args.protect,
                args.only,
                interval=args.interval,
                settle=args.settle,
                psi_margin=args.psi_margin,
                max_iterations=args.iterations,
                min_samples=args.min_samples,
                hours=args.hours,
            )
            return 0
        elif args.subcommand == "dashboard":
            from irm.dashboard import serve
            serve(args.port, args.db, HOME / "reports", HOME / "data" / "recommendations.json")
            return 0
        elif args.subcommand == "train":
            if getattr(args, "train_subcommand", None) == "forecast":
                from irm.forecast import demo
                out_path = HOME / "reports" / "forecast.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                demo(out_path, args.seed, args.epochs)
                return 0
            else:
                train_parser.print_help()
                return 2
        elif args.subcommand == "experiment":
            if getattr(args, "experiment_subcommand", None) == "slo":
                from irm.experiment import run_slo
                out_path = HOME / "reports" / "slo.json"
                arms = [a.strip() for a in args.arms.split(",") if a.strip()]
                raw_dir = Path(args.raw_dir)
                run_slo(
                    out_path,
                    minutes=args.minutes,
                    reps=args.reps,
                    rate=args.rate,
                    arms=arms,
                    raw_dir=raw_dir,
                )
                return 0
            else:
                exp_parser.print_help()
                return 2

        elif args.subcommand == "evaluate":
            if getattr(args, "evaluate_subcommand", None) == "placement":
                from irm.dqn import demo
                out_path = HOME / "reports" / "placement.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                demo(out_path, args.seed, args.episodes)
                return 0
            elif getattr(args, "evaluate_subcommand", None) == "study":
                from irm.dqn import study
                out_path = HOME / "reports" / "placement_study.json"
                study(out_path, seeds=list(range(args.seeds)), episodes=args.episodes)
                return 0
            else:
                eval_parser.print_help()
                return 2
        else:
            parser.print_help()
            return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0
