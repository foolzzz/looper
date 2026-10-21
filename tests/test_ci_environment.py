"""What the suite needs from the machine it runs on: the full history, and no host-specific temp root in a pin."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from model_launch_parity import above_root_normalized

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", ".claude/rules/**", "AGENTS.md", ".claude/AGENTS.md")


def test_python_tests_job_fetches_the_full_history() -> None:
    # The legacy-checkout parity tests check out pinned old revisions, which a shallow clone lacks.
    text = (ROOT / ".github/workflows/python-tests.yml").read_text()
    job = text[text.index("  agent-system:"):text.index("  dashboard:")]
    checkout = re.search(r"uses: actions/checkout@\S+\n(?:\s+with:\n(?:\s+\S.*\n)+)?", job)
    assert checkout and re.search(r"fetch-depth:\s*0\b", checkout.group(0)), job


@pytest.mark.parametrize("ancestors", [("/private/tmp", "/private", ""), ("/tmp", "")])
def test_settings_comparison_ignores_the_hosts_temp_root_ancestors(ancestors: tuple[str, ...]) -> None:
    root = "/tmp/looper-x"
    excludes = [f"{root}/{n}" for n in NAMES]
    for above in ancestors:
        excludes += [f"{above}/{n}" for n in NAMES]
    excludes.append(f"{root}/home/.claude/CLAUDE.md")
    text = json.dumps({"claudeMdExcludes": excludes}, separators=(",", ":"))
    pinned = json.dumps({"claudeMdExcludes": [f"<root>/{n}" for n in NAMES] + [f"<above>/{n}" for n in NAMES]
                         + ["<root>/home/.claude/CLAUDE.md"]}, separators=(",", ":"))
    assert above_root_normalized(text, root) == above_root_normalized(pinned, root) == pinned


def test_launch_parity_ignores_the_temp_root_ancestors_in_a_pinned_claude_launch(tmp_path: Path) -> None:
    import copy
    from test_model_launch_parity_db import pinned
    import model_launch_parity as parity

    expected = pinned("claude")["oauth-default"]
    actual = copy.deepcopy(expected)
    swap = lambda text: text.replace("/private/tmp/", "/tmp/").replace("/private/", "/")  # noqa: E731
    row = actual[0]
    assert "/private/tmp/" in json.dumps(row)
    row["config"]["settings"] = swap(row["config"]["settings"])
    row["argv"] = [swap(a) if a.startswith("{") else a for a in row["argv"]]
    row["isolation"] = json.loads(swap(json.dumps(row["isolation"])))
    parity.assert_parity(expected, actual, expected_root=tmp_path, actual_root=tmp_path)
