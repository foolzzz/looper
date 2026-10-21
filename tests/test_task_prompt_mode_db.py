"""A task's own system prompt mode per role: stored, read, written, reset, and applied to a real launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_db import DSN
from tests.test_profile_registry_db import _call, _launch, _model_id, _ok, _run, _runtime

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _task(tmp_path: Path, name: str = "calc") -> str:
    project = (await _ok("POST", "/api/projects", {"name": name}))["project_id"]
    return (await _ok("POST", "/api/tasks/create", {
        "requirement": "do it", "project_id": project, "work_dir": str(tmp_path / "work")}))["task_id"]


def _modes(view: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {role: (v["system_prompt_mode"]["value"], v["system_prompt_mode"]["source"])
            for role, v in view["roles"].items()}


async def _agents(task_id: str) -> dict[str, Any]:
    return (await _ok("GET", f"/api/tasks/{task_id}"))["agents"]


def test_task_modes_are_read_written_reset_and_validated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def run(pool: Any) -> None:
        task = await _task(tmp_path)
        builtin = {"planner": ("replace", "builtin"), "worker": ("append", "builtin"), "reviewer": ("append", "builtin")}
        view = await _agents(task)
        assert _modes(view) == builtin
        assert all(v["runtime_type"] == "claude-code" for v in view["roles"].values())
        # The role layer applies to every task; the task's own choice wins over it, per role.
        await _ok("PUT", "/api/agent-settings/roles/worker", {"system_prompt_mode": "replace"})
        body = await _ok("PUT", f"/api/tasks/{task}/agents", {"system_prompt_modes": {"planner": "append",
                                                                                      "worker": "append"}})
        assert _modes(body["agents"]) == {"planner": ("append", "task"), "worker": ("append", "task"),
                                          "reviewer": ("append", "builtin")}
        other = await _task(tmp_path / "other", "other")
        assert _modes(await _agents(other)) == {**builtin, "worker": ("replace", "role")}
        body = await _ok("PUT", f"/api/tasks/{task}/agents", {"system_prompt_modes": {"worker": None}})
        assert _modes(body["agents"])["worker"] == ("replace", "role")
        assert await pool.fetchval("SELECT count(*) FROM task_system_prompt_modes WHERE task_id = $1", task) == 1
        # Bad input fails fast and changes nothing.
        for bad, text in (({"manager": "append"}, "not 'manager'"), ({"planner": "auto"}, "must be"),
                          ({"planner": 1}, "must be"), ("append", "must be an object")):
            status, error = await _call("PUT", f"/api/tasks/{task}/agents", {"system_prompt_modes": bad})
            assert status == 400 and text in error["error"], bad
        assert await pool.fetchval("SELECT count(*) FROM task_system_prompt_modes WHERE task_id = $1", task) == 1
        # Its rows go with the task.
        await pool.execute("DELETE FROM tasks WHERE id = $1", task)
        assert await pool.fetchval("SELECT count(*) FROM task_system_prompt_modes WHERE task_id = $1", task) == 0

    _run(run, monkeypatch)


def test_task_mode_shows_the_runtime_type_so_the_ui_hides_it_off_claude_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def run(_pool: Any) -> None:
        await _runtime("codex")
        profile = (await _ok("POST", "/api/agent-profiles", {
            "name": "cdx", "runtime_id": "rt-codex",
            "model_id": await _model_id("prov-codex-login", "gpt-5", "openai-responses")}))["profile"]
        task = await _task(tmp_path)
        body = await _ok("PUT", f"/api/tasks/{task}/agents", {"profiles": {"worker": profile["profile_id"]},
                                                              "system_prompt_modes": {"worker": "replace"}})
        roles = body["agents"]["roles"]
        assert roles["worker"]["runtime_type"] == "codex" and roles["planner"]["runtime_type"] == "claude-code"

    _run(run, monkeypatch)


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
@pytest.mark.parametrize("choice", [None, "append", "replace"])
def test_a_real_claude_code_launch_follows_the_task_then_the_role_then_the_builtin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str, choice: str | None,
) -> None:
    async def run(_pool: Any) -> None:
        task = await _task(tmp_path)
        await _ok("PUT", f"/api/agent-settings/roles/{role}", {"system_prompt_mode": "replace" if role == "worker" else "append"})
        if choice:
            await _ok("PUT", f"/api/tasks/{task}/agents", {"system_prompt_modes": {role: choice}})
        used = choice or ("replace" if role == "worker" else "append")
        seen = await _launch(role, task, tmp_path / "work", tmp_path)
        flag = "--system-prompt-file" if used == "replace" else "--append-system-prompt-file"
        other = "--append-system-prompt-file" if used == "replace" else "--system-prompt-file"
        assert flag in seen["argv"] and other not in seen["argv"]

    _run(run, monkeypatch)


@pytest.mark.parametrize("falsy", [False, 0, "", [], None])
def test_a_falsy_system_prompt_modes_is_refused_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, falsy: Any,
) -> None:
    async def run(pool: Any) -> None:
        task = await _task(tmp_path)
        status, body = await _call("PUT", f"/api/tasks/{task}/agents",
                                   {"concurrency": {"worker": 3}, "system_prompt_modes": falsy})
        assert status == 400 and "system_prompt_modes must be an object" in body["error"]
        assert await pool.fetchval("SELECT count(*) FROM task_concurrency_limits WHERE task_id = $1", task) == 0

    _run(run, monkeypatch)
