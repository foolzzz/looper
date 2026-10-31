"""The public RFC and its reachable history must not expose operator telemetry."""

import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RFC = ROOT / "docs/architecture/rfcs/command-schedules-v0.zh-CN.md"


PRIVATE_CONTEXT_PATTERNS = [
    ("requester background", r"需求方|(?:一个|某个)[^。\n]*项目有[^。\n]*周期性工作"),
    ("measured workload duration", r"(?:实测|中位|离群)[^。\n]*(?:分钟|小时)"),
    ("operator spending", r"(?:每天|每日)[^。\n]*美元"),
    ("operator restart counts", r"(?:每天|每日)重启[^。\n]*\d"),
    ("operator database size", r"`llm_records`[^。\n]*\d+\s*(?:[KMGT]i?B)"),
    ("operator deployment cadence", r"发布间隔[^。\n]*(?:中位|实测|统计)"),
]


@pytest.mark.parametrize(("category", "pattern"), PRIVATE_CONTEXT_PATTERNS)
def test_command_schedules_rfc_excludes_private_operator_context(category: str, pattern: str) -> None:
    if re.search(pattern, RFC.read_text(encoding="utf-8")):
        # Do not reproduce the private text in test output or fixtures.
        pytest.fail(f"The public RFC includes {category}; use generic product requirements", pytrace=False)


@pytest.mark.parametrize(("category", "pattern"), PRIVATE_CONTEXT_PATTERNS)
def test_command_schedules_rfc_history_excludes_private_operator_context(category: str, pattern: str) -> None:
    path = RFC.relative_to(ROOT).as_posix()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, check=True, capture_output=True,
            text=True, encoding="utf-8", timeout=10,
        ).stdout

    # Include every parent of merges; checking only HEAD misses earlier exposure.
    commits = git("log", "--full-history", "--format=%H", "HEAD", "--", path).splitlines()
    scanned: set[str] = set()
    for commit in commits:
        blob = git("ls-tree", "--format=%(objectname)", commit, "--", path).strip()
        if not blob or blob in scanned:
            continue  # The file was deleted, or this snapshot was already checked.
        scanned.add(blob)
        if re.search(pattern, git("cat-file", "blob", blob)):
            pytest.fail(
                f"RFC history includes {category} at {commit}; "
                "replace the exposed history, not only the current file",
                pytrace=False,
            )
