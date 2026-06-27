#!/usr/bin/env python3
"""Command-line entry point for IoT resource lifetime analysis."""

import argparse
import sys

if not __package__:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))


def parse_subcommand_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    lifetime = subparsers.add_parser("lifetime", help="Run IoT lifetime analysis")
    lifetime.add_argument("target", nargs="?", default=".")
    lifetime.add_argument("--max-files", type=int, default=0)
    lifetime.add_argument("--api-specs", action="append")
    lifetime.add_argument("--include-tests", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] != "lifetime":
        import iot.analyzer as iot_analyzer

        return iot_analyzer.main(argv or ["."])

    args = parse_subcommand_args(argv)
    if args.command == "lifetime":
        import iot.analyzer as iot_analyzer

        analyzer_args = [args.target]
        if args.max_files:
            analyzer_args.extend(["--max-files", str(args.max_files)])
        for spec in args.api_specs or []:
            analyzer_args.extend(["--api-specs", spec])
        if args.include_tests:
            analyzer_args.append("--include-tests")
        return iot_analyzer.main(analyzer_args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
