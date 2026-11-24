#!/usr/bin/env python3
# Refresh from a completed CI run: python scripts/refresh_shard_durations.py RUN_ID
"""Replace shard weights with setup + call + teardown seconds from all shard JUnit reports."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_durations(directory: Path) -> dict[str, float]:
    durations: dict[str, float] = defaultdict(float)
    for shard in range(1, 7):
        report = directory / f"agent-shard-junit-{shard}-of-6/agent-shard-junit.xml"
        cases = list(ET.parse(report).iter("testcase"))
        if not cases:
            raise ValueError(f"No test cases in {report.name} for shard {shard}")
        for case in cases:
            path = PurePosixPath(case.get("file", ""))
            if path.is_absolute() or ".." in path.parts or not str(path).startswith("tests/") or path.suffix != ".py":
                raise ValueError(f"Missing or invalid test file in shard {shard}; use junit_family=xunit1")
            seconds = float(case.attrib["time"])
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError(f"Invalid test duration for {path}")
            durations[path.as_posix()] += seconds
    return {path: round(seconds, 1) for path, seconds in sorted(durations.items())}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=int, help="completed Python Tests workflow run ID")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="looper-shard-durations-") as directory:
            subprocess.run(
                ["gh", "run", "download", str(args.run_id), "--repo", "foolzzz/looper",
                 "--pattern", "agent-shard-junit-*", "--dir", directory], check=True,
            )
            durations = _read_durations(Path(directory))
        (REPO_ROOT / "tests/shard_durations.json").write_text(
            json.dumps(durations, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Cannot refresh shard durations: {exc}\n")
    print(f"Recorded durations for {len(durations)} files from run {args.run_id}")


if __name__ == "__main__":
    main()
