"""With scoped routing off, the Manager's process signature is what it was before chat scopes."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

import pytest

from looper.agents.ops.service import MANAGER_TOOLS
from looper.agents.tiers.manager import ManagerAgent
from tests.chat_scope_freeze import SCOPE_SIGNATURE_NODE
from tests.db_support import DSN, run_db
from tests.test_manager_tools_db import _converse


def test_the_scope_signature_part_is_only_added_for_a_scoped_session() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(ManagerAgent._prepare_turn)))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    (found,) = [node for node in ast.walk(tree) if isinstance(node, ast.AugAssign)
                and ast.dump(node, include_attributes=False) == SCOPE_SIGNATURE_NODE]
    guard = parents[found]
    assert isinstance(guard, ast.If) and found in guard.body and not guard.orelse
    assert ast.dump(guard.test) == ast.dump(ast.parse("scope is not None", mode="eval").body)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_an_unscoped_turn_signature_has_no_scope_part(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)

    async def test(pool: Any) -> None:
        await _converse(tmp_path, [[("looper_overview", {})]], ["hi"])
        (turn,) = prepared
        workflows = any(tool.name == "looper_task_create" and "workflow" in tool.schema.get("properties", {})
                        for tool in MANAGER_TOOLS)
        # The command, prompt, environment, library, binding and recording route, plus the listing hash only
        # where templates or workflows put it there before chat scopes.
        assert len(turn.signature) == 6 + workflows
        assert "scope" not in turn.signature_components

    run_db(test, monkeypatch, timeout=60)
