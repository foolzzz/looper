"""The Manager's configuration tools on a real PostgreSQL: each call changes what the dashboard's API reads back,
refuses with the dashboard's own message, asks the user first where it is ``high``, and leaves the trail.

Each call goes through ``ManagerOps`` as the endpoint hands it over, in a turn
of a Lark chat (``tests.test_ops_tasks_db._Chat``); what changed is read back
through the dashboard's HTTP API. Runs only when LOOPER_TEST_DATABASE_URL
points at a disposable database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents.ops import Turn
from tests.test_ops_tasks_db import _Chat, _error, _project, _task
from tests.test_task_agents_db import _call, _concurrency_chat
from tests.test_usage_api_db import DSN, _run, _run_record

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SECRET = "sk-test-0123456789"


async def _ok(method: str, path: str, body: Any = None) -> Any:
    status, answer = await _call(method, path, body)
    assert status in (200, 201), (status, answer)
    return answer


async def _trail(pool: Any) -> list[tuple[str, str, str]]:
    return [(r["tool"], r["risk"], r["status"]) for r in await pool.fetch(
        "SELECT tool, risk, status FROM manager_actions ORDER BY id")]


async def _sonnet(pool: Any) -> None:
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model) VALUES "
                       "('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', 'prov-claude-subscription', 'sonnet')")


# --- looper_projects ---


def test_projects_are_created_changed_and_read_back_as_the_dashboard_reads_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        (created,) = await chat.say("建一个项目 tetris，目录 /srv/tetris", ("looper_projects", {
            "action": "create", "name": "tetris", "work_dir": "/srv/tetris", "background": "A falling-blocks game."}))
        assert created["status"] == "ok", created
        (changed,) = await chat.say("把 tetris 改名叫 Tetris2，背景改成 Blocks.", ("looper_projects", {
            "action": "update", "project": "TETRIS", "name": "Tetris2", "background": "Blocks."}))
        assert changed["result"]["project"]["name"] == "Tetris2"
        (project,) = [p for p in (await _ok("GET", "/api/projects"))["projects"] if p["name"] == "Tetris2"]
        assert (project["work_dir"], project["background"]) == ("/srv/tetris", "Blocks.")
        (listed,) = await chat.say("有哪些项目", ("looper_projects", {"action": "list"}))
        assert listed["result"]["projects"] == [{"project": "Tetris2", "work_dir": "/srv/tetris", "tasks": 0,
                                                 "active_tasks": 0, "schedules": 0, "background": "Blocks."}]
        (got,) = await chat.say("看看 Tetris2", ("looper_projects", {"action": "get", "project": "tetris2"}))
        assert got["result"]["project"]["project_id"] == project["project_id"]
        # Writes keep their result in the trail; reads do not.
        assert await _trail(pool) == [("looper_projects", "write", "ok"), ("looper_projects", "write", "ok"),
                                      ("looper_projects", "read", "ok"), ("looper_projects", "read", "ok")]
        assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE risk = 'read' AND result IS NOT NULL") \
            == 0

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "code", "message"), [
    ({"action": "create", "name": "calc"}, "conflict", "a project named 'calc' already exists"),
    ({"action": "create", "name": "x", "work_dir": "relative/dir"}, "invalid", "absolute"),
    ({"action": "create"}, "invalid", "name is required for action create"),
    ({"action": "update", "project": "nope", "name": "y"}, "not_found", "no project named 'nope'"),
    ({"action": "update", "project": "calc"}, "invalid", "give at least one of name"),
])
def test_a_project_change_is_refused_with_the_dashboards_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], code: str, message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool)
        (answer,) = await _Chat(tmp_path).say("改项目", ("looper_projects", arguments))
        assert _error(answer) == code and message in answer["error"]["message"], answer
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    _run(test, monkeypatch)


def test_deleting_a_project_asks_first_and_runs_only_on_the_next_messages_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool, "scratch", "/srv/scratch")
        chat = _Chat(tmp_path)
        (asked,) = await chat.say("删掉 scratch 项目", ("looper_projects", {"action": "delete", "project": "scratch"}))
        assert asked["status"] == "needs_confirmation"
        (block,) = chat.turns[0].blocks
        assert "要删除项目「scratch」（默认目录：/srv/scratch）吗？它没有任务和定时任务。" in block
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1
        assert await chat.say("算了") == []  # a message that does not confirm it: it lapses
        (again,) = await chat.say("还是删吧", ("looper_projects", {"action": "delete", "project": "scratch"}))
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": again["confirmation_id"]}))
        assert done == {"status": "ok", "result": {"deleted": "scratch"}}
        assert (await _call("GET", f"/api/projects/{project['id']}/agent-settings"))[0] == 404
        assert await _trail(pool) == [("looper_projects", "high", "needs_confirmation"),
                                      ("looper_projects", "high", "needs_confirmation"),
                                      ("looper_projects", "high", "ok")]

    _run(test, monkeypatch)


def test_a_project_with_tasks_is_not_offered_for_deletion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "completed", await _project(pool))
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("删掉 calc", ("looper_projects", {"action": "delete", "project": "calc"}))
        assert _error(answer) == "conflict" and "still has tasks" in answer["error"]["message"]
        assert chat.turns[0].blocks == []  # nothing was asked
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    _run(test, monkeypatch)


# --- looper_agent_profiles and looper_providers ---


def test_agent_profiles_are_created_changed_and_read_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        (created,) = await chat.say("加一个 sonnet 的 profile", ("looper_agent_profiles", {
            "action": "create", "name": "fast", "runtime": "claude-code", "provider": "CLAUDE-SUBSCRIPTION",
            "model": "sonnet", "effort": "low"}))
        assert created["status"] == "ok", created
        profile = created["result"]["profile"]
        assert (profile["runtime"], profile["provider"], profile["model"], profile["effort"]) == (
            "claude-code", "claude-subscription", "sonnet", "low")
        (changed,) = await chat.say("fast 的 effort 去掉", ("looper_agent_profiles", {
            "action": "update", "profile": "fast", "effort": None}))
        assert changed["result"]["profile"]["effort"] is None
        (registered,) = [p for p in (await _ok("GET", "/api/registry"))["profiles"] if p["name"] == "fast"]
        assert (registered["profile_id"], registered["model"], registered["effort"]) == (
            profile["profile_id"], "sonnet", None)
        (listed,) = await chat.say("有哪些 profile", ("looper_agent_profiles", {"action": "list"}))
        assert [p["name"] for p in listed["result"]["profiles"]] == ["claude-code/opus", "fast"]
        assert listed["result"]["runtimes"] == [{"runtime_id": "rt-claude-code", "name": "claude-code",
                                                 "type": "claude-code", "roles": ["manager", "planner", "worker",
                                                                                  "reviewer"]}]
        # A profile's id works where its name does; an unknown one is named with the ones there are.
        (unknown,) = await chat.say("改 slow", ("looper_agent_profiles", {"action": "update", "profile": "slow",
                                                                          "model": "opus"}))
        assert _error(unknown) == "not_found" and "claude-code/opus, fast" in unknown["error"]["message"]
        (taken,) = await chat.say("再加一个", ("looper_agent_profiles", {
            "action": "create", "name": "fast", "runtime": "rt-claude-code", "provider": "prov-claude-subscription",
            "model": "opus"}))
        assert _error(taken) == "conflict" and "already exists" in taken["error"]["message"]

    _run(test, monkeypatch)


def test_deleting_an_agent_profile_asks_first_and_one_in_use_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _sonnet(pool)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": "agent-sonnet"})
        chat = _Chat(tmp_path)
        (used,) = await chat.say("删掉 sonnet", ("looper_agent_profiles", {"action": "delete",
                                                                         "profile": "claude-code/sonnet"}))
        assert _error(used) == "conflict" and "selected by the worker role" in used["error"]["message"]
        assert chat.turns[0].blocks == []
        await _ok("PUT", "/api/agent-settings/roles/worker", {"profile_id": None})
        (asked,) = await chat.say("删掉 sonnet", ("looper_agent_profiles", {"action": "delete",
                                                                          "profile": "agent-sonnet"}))
        assert "要删除 Agent Profile「claude-code/sonnet」（claude-code · claude-subscription · sonnet）吗？" in \
            chat.turns[1].blocks[0]
        assert await pool.fetchval("SELECT count(*) FROM agent_profiles WHERE id = 'agent-sonnet'") == 1
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["result"] == {"deleted": "claude-code/sonnet"}
        assert [p["name"] for p in (await _ok("GET", "/api/registry"))["profiles"]] == ["claude-code/opus"]

    _run(test, monkeypatch)


def test_a_provider_is_registered_without_a_key_and_the_user_told_where_to_enter_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("登记 DeepSeek", ("looper_providers", {
            "action": "register", "name": "deepseek", "protocols": ["anthropic-messages"],
            "base_url": "https://api.deepseek.com/anthropic", "models": ["deepseek-flash"],
            "env": {"API_TIMEOUT_MS": "3000000"}}))
        assert answer["status"] == "ok", answer
        provider = answer["result"]["provider"]
        assert (provider["auth"], provider["models"], provider["api_key"]) == (
            "api_key", ["deepseek-flash"], "missing: the user enters it on the dashboard")
        assert "Settings → LLM Providers → deepseek → API key" in answer["result"]["note"]
        (stored,) = [p for p in (await _ok("GET", "/api/registry"))["providers"] if p["name"] == "deepseek"]
        assert (stored["api_key"], stored["base_url"], stored["env"]) == (
            None, "https://api.deepseek.com/anthropic", [{"key": "API_TIMEOUT_MS", "value": "3000000"}])
        (listed,) = await chat.say("有哪些 provider", ("looper_providers", {"action": "list"}))
        assert [p["name"] for p in listed["result"]["providers"]] == ["claude-subscription", "codex-login",
                                                                       "deepseek"]
        # A credential in its environment is refused as the dashboard refuses it.
        (refused,) = await chat.say("再登记一个", ("looper_providers", {
            "action": "register", "name": "other", "protocols": ["anthropic-messages"],
            "env": {"OTHER_API_TOKEN": "x"}}))
        assert _error(refused) == "invalid" and "looks like a credential" in refused["error"]["message"]

    _run(test, monkeypatch)


@pytest.mark.parametrize("name", ["api_key", "apiKey", "password", "credential"])
def test_no_tool_takes_a_key_and_none_is_kept_or_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str,
) -> None:
    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        (answer,) = await chat.say(f"登记 deepseek，key 是 {SECRET}", ("looper_providers", {
            "action": "register", "name": "deepseek", "protocols": ["anthropic-messages"], name: SECRET}))
        assert _error(answer) == "forbidden"
        assert "Settings → LLM Providers" in answer["error"]["message"] and SECRET not in json.dumps(answer)
        assert await pool.fetchval("SELECT count(*) FROM llm_providers WHERE name = 'deepseek'") == 0
        assert await pool.fetchval("SELECT count(*) FROM llm_provider_secrets") == 0
        (row,) = await pool.fetch("SELECT arguments, result FROM manager_actions")
        assert SECRET not in row["arguments"] + row["result"]
        assert json.loads(row["arguments"])[name] == "***"

    _run(test, monkeypatch)


# --- looper_settings ---


async def _project_settings(project_id: str) -> dict[str, Any]:
    return await _ok("GET", f"/api/projects/{project_id}/agent-settings")


def test_a_role_in_a_projects_profile_is_set_at_once_and_reads_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _sonnet(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("calc 里的 Worker 用 sonnet", ("looper_settings", {
            "action": "set", "project": "calc", "role": "worker", "values": {"profile": "claude-code/sonnet"}}))
        assert answer["status"] == "ok", answer
        assert chat.turns[0].blocks == []
        assert answer["result"]["changes"] == [{"setting": "profile_id", "before": "未设置（继承上一层）",
                                                "after": "claude-code/sonnet"}]
        assert answer["result"]["effective"]["worker"]["profile"] == {"value": "claude-code/sonnet",
                                                                      "source": "role_project"}
        worker = (await _project_settings(project["id"]))["roles"]["worker"]
        assert worker["values"]["profile_id"] == "agent-sonnet"
        assert worker["effective"]["profile"]["value"]["name"] == "claude-code/sonnet"
        # The view says the same as the dashboard.
        (view,) = await chat.say("看看 calc 的设置", ("looper_settings", {"action": "view", "project": "calc"}))
        assert view["result"]["roles"]["worker"]["set"] == {"profile_id": "claude-code/sonnet"}
        assert view["result"]["roles"]["planner"]["effective"]["profile"] == {"value": "claude-code/opus",
                                                                              "source": "builtin"}
        assert await _trail(pool) == [("looper_settings", "write", "ok"), ("looper_settings", "read", "ok")]

    _run(test, monkeypatch)


def test_the_shared_defaults_change_only_after_the_user_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        await _sonnet(pool)
        chat = _Chat(tmp_path)
        change = {"action": "set", "values": {"profile": "claude-code/sonnet", "env": {"LOG_LEVEL": "debug"}}}
        (asked,) = await chat.say("所有角色默认用 sonnet", ("looper_settings", change))
        assert asked["status"] == "needs_confirmation"
        assert chat.turns[0].blocks[0].splitlines()[1:4] == [
            "要修改共用默认（Settings → Roles → Shared defaults：所有角色都继承，下层设置的除外）吗？",
            "- Agent Profile：未设置（继承上一层） → claude-code/sonnet",
            "- 环境变量：未设置（继承上一层） → LOG_LEVEL=debug",
        ]
        assert (await _ok("GET", "/api/agent-settings"))["system"]["values"]["profile_id"] is None
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["status"] == "ok", done
        system = (await _ok("GET", "/api/agent-settings"))["system"]["values"]
        assert (system["profile_id"], system["env"]) == ("agent-sonnet", [{"key": "LOG_LEVEL", "value": "debug"}])
        assert set(done["result"]["effective"]) == {"manager", "planner", "worker", "reviewer"}
        assert await _trail(pool) == [("looper_settings", "high", "needs_confirmation"),
                                      ("looper_settings", "high", "ok")]

    _run(test, monkeypatch)


def test_a_roles_default_limit_changes_only_after_the_user_confirms_and_a_projects_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        chat = _concurrency_chat(tmp_path)
        # A project has no limits: refused with the reason, nothing asked or stored.
        (refused,) = await chat.say("calc 的 Worker 并发改成 3", ("looper_settings", {
            "action": "set", "project": "calc", "role": "worker", "values": {"max_concurrency": 3}}))
        assert refused["status"] == "error" and "concurrency is per task" in refused["error"]["message"]
        assert chat.turns[0].blocks == [] and chat.workers.rechecks == 0
        assert "max_concurrency" not in (await _project_settings(project["id"]))["roles"]["worker"]["effective"]

        (asked,) = await chat.say("Worker 的默认并发改成 3", ("looper_settings", {
            "action": "set", "role": "worker", "values": {"max_concurrency": 3}}))
        assert asked["status"] == "needs_confirmation"
        lines = chat.turns[1].blocks[0].splitlines()
        assert lines[2] == "- 并发上限（每个任务同时运行的 Worker 数）：16（内置默认） → 3（角色默认）"
        assert "调低不会停止正在运行的工作" in lines[3]
        assert chat.workers.rechecks == 0
        (done,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert done["status"] == "ok", done
        assert (chat.router.rechecks, chat.workers.rechecks) == (0, 1)
        assert done["result"]["effective"]["worker"]["max_concurrency"] == {"value": 3, "source": "role"}
        # The Planner has none.
        (planner,) = await chat.say("Planner 并发 2", ("looper_settings", {
            "action": "set", "role": "planner", "values": {"max_concurrency": 2}}))
        assert planner["status"] == "error" and "concurrency is per task" in planner["error"]["message"]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("start", "change", "asks"), [
    (None, 5.0, False),  # no cap at all before: a cap only lowers it
    (5.0, 2.0, False),
    (2.0, 5.0, True),
    (2.0, None, True),  # back to no cap
])
def test_raising_a_spend_cap_asks_first_and_lowering_it_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, start: float | None, change: float | None, asks: bool,
) -> None:
    async def test(pool: Any) -> None:
        if start is not None:
            await _ok("PUT", "/api/agent-settings/roles/worker", {"run_budget_usd": start})
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("改 Worker 的单次花费上限", ("looper_settings", {
            "action": "set", "role": "worker", "values": {"run_budget_usd": change}}))
        worker = (await _ok("GET", "/api/agent-settings"))["roles"]["worker"]["values"]
        if asks:
            assert answer["status"] == "needs_confirmation"
            assert "这会提高（或取消）运行的花费上限。" in chat.turns[0].blocks[0]
            assert worker["run_budget_usd"] == start
        else:
            assert answer["status"] == "ok", answer
            assert worker["run_budget_usd"] == change

    _run(test, monkeypatch)


def test_a_cap_set_lower_down_counts_against_what_runs_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A project's own cap above the role's raises what its runs get, though the project had none of its own."""

    async def test(pool: Any) -> None:
        await _project(pool)
        await _ok("PUT", "/api/agent-settings/roles/worker", {"run_budget_usd": 1.0})
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("calc 的单次上限 3", ("looper_settings", {
            "action": "set", "project": "calc", "values": {"run_budget_usd": 3.0}}))
        assert answer["status"] == "needs_confirmation"

    _run(test, monkeypatch)


def test_a_roles_timeout_is_set_as_settings_roles_saves_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.serve import seed_role_configs

    async def test(pool: Any) -> None:
        await seed_role_configs()
        await _project(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("Worker 超时改成一小时", ("looper_settings", {
            "action": "set", "role": "worker", "values": {"timeout_seconds": 3600}}))
        assert answer["status"] == "ok", answer
        assert answer["result"]["changes"] == [{"setting": "timeout_seconds", "before": 86400, "after": 3600}]
        assert (await _ok("GET", "/api/roles/worker"))["timeout_seconds"] == 3600
        (refused,) = await chat.say("calc 的 Worker 超时一小时", ("looper_settings", {
            "action": "set", "project": "calc", "role": "worker", "values": {"timeout_seconds": 3600}}))
        assert _error(refused) == "invalid" and "a role's own" in refused["error"]["message"]

    _run(test, monkeypatch)


@pytest.mark.parametrize(("arguments", "message"), [
    ({"role": "worker", "values": {"effort": "high"}}, "values has an unknown key 'effort'"),
    ({"role": "worker", "values": {"env": {"MY_TOKEN": "x"}}}, "MY_TOKEN looks like a credential"),
    ({"values": {"max_concurrency": 2}}, "concurrency is per task: max_concurrency is how many of one task's"),
    ({"role": "manager", "values": {"max_concurrency": 2}}, "max_concurrency is how many"),
    ({"role": "manager", "project": "calc", "values": {"harness_prompt": "x"}}, "the manager spans every project"),
    ({"role": "worker", "values": {"profile": "nope"}}, "no Agent Profile 'nope'; there are: claude-code/opus"),
    ({"role": "worker", "values": {}}, "values: give at least one setting"),
    ({"role": "worker", "values": {"run_budget_usd": -1}}, "run_budget_usd must be more than 0"),
])
def test_a_settings_change_is_refused_with_the_dashboards_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: dict[str, Any], message: str,
) -> None:
    async def test(pool: Any) -> None:
        await _project(pool)
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("改设置", ("looper_settings", {"action": "set", **arguments}))
        assert _error(answer) in ("invalid", "not_found") and message in answer["error"]["message"], answer
        assert chat.turns[0].blocks == []
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers") == 0

    _run(test, monkeypatch)


def test_a_projects_allowed_profiles_change_as_its_settings_tab_sets_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        await _sonnet(pool)
        chat = _Chat(tmp_path)

        async def allowed() -> list[str]:
            return [p["name"] for p in (await _ok("GET", f"/api/projects/{project['id']}/allowed-profiles"))[
                "profiles"]]

        (answer,) = await chat.say("calc 的规划者可以选 sonnet", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "add": ["claude-code/sonnet"]}))
        assert answer["status"] == "ok", answer
        api = await _ok("GET", f"/api/projects/{project['id']}/allowed-profiles")
        assert answer["result"]["allowed_profiles"] == api["profiles"]
        assert await allowed() == ["claude-code/sonnet"] and answer["result"]["before"] == []
        # Adding one keeps what is there; removing one keeps the rest.
        (more,) = await chat.say("也可以选 opus", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "add": ["agent-claude-code-opus"]}))
        assert more["result"]["before"] == ["claude-code/sonnet"]
        assert sorted(await allowed()) == ["claude-code/opus", "claude-code/sonnet"]
        (fewer,) = await chat.say("不要 sonnet 了", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "remove": ["CLAUDE-CODE/SONNET"]}))
        assert fewer["status"] == "ok" and await allowed() == ["claude-code/opus"]
        (view,) = await chat.say("看看 calc", ("looper_settings", {"action": "view", "project": "calc"}))
        assert view["result"]["allowed_profiles"] == ["claude-code/opus"]
        # Refused as the tab refuses, or as not on the list; nothing changes either way.
        (unknown,) = await chat.say("再加一个", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "add": ["claude-code/sonnet", "nope"]}))
        assert _error(unknown) == "invalid" and "no Agent Profile 'nope'" in unknown["error"]["message"]
        (absent,) = await chat.say("去掉 sonnet", ("looper_settings", {
            "action": "allowed_profiles", "project": "calc", "remove": ["claude-code/sonnet"], "add": ["fast"]}))
        assert _error(absent) == "not_found" and "is not on the list" in absent["error"]["message"]
        (empty,) = await chat.say("改列表", ("looper_settings", {"action": "allowed_profiles", "project": "calc"}))
        assert _error(empty) == "invalid"
        assert await allowed() == ["claude-code/opus"]

    _run(test, monkeypatch)


def test_a_turn_the_manager_runs_on_its_own_reads_settings_but_changes_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.serve import seed_role_configs

    async def test(pool: Any) -> None:
        await seed_role_configs()
        await _project(pool)
        chat = _Chat(tmp_path)
        chat.ops.begin_turn(own := Turn(channel=None, chat=None))
        try:
            view = await chat.ops.call("looper_settings", {"action": "view"})
            listed = await chat.ops.call("looper_projects", {"action": "list"})
            changed = await chat.ops.call("looper_settings", {"action": "set", "role": "worker",
                                                              "values": {"timeout_seconds": 60}})
            created = await chat.ops.call("looper_projects", {"action": "create", "name": "x"})
        finally:
            chat.ops.end_turn(own)
        assert (view["status"], listed["status"]) == ("ok", "ok")
        assert _error(changed) == _error(created) == "needs_user_turn"
        assert (await _ok("GET", "/api/roles/worker"))["timeout_seconds"] == 86400
        assert await pool.fetchval("SELECT count(*) FROM projects") == 1

    _run(test, monkeypatch)


# --- looper_catalog ---


def test_a_catalog_item_is_installed_changed_and_uninstalled_as_the_dashboard_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        project = await _project(pool)
        server = (await _ok("POST", "/api/catalog/mcp", {"name": "search", "transport": "stdio", "command": "python3",
                                                          "env": {"SEARCH_KEY": SECRET}}))["item"]
        chat = _Chat(tmp_path)
        (listed,) = await chat.say("有哪些 MCP", ("looper_catalog", {"action": "list"}))
        assert listed["result"]["items"] == [{"item_id": server["item_id"], "kind": "mcp", "name": "search",
                                              "description": None, "project": None, "installs": []}]
        assert SECRET not in json.dumps(listed)
        (installed,) = await chat.say("calc 的 Worker 装上 search", ("looper_catalog", {
            "action": "install", "item": "search", "project": "calc", "roles": ["worker"]}))
        assert installed["status"] == "ok", installed
        install_id = installed["result"]["install"]["install_id"]
        (entry,) = (await _ok("GET", f"/api/projects/{project['id']}/installs"))["installs"]
        assert (entry["install_id"], entry["roles"], entry["inherited"]) == (install_id, ["worker"], False)
        (again,) = await chat.say("再装一次", ("looper_catalog", {"action": "install", "item": "search",
                                                                "project": "calc"}))
        assert _error(again) == "conflict" and "installed there already" in again["error"]["message"]
        (manager,) = await chat.say("calc 里给管家装", ("looper_catalog", {
            "action": "install", "item": "search", "project": "calc", "roles": ["manager"]}))
        assert _error(manager) == "invalid" and "cannot include the manager" in manager["error"]["message"]
        (changed,) = await chat.say("Reviewer 也装上", ("looper_catalog", {
            "action": "update", "install_id": install_id, "roles": ["worker", "reviewer"]}))
        assert changed["result"]["install"]["roles"] == ["worker", "reviewer"]
        (here,) = await chat.say("calc 里有什么", ("looper_catalog", {"action": "list", "project": "calc"}))
        assert [(i["item"]["name"], i["roles"]) for i in here["result"]["installs"]] == [
            ("search", ["worker", "reviewer"])]
        (gone,) = await chat.say("卸掉", ("looper_catalog", {"action": "uninstall", "install_id": install_id}))
        assert gone["status"] == "ok"
        assert (await _ok("GET", f"/api/projects/{project['id']}/installs"))["installs"] == []

    _run(test, monkeypatch)


# --- looper_costs ---


async def _at(pool: Any, record_id: int, when: datetime) -> None:
    await pool.execute("UPDATE llm_records SET created_at = $2 WHERE id = $1", record_id, when)


def test_a_tasks_cost_is_the_task_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        await _task(pool, "task-a", "completed", await _project(pool))
        for agent_id, cost in (("planner-task-a", 0.12), ("worker-1", 0.4), ("reviewer-2", 0.05)):
            await _run_record(pool, agent_id, task_id="task-a", cost=cost)
        before = (await _ok("GET", "/api/tasks/task-a"))["cost"]["cost_usd"]
        record_id = await _run_record(pool, "manager", cost=7.0)
        await llm_records.link_task(record_id, "task-a", pool=pool)
        (answer,) = await _Chat(tmp_path).say("task-a 花了多少", ("looper_costs", {"task_id": "task-a"}))
        page = await _ok("GET", "/api/tasks/task-a")
        assert page["cost"]["cost_usd"] == before
        assert answer["result"]["cost"] == page["cost"]
        assert answer["result"]["cost"]["by_model"] == page["cost"]["by_model"]
        assert "manager_by_model" in answer["result"]["note"]
        assert answer["result"]["cost"]["cost_usd"] == pytest.approx(0.57)
        (mixed,) = await _Chat(tmp_path).say("task-a 今天花了多少", ("looper_costs", {"task_id": "task-a",
                                                                                     "period": "today"}))
        assert _error(mixed) == "invalid"

    _run(test, monkeypatch)


def test_spend_by_project_and_time_range_counts_the_runs_started_in_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.overview import local_midnight
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        calc, other = await _project(pool), await _project(pool, "other", "/o")
        await _task(pool, "task-a", "completed", calc)
        await _task(pool, "task-b", "completed", calc)
        await _task(pool, "task-c", "completed", other)
        now = datetime.now(timezone.utc)
        await _run_record(pool, "worker-1", task_id="task-a", cost=0.4)
        await _run_record(pool, "planner-task-b", task_id="task-b", cost=0.1)
        await _run_record(pool, "worker-2", task_id="task-c", cost=0.2)
        await _run_record(pool, "manager", cost=0.03)
        await _run_record(pool, "manager", task_id="task-a", cost=7.0)  # the Manager's: no task's, no project's
        await _at(pool, await _run_record(pool, "worker-3", task_id="task-a", cost=5.0), now - timedelta(days=40))
        chat = _Chat(tmp_path)

        (calc_now,) = await chat.say("calc 最近花了多少", ("looper_costs", {
            "project": "calc", "since": (now - timedelta(days=7)).date().isoformat()}))
        result = calc_now["result"]
        assert result["total"]["provider_priced_usd"] + result["total"]["equivalent_usd"] == pytest.approx(0.5)
        assert [(t["task_id"], t["runs"]) for t in result["tasks"]] == [("task-a", 1), ("task-b", 1)]
        # All time, the run of 40 days ago counts too; a project's total is the sum of its tasks' costs.
        (calc_all,) = await chat.say("calc 一共花了多少", ("looper_costs", {"project": "calc"}))
        total = calc_all["result"]["total"]
        task_costs = [(await _ok("GET", f"/api/tasks/{t}"))["cost"]["cost_usd"] for t in ("task-a", "task-b")]
        assert total["provider_priced_usd"] + total["equivalent_usd"] == pytest.approx(sum(task_costs)) == \
            pytest.approx(5.5)

        (today,) = await chat.say("今天花了多少", ("looper_costs", {"period": "today"}))
        spend = await llm_records.spend_since(local_midnight())  # the overview's today
        assert {k: today["result"]["total"][k] for k in ("runs", "provider_priced_usd", "equivalent_usd")} == \
            {k: spend[k] for k in ("runs", "provider_priced_usd", "equivalent_usd")}
        assert [(p["project"], p["tasks"], pytest.approx(p["provider_priced_usd"] + p["equivalent_usd"]))
                for p in today["result"]["projects"]] == [("calc", 2, 0.5), ("other", 1, 0.2)]
        # A day as until counts that whole day.
        (to_today,) = await chat.say("calc 到今天为止", ("looper_costs", {
            "project": "calc", "until": datetime.now().astimezone().date().isoformat()}))
        assert to_today["result"]["total"]["runs"] == 3
        (window,) = await chat.say("40 天前那天", ("looper_costs", {
            "since": (now - timedelta(days=41)).isoformat(), "until": (now - timedelta(days=39)).isoformat()}))
        assert window["result"]["total"]["runs"] == 1
        (bad,) = await chat.say("上周", ("looper_costs", {"since": "last week"}))
        assert _error(bad) == "invalid" and "YYYY-MM-DD" in bad["error"]["message"]

    _run(test, monkeypatch)
