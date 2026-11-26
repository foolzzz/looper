#!/usr/bin/env python3
# Refresh from a completed CI run: python scripts/refresh_shard_durations.py RUN_ID
"""Replace shard weights with setup + call + teardown seconds from all shard JUnit reports.

Files heavier than the split threshold also record per-test seconds, so chunk
boundaries can follow time. The threshold matches tests/conftest.py.
"""

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
# Same cut as tests.conftest.HEAVY_FILE_SPLIT_SECONDS. A file at or under it stays whole.
HEAVY_FILE_SPLIT_SECONDS = 300.0


def _nodeid(file: str, classname: str | None, name: str | None) -> str:
    """Invert pytest's xunit1 classname/name split back into a node id."""
    if not classname or not name:
        raise ValueError(f"Missing classname or name for {file}")
    module = PurePosixPath(file).with_suffix("").as_posix().replace("/", ".")
    if classname == module:
        return f"{file}::{name}"
    prefix = module + "."
    if not classname.startswith(prefix):
        raise ValueError(f"classname {classname} does not match {file}")
    classes = classname[len(prefix):].replace(".", "::")
    return f"{file}::{classes}::{name}"


def _read_durations(directory: Path) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    file_sums: dict[str, float] = defaultdict(float)
    # classname/name are kept raw. Only a file over the split threshold needs node ids,
    # and a collected test can report a different module than the file it is defined in.
    cases_by_file: dict[str, list[tuple[str | None, str | None, float]]] = defaultdict(list)
    for shard in range(1, 9):
        report = directory / f"agent-shard-junit-{shard}-of-8/agent-shard-junit.xml"
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
            file_name = path.as_posix()
            file_sums[file_name] += seconds
            cases_by_file[file_name].append((case.get("classname"), case.get("name"), seconds))
    files = {path: round(seconds, 1) for path, seconds in sorted(file_sums.items())}
    tests: dict[str, dict[str, float]] = {}
    for path, seconds in files.items():
        if seconds <= HEAVY_FILE_SPLIT_SECONDS:
            continue
        totals: dict[str, float] = defaultdict(float)
        for classname, name, value in cases_by_file[path]:
            totals[_nodeid(path, classname, name)] += value
        tests[path] = {nodeid: round(value, 1) for nodeid, value in sorted(totals.items())}
    return files, tests


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
            files, tests = _read_durations(Path(directory))
        (REPO_ROOT / "tests/shard_durations.json").write_text(
            json.dumps(files, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        (REPO_ROOT / "tests/shard_test_durations.json").write_text(
            json.dumps(tests, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Cannot refresh shard durations: {exc}\n")
    print(
        f"Recorded durations for {len(files)} files and per-test seconds for {len(tests)} "
        f"files from run {args.run_id}"
    )


if __name__ == "__main__":
    main()
