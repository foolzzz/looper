"""Knowledge-vault settings, token permissions and runs on a disposable PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper import child_env
from tests.test_room_db import BACKEND, _room, _room_task
from tests.test_usage_api_db import DSN, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _vault_path(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "codex-mem").mkdir(parents=True)
    (root / "mine").mkdir()
    return root


@pytest.mark.parametrize(("enabled", "configured", "writable", "expected"), [
    ("off", True, True, set()),
    ("on", False, True, set()),
    ("on", True, True, {"looper_vault_search", "looper_vault_read", "looper_vault_write"}),
    ("on", True, False, {"looper_vault_search", "looper_vault_read"}),
])
def test_planner_lists_only_the_vault_tools_its_project_allows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: str, configured: bool, writable: bool,
    expected: set[str],
) -> None:
    from looper.agents import vault
    from looper.agents.ops.planner import PlannerOps

    async def test(pool: Any) -> None:
        await _room_task(pool, room="off", vault=enabled)
        if configured:
            await vault.update_settings({"path": str(_vault_path(tmp_path)),
                                         "writable": ["codex-mem"] if writable else []})
        ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
        header = f"Bearer {ops.issue('task-r')}"
        assert {t["name"] for t in await ops.listing(header) if t["name"].startswith("looper_vault_")} == expected

    _run(test, monkeypatch)


def test_planner_vault_write_needs_a_turn_and_records_writes_and_refusals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import vault
    from looper.agents.ops.planner import PlannerOps

    async def test(pool: Any) -> None:
        await _room_task(pool, vault="on")
        root = _vault_path(tmp_path)
        await vault.update_settings({"path": str(root), "writable": ["codex-mem"]})
        ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
        token = ops.issue("task-r")
        header = f"Bearer {token}"
        args = {"path": "codex-mem/build.md", "content": "Use make gen.", "reason": "keep build command"}
        assert (await ops.call(header, "looper_vault_write", args))["error"]["code"] == "needs_user_turn"
        ops.begin_turn(token)
        written = await ops.call(header, "looper_vault_write", args)
        assert written["status"] == "ok", written
        refused = await ops.call(header, "looper_vault_write", {**args, "path": "mine/diary.md"})
        assert refused["error"]["code"] == "forbidden"
        rows = await pool.fetch("SELECT summary FROM activity WHERE task_id = 'task-r'")
        lines = [r["summary"] for r in rows]
        assert sum(s.startswith("Vault: planner wrote codex-mem/build.md") for s in lines) == 1
        assert sum(s.startswith("Vault: planner's looper_vault_write refused (forbidden)") for s in lines) == 1
        assert not (root / "mine/diary.md").exists()

    _run(test, monkeypatch)


@pytest.mark.parametrize("room_on", [False, True])
def test_run_token_carries_vault_tools_and_mcp_and_rechecks_the_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, room_on: bool,
) -> None:
    from looper.agents import vault
    from looper.agents.library import AgentLibrary
    from looper.agents.ops.room import RoomOps

    async def test(pool: Any) -> None:
        project = await _room_task(pool, room="on" if room_on else "off", vault="on")
        await vault.update_settings({"path": str(_vault_path(tmp_path)), "writable": ["codex-mem"]})
        config = await vault.active()
        room, _ = _room()
        ops = RoomOps(room, url="http://127.0.0.1:9")
        token = ops.issue("task-r", BACKEND, room=room_on, vault=config)
        header = f"Bearer {token}"
        names = {t["name"] for t in ops.listing(header)}
        assert {"looper_vault_search", "looper_vault_read", "looper_vault_write"} <= names
        assert len(names) == (8 if room_on else 3)
        library = await ops.library_with_server(AgentLibrary(), tmp_path / "runtime", token, BACKEND.run)
        server = json.loads(Path(library.mcp_config).read_text())["mcpServers"]["looper"]
        assert server["env"]["LOOPER_AGENT_TOKEN"] == token
        await pool.execute("UPDATE projects SET vault = 'off' WHERE id = $1", project["id"])
        assert (await ops.call(header, "looper_vault_search", {"query": "build"}))["error"]["code"] == "forbidden"
        ops.revoke_run(BACKEND.run)
        assert not ops.authorized(header) and not Path(library.mcp_config).exists()

    _run(test, monkeypatch)


def test_run_token_limits_total_vault_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents import vault
    from looper.agents.ops.room import RoomOps

    async def test(pool: Any) -> None:
        await _room_task(pool, room="off", vault="on")
        await vault.update_settings({"path": str(_vault_path(tmp_path)), "writable": ["codex-mem"]})
        room, _ = _room()
        ops = RoomOps(room, url="http://127.0.0.1:9")
        header = f"Bearer {ops.issue('task-r', BACKEND, room=False, vault=await vault.active())}"
        for index in range(4):
            result = await ops.call(header, "looper_vault_write", {
                "path": f"codex-mem/{index}.md", "content": "x" * (64 * 1024), "reason": "keep note"})
            assert result["status"] == "ok", result
        result = await ops.call(header, "looper_vault_write", {
            "path": "codex-mem/last.md", "content": "x", "reason": "keep note"})
        assert result["error"]["code"] == "rate_limited"
        assert await pool.fetchval("SELECT count(*) FROM activity WHERE summary LIKE 'Vault:%wrote%'") == 4

    _run(test, monkeypatch)


@pytest.mark.parametrize("enabled", ["off", "on"])
@pytest.mark.parametrize("defined_role", [False, True], ids=["built-in", "task-role"])
def test_worker_gets_vault_tools_without_the_room(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: str, defined_role: bool,
) -> None:
    from looper.agents import vault
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.ops.room import RoomOps
    from looper.agents.room import RoomService
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool
    from tests.fake_claude import fake_claude
    from tests.test_task_agents_db import _until

    body = '''
import json, os, sys
flag = next(f for f in ("--append-system-prompt-file", "--system-prompt-file") if f in sys.argv)
record = {"argv": sys.argv[1:], "prompt": open(sys.argv[sys.argv.index(flag) + 1]).read(),
          "first_turn": json.loads(sys.stdin.readline())["message"]["content"]}
open(os.environ["SEEN"], "w").write(json.dumps(record))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps({"status": "completed", "changed_files": {}, "summary": "done"})}), flush=True)
'''
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("SEEN", str(seen))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"SEEN"})

    async def test(pool: Any) -> None:
        await _room_task(pool, room="off", vault=enabled, operator_notes="off")
        await vault.update_settings({"path": str(_vault_path(tmp_path)), "writable": ["codex-mem"]})
        (tmp_path / "work").mkdir()
        bus = MessageBus(runtime_root=tmp_path)
        workers = WorkerPool(config=WorkerConfig(claude_bin=fake_claude(tmp_path / "claude", body)),
                             runtime_root=tmp_path / "runtime")
        workers.room_ops = RoomOps(RoomService(worker_pool=workers), url="http://127.0.0.1:9")
        results: list[MessageEnvelope] = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        await workers.start(bus)
        await asyncio.sleep(0)
        payload = {"task_id": "step-s2", "parent_task_id": "task-r", "step_key": "s2", "instruction": "do it",
                   "context": {"work_dir": str(tmp_path / "work")}}
        if defined_role:
            await pool.execute("INSERT INTO task_roles (task_id, role_key, version, kind, role_prompt) "
                               "VALUES ('task-r', 'fe_rd', 1, 'work', 'You build the UI.')")
            payload["task_role"] = {"id": "fe_rd", "kind": "work", "version": 1,
                                    "role_prompt": "You build the UI.", "harness": None, "profile_id": None}
        try:
            await bus.publish(MessageEnvelope.create(
                source=planner_address("task-r"), target=worker_address("worker-2"), message_type=MessageType.WORK_ITEM,
                payload=payload))
            await _until(lambda: results, "the run's result", seconds=30)
        finally:
            await workers.stop()
            await bus.shutdown()
        record = json.loads(seen.read_text())
        assert results[0].payload["status"] == "completed", results[0].payload
        assert ("--mcp-config" in record["argv"]) is (enabled == "on")
        assert ("## Knowledge vault" in record["prompt"]) is (enabled == "on")
        assert "task room" not in record["prompt"] and "room" not in record["first_turn"]
        assert not workers.room_ops._grants
        if enabled == "on":
            mcp_path = record["argv"][record["argv"].index("--mcp-config") + 1]
            assert not Path(mcp_path).exists()

    _run(test, monkeypatch)


@pytest.mark.parametrize(("configured", "enabled"), [(True, "off"), (False, "on"), (False, "off")])
def test_vault_off_worker_prompt_and_argv_match_the_unconfigured_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured: bool, enabled: str,
) -> None:
    from looper.agents import vault
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.ops.room import RoomOps
    from looper.agents.room import RoomService
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool
    from tests.fake_claude import fake_claude
    from tests.test_task_agents_db import _until

    body = '''
import json, os, sys
flag = next(f for f in ("--append-system-prompt-file", "--system-prompt-file") if f in sys.argv)
record = {"argv": sys.argv[1:], "prompt": open(sys.argv[sys.argv.index(flag) + 1]).read()}
open(os.environ["SEEN"], "w").write(json.dumps(record))
json.loads(sys.stdin.readline())
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps({"status": "completed", "changed_files": {}, "summary": "done"})}), flush=True)
'''
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("SEEN", str(seen))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"SEEN"})
    claude = fake_claude(tmp_path / "claude", body)

    async def run_worker() -> dict[str, Any]:
        bus = MessageBus(runtime_root=tmp_path)
        workers = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=tmp_path / "runtime")
        workers.room_ops = RoomOps(RoomService(worker_pool=workers), url="http://127.0.0.1:9")
        results: list[MessageEnvelope] = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        await workers.start(bus)
        await asyncio.sleep(0)
        try:
            await bus.publish(MessageEnvelope.create(
                source=planner_address("task-r"), target=worker_address("worker-2"), message_type=MessageType.WORK_ITEM,
                payload={"task_id": "step-s2", "parent_task_id": "task-r", "step_key": "s2", "instruction": "do it",
                         "context": {"work_dir": str(tmp_path / "work")}}))
            await _until(lambda: results, "the run's result", seconds=30)
        finally:
            await workers.stop()
            await bus.shutdown()
        assert results[0].payload["status"] == "completed", results[0].payload
        return json.loads(seen.read_text())

    async def test(pool: Any) -> None:
        project = await _room_task(pool, room="off", vault="off", operator_notes="off")
        (tmp_path / "work").mkdir()
        baseline = await run_worker()
        if configured:
            await vault.update_settings({"path": str(_vault_path(tmp_path)), "writable": ["codex-mem"]})
        await pool.execute("UPDATE projects SET vault = $2 WHERE id = $1", project["id"], enabled)
        record = await run_worker()
        assert record["prompt"] == baseline["prompt"]
        assert "Knowledge vault" not in record["prompt"]
        assert "Knowledge vault" not in baseline["prompt"]
        assert "--mcp-config" not in record["argv"]
        assert "--mcp-config" not in baseline["argv"]

    _run(test, monkeypatch)


def test_concurrent_planner_vault_calls_commit_once_and_record_one_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import vault
    from looper.agents.ops.planner import PlannerOps
    from tests.test_vault import git

    async def test(pool: Any) -> None:
        await _room_task(pool, room="off", vault="on")
        root = _vault_path(tmp_path)
        git(root, "init")
        git(root, "config", "user.name", "Vault Tester")
        git(root, "config", "user.email", "vault@example.test")
        note = root / "codex-mem/build.md"
        note.write_text("initial synthetic note")
        git(root, "add", "--", "codex-mem/build.md")
        git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "initial note")
        before = git(root, "rev-parse", "HEAD")
        await vault.update_settings({"path": str(root), "writable": ["codex-mem"]})
        config = await vault.active()
        expected = (await vault.read(config, "codex-mem/build.md"))["sha256"]
        ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
        token = ops.issue("task-r")
        header = f"Bearer {token}"
        ops.begin_turn(token)
        args = {"path": "codex-mem/build.md", "expected_sha256": expected, "reason": "keep build note"}
        try:
            results = await asyncio.gather(*(ops.call(header, "looper_vault_write", {**args, "content": text})
                                             for text in ("first synthetic note", "second synthetic note")))
        finally:
            ops.end_turn(token)
            ops.revoke(token)
        assert sum(result["status"] == "ok" for result in results) == 1, results
        assert sum(result.get("error", {}).get("code") == "conflict" for result in results) == 1, results
        assert git(root, "rev-list", "--count", f"{before}..HEAD") == "1"
        rows = await pool.fetch("SELECT summary FROM activity WHERE task_id = 'task-r'")
        assert sum(row["summary"].startswith("Vault: planner wrote") for row in rows) == 1

    _run(test, monkeypatch)
