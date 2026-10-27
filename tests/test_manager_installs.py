"""Manager extras share the catalog loader, immutable copies and resident lifecycle."""

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from looper.agents import library, read_only_copies
from looper.agents.runtimes.pi import PiRuntime
from looper.db import manager_installs, managers, projects
from tests.runtime_model_helpers import builtin_launch


@pytest.fixture
def installed(_installed, monkeypatch):
    rows = {}
    monkeypatch.setattr(manager_installs, "list_installed", AsyncMock(side_effect=lambda owner: rows.get(owner, [])))
    return _installed, rows


def _extra(stored, name="manager-greeting"):
    text = (Path(__file__).parents[1] / "evals/prompt_regression/fixtures/manager-greeting/SKILL.md").read_text()
    return {**stored.item(name, "skill", name, "ui", None, {"SKILL.md": text}), "scope": "manager"}


def test_extra_isolated_inherits_globals_and_reuses_item_materialization(tmp_path, installed):
    stored, rows = installed
    inherited = stored.item("global", "skill", "global", "ui", None, {"SKILL.md": "Global skill."})
    stored.global_rows = [inherited]
    extra = _extra(stored)
    rows["manager-a"] = [extra]
    a = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager-a"))
    b = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager-b"))
    assert [(item.name, item.scope) for item in a.items] == [("global", "global"), ("manager-greeting", "manager")]
    assert [item.name for item in b.items] == ["global"]
    assert a.sha256 != b.sha256
    rows["manager-b"] = [extra]
    second = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager-b"))
    assert second.plugin_dirs == a.plugin_dirs
    assert len(list((tmp_path / ".local/library").glob("*/manager-greeting"))) == 1


def test_default_no_extra_and_redundant_extra_preserve_hash_metadata_order_and_argv(tmp_path, installed):
    stored, rows = installed
    skill = _extra(stored)
    plugin = stored.item("plugin", "plugin", "demo", "ui", None,
                         {".claude-plugin/plugin.json": '{"name":"demo"}'})
    stored.global_rows = [plugin, {**skill, "scope": "global"}]
    baseline = asyncio.run(library.load_installed(tmp_path, "manager"))
    empty = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager"))
    rows["manager"] = [skill, {**plugin, "scope": "manager"}]
    duplicate = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager"))
    # Disabled or deleted explicit rows are absent from this enabled-row query; globals remain.
    rows["manager"] = []
    disabled = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager"))
    assert baseline == empty == duplicate == disabled
    launch = builtin_launch(library=baseline)
    arguments = dict(cwd=tmp_path, prompt_file=tmp_path / "prompt")
    command = launch.runtime.resident_command("claude", launch=launch, **arguments)
    for loaded in (empty, duplicate, disabled):
        assert loaded.sha256 == baseline.sha256
        assert launch.runtime.resident_command("claude", launch=replace(launch, library=loaded), **arguments) == command


def test_manager_id_is_rejected_for_task_role(tmp_path):
    with pytest.raises(ValueError, match="manager_id"):
        asyncio.run(library.load_installed(tmp_path, "worker", manager_id="manager-a"))


def test_reclaim_retains_cold_and_disabled_manager_skill_bundles(tmp_path, installed, monkeypatch):
    stored, rows = installed
    rows["manager-cold"] = [_extra(stored, "cold")]
    rows["manager-disabled"] = [_extra(stored, "disabled")]
    monkeypatch.setattr(projects, "list_projects", AsyncMock(return_value=[]))
    monkeypatch.setattr(managers, "list_managers", AsyncMock(return_value=[
        {"id": "manager", "enabled": True}, {"id": "manager-cold", "enabled": True},
        {"id": "manager-disabled", "enabled": False}]))
    paths = asyncio.run(read_only_copies.current_copies(tmp_path))
    assert paths is not None and len(paths) == 2
    assert read_only_copies.reclaim(tmp_path, keep=paths, command_lines=[]) == []
    assert all(Path(path).is_dir() for path in paths)


def test_pi_reports_manager_installs_skipped(tmp_path, installed):
    stored, rows = installed
    rows["manager-a"] = [_extra(stored)]
    loaded = asyncio.run(library.load_installed(tmp_path, "manager", manager_id="manager-a"))
    skipped = PiRuntime().launch_record(builtin_launch(library=loaded))["runtime_catalog"]["skipped"]
    assert len(skipped) == 1 and "manager-greeting" in skipped[0] and "does not load" in skipped[0]


def test_changed_extra_waits_for_background_work_then_resumes_saved_session(installed, monkeypatch, agent_db):
    from tests.test_manager_reply import _converse, _flag, _resident

    stored, rows = installed
    extra = _extra(stored)
    body = _resident().replace("    cost += 0.25\n", (
        "    if turn == 1 and '--resume' not in sys.argv:\n"
        "        print(json.dumps({'type':'system','subtype':'task_started','task_id':'greeting-job'}), flush=True)\n"
        "    if turn == 2:\n"
        "        print(json.dumps({'type':'system','subtype':'task_notification','task_id':'greeting-job',"
        "'status':'completed'}), flush=True)\n"
        "    cost += 0.25\n"))

    async def install(spawned):
        rows["manager"] = [extra]
        assert spawned[0][1].returncode is None

    result = _converse(monkeypatch, body, ["hello", "while background runs", "after completion"], between=install)
    assert [reply.split("\n\n")[0] for reply in result["replies"]] == [
        "turn 1: hello", "turn 2: while background runs", "turn 1: after completion"]
    assert "/new" in result["replies"][1]
    first, second = [argv for argv, _ in result["spawned"]]
    assert _flag(first, "--resume") is None and _flag(second, "--resume") == "sess-1"
    records = [call.kwargs["metadata"] for call in agent_db.start_record.await_args_list]
    assert "library" not in records[0] and "library" not in records[1]
    assert records[2]["library"]["items"][0]["scope"] == "manager"
    assert result["saved_session"] == "sess-1"
    assert all(process.returncode is not None for _, process in result["spawned"])


def test_per_turn_runtime_refreshes_installs_and_keeps_session(tmp_path, installed, monkeypatch, agent_db):
    from fake_claude import fake_claude
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from tests.test_manager_idle import message
    from tests.test_omp_runtime import _FAKE_OMP, _launch

    stored, rows = installed
    launch = _launch(executable=fake_claude(tmp_path / "omp", _FAKE_OMP))
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    manager._session_loaded, manager._clean_start = True, True
    monkeypatch.setattr(manager, "_load_db_config", AsyncMock())
    monkeypatch.setattr(manager, "_load_agent_settings", AsyncMock(return_value=launch.settings))
    monkeypatch.setattr(manager, "_send_status", AsyncMock())

    async def run():
        try:
            first = await manager._run_turn("one", message(manager, "one"))
            assert manager._claude.pid is None
            rows["manager"] = [_extra(stored)]
            second = await manager._run_turn("two", message(manager, "two"))
            assert first.endswith("one | turn 1") and second.endswith("two | turn 2")
            assert manager._session_id == "sess-1"
        finally:
            await manager.stop()
    asyncio.run(run())
    records = [call.kwargs["metadata"] for call in agent_db.start_record.await_args_list]
    assert "library" not in records[0]
    assert records[1]["library"]["items"][0]["scope"] == "manager"
