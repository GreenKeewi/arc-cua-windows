"""Run arc's driver benchmarks, and compare runs between versions.

    python benchmarks/run.py primitives [--quick] [--only SCENARIO ...]
    python benchmarks/run.py workflows [--reps N] [--only NAME ...] [--no-mcp]
    python benchmarks/run.py all [--quick]
    python benchmarks/run.py compare OLD.json NEW.json [--slower 1.2]

Results go to output/benchmarks/ as JSON (with the arc version, commit, macOS and
machine) and Markdown. See benchmarks/README.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "primitives":
        import primitives

        primitives.main(rest)
    elif command == "workflows":
        import workflows

        workflows.main(rest)
    elif command == "all":
        import primitives
        import workflows

        quick = ["--quick"] if "--quick" in rest else []
        primitives.main(quick)
        workflows.main(["--reps", "3"] if quick else [])
    elif command == "compare":
        import argparse

        import common

        parser = argparse.ArgumentParser(prog="benchmarks/run.py compare")
        parser.add_argument("old")
        parser.add_argument("new")
        parser.add_argument("--slower", type=float, default=1.2,
                            help="flag a measurement this many times its old median (default 1.2)")
        args = parser.parse_args(rest)
        print(common.compare(Path(args.old), Path(args.new), slower=args.slower))
    else:
        print(f"unknown command {command!r}\n{__doc__}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
