"""Usage is read-only: what a run loads, records and is launched with is what it was before it."""

from __future__ import annotations

import ast
from pathlib import Path

from looper.agents import library
from looper.agents.claude_cli import library_args

AGENTS = Path(library.__file__).parent


def test_a_loaded_skill_is_recorded_and_launched_as_before() -> None:
    item = library.LoadedItem(library.ItemKind.SKILL, "deploy-check", "a" * 64, library.PROJECT_SKILLS_PLUGIN, "project")
    assert item.view() == {"kind": "skill", "name": "deploy-check", "sha256": "a" * 64, "plugin": "project",
                           "scope": "project"}
    loaded = library.AgentLibrary((item,), ("/runtime/.local/library/k/project",), role="worker", project_id="p")
    assert loaded.record() == {"sha256": library.library_sha256([item]), "role": "worker", "project_id": "p",
                               "items": [item.view()]}
    assert library_args(loaded) == ["--plugin-dir", "/runtime/.local/library/k/project"]


def test_no_run_side_module_imports_the_usage_query() -> None:
    importers = []
    for path in AGENTS.rglob("*.py"):
        if path.name in {"skill_usage.py"} or "v1_domains" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            module = getattr(node, "module", None) or ""
            if "skill_usage" in names or "skill_usage" in module:
                importers.append(path.name)
    assert importers == []
