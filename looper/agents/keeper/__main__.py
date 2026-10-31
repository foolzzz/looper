"""Isolated entry point; load every keeper module before starting a run."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .process import Keeper


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--cwd")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(argv)
        if args.check:
            print(json.dumps(sorted(m for m in sys.modules if m == "looper" or m.startswith("looper."))))
            return 0
        if args.dir is None or args.runtime_root is None or args.cwd is None:
            parser.error("--dir, --runtime-root and --cwd are required")
        if len(args.argv) < 2 or args.argv[0] != "--":
            parser.error("-- must be followed by a child command")
    except SystemExit as exc:
        return int(exc.code)
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(Keeper(args.dir, args.runtime_root, args.cwd, args.argv[1:]).run())


if __name__ == "__main__":
    sys.exit(main())
