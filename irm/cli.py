import argparse
import sys
from irm import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="irm")
    parser.add_argument("--version", action="version", version=f"irm {__version__}")
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 2
    parser.parse_args(argv)
    return 0
