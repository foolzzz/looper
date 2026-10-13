"""What every agent inherits: the harness prompt, the model, the effort and the environment.

The harness is resolved in one place for an agent type and a project: the
Manager's own override, a project's Planner or Worker override (a Reviewer
follows its project's Workers), else the Settings default, else the built-in
one. The model is the agent's own, else the Settings default, else opus.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from loopx.agents import agent_settings
from loopx.agents.agent_settings import (
    BUILTIN_DEFAULTS,
    DEFAULT_HARNESS_PROMPT,
    DEFAULT_MODEL,
    EFFORT_KEY,
    ENV_KEY,
    HARNESS_KEY,
    MODEL_KEY,
    AgentDefaults,
    HarnessSource,
    ModelSource,
    SettingsError,
    defaults_from,
    resolve_harness,
    resolve_launch,
)
from loopx.agents.http_server import AgentHTTPServer
from loopx.agents.tiers.task import TaskRouter


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SYSTEM = AgentDefaults("SYSTEM HARNESS", HarnessSource.SYSTEM_DEFAULT, "sonnet", ModelSource.SYSTEM_DEFAULT)


def _project(project_id: str, *, planner: str = "", worker: str = "") -> dict[str, Any]:
    return {"id": project_id, "name": project_id, "background": "", "planner_harness_prompt": planner,
            "worker_harness_prompt": worker}


# --- Harness resolution ---


class TestHarnessResolution:
    def test_without_settings_every_agent_gets_the_builtin_harness(self) -> None:
        project = _project("proj-a")
        for agent in ("manager", "planner", "worker", "reviewer"):
            harness = resolve_harness(agent, BUILTIN_DEFAULTS, project=None if agent == "manager" else project)
            assert (harness.text, harness.source) == (DEFAULT_HARNESS_PROMPT, HarnessSource.BUILTIN_DEFAULT)
            assert harness.project_id is None

    def test_the_settings_default_applies_to_every_agent_without_an_override(self) -> None:
        project = _project("proj-a")
        for agent in ("manager", "planner", "worker", "reviewer"):
            harness = resolve_harness(agent, SYSTEM, project=None if agent == "manager" else project)
            assert (harness.text, harness.source) == ("SYSTEM HARNESS", HarnessSource.SYSTEM_DEFAULT)

    def test_the_manager_override_affects_only_the_manager(self) -> None:
        manager = resolve_harness("manager", SYSTEM, manager_override="MANAGER HARNESS")
        assert (manager.text, manager.source) == ("MANAGER HARNESS", HarnessSource.MANAGER_OVERRIDE)
        project = _project("proj-a")
        for agent in ("planner", "worker", "reviewer"):
            # A Manager override handed to a task agent by mistake is still not used.
            other = resolve_harness(agent, SYSTEM, manager_override="MANAGER HARNESS", project=project)
            assert other.text == "SYSTEM HARNESS"

    def test_a_project_override_reaches_its_planner_and_its_workers(self) -> None:
        project = _project("proj-a", planner="PLANNER A", worker="WORKER A")
        planner = resolve_harness("planner", SYSTEM, project=project)
        worker = resolve_harness("worker", SYSTEM, project=project)
        assert (planner.text, planner.source, planner.project_id) == (
            "PLANNER A", HarnessSource.PROJECT_OVERRIDE, "proj-a")
        assert (worker.text, worker.source, worker.project_id) == ("WORKER A", HarnessSource.PROJECT_OVERRIDE, "proj-a")

    def test_a_reviewer_follows_its_projects_worker_harness(self) -> None:
        reviewer = resolve_harness("reviewer", SYSTEM, project=_project("proj-a", planner="PLANNER A", worker="WORKER A"))
        assert (reviewer.text, reviewer.source, reviewer.project_id) == (
            "WORKER A", HarnessSource.PROJECT_OVERRIDE, "proj-a")
        # Without a Worker override the Reviewer does not take the Planner's.
        alone = resolve_harness("reviewer", SYSTEM, project=_project("proj-a", planner="PLANNER A"))
        assert alone.text == "SYSTEM HARNESS"

    def test_one_projects_override_does_not_leak_to_another_project(self) -> None:
        a = _project("proj-a", planner="PLANNER A", worker="WORKER A")
        b = _project("proj-b")
        for agent in ("planner", "worker", "reviewer"):
            assert resolve_harness(agent, SYSTEM, project=a).project_id == "proj-a"
            in_b = resolve_harness(agent, SYSTEM, project=b)
            assert (in_b.text, in_b.source, in_b.project_id) == ("SYSTEM HARNESS", HarnessSource.SYSTEM_DEFAULT, None)

    def test_a_planner_override_is_not_the_workers_and_the_manager_ignores_projects(self) -> None:
        project = _project("proj-a", planner="PLANNER A")
        assert resolve_harness("worker", SYSTEM, project=project).text == "SYSTEM HARNESS"
        assert resolve_harness("manager", SYSTEM, project=_project("proj-a", worker="W")).text == "SYSTEM HARNESS"

    @pytest.mark.parametrize("blank", ["", "   \n\n"])
    def test_a_blank_override_inherits(self, blank: str) -> None:
        assert resolve_harness("worker", SYSTEM, project=_project("p", worker=blank)).source is HarnessSource.SYSTEM_DEFAULT
        assert resolve_harness("manager", SYSTEM, manager_override=blank).source is HarnessSource.SYSTEM_DEFAULT

    def test_the_record_names_the_source_the_hash_and_the_project(self) -> None:
        harness = resolve_harness("worker", SYSTEM, project=_project("proj-a", worker="WORKER A"))
        assert harness.record() == {"source": "project_override", "sha256": _sha("WORKER A"), "project_id": "proj-a"}
        assert resolve_harness("planner", SYSTEM, project=_project("proj-a")).record() == {
            "source": "system_default", "sha256": _sha("SYSTEM HARNESS")}

    def test_unknown_agent_types_are_refused(self) -> None:
        with pytest.raises(ValueError):
            resolve_harness("operator", SYSTEM)


# --- The stored defaults ---


class TestStoredDefaults:
    def test_missing_rows_are_the_builtin_defaults(self) -> None:
        assert defaults_from({}) == BUILTIN_DEFAULTS
        assert (BUILTIN_DEFAULTS.model, BUILTIN_DEFAULTS.effort, BUILTIN_DEFAULTS.env) == ("opus", "", ())
        assert DEFAULT_MODEL == "opus"

    def test_stored_rows_are_the_system_defaults(self) -> None:
        env = json.dumps([{"key": "LANG", "value": "C.UTF-8"}, {"key": "TZ", "value": "UTC"}])
        defaults = defaults_from({HARNESS_KEY: "Mine.", MODEL_KEY: "sonnet", EFFORT_KEY: "high", ENV_KEY: env})
        assert defaults == AgentDefaults("Mine.", HarnessSource.SYSTEM_DEFAULT, "sonnet", ModelSource.SYSTEM_DEFAULT,
                                         "high", (("LANG", "C.UTF-8"), ("TZ", "UTC")))

    def test_a_stored_copy_of_the_builtin_harness_counts_as_builtin(self) -> None:
        assert defaults_from({HARNESS_KEY: DEFAULT_HARNESS_PROMPT + "\n"}).harness_source is HarnessSource.BUILTIN_DEFAULT

    def test_an_unusable_row_is_ignored(self) -> None:
        defaults = defaults_from({MODEL_KEY: "--dangerously-skip-permissions", EFFORT_KEY: "extreme",
                                  ENV_KEY: "not json"})
        assert (defaults.model, defaults.model_source, defaults.effort, defaults.env) == (
            "opus", ModelSource.BUILTIN_DEFAULT, "", ())

    def test_an_unreadable_table_falls_back_to_the_builtin_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from loopx.db import system_settings

        async def _down(keys: Any) -> dict[str, str]:
            raise ConnectionError("database is down")

        monkeypatch.setattr(system_settings, "get_settings", _down)
        assert asyncio.run(agent_settings.load_defaults()) == BUILTIN_DEFAULTS


# --- Model, effort and environment of a run ---


class TestLaunch:
    def test_the_default_model_reaches_the_flags(self) -> None:
        launch = resolve_launch(BUILTIN_DEFAULTS)
        assert launch.args() == ["--model", "opus"]
        assert launch.record() == {"model_choice": {"model": "opus", "source": "builtin_default", "effort": None}}
        assert launch.environment() is None and launch.env_sha256 is None

    @pytest.mark.parametrize("override", [None, "", "   "])
    def test_no_override_uses_the_settings_default(self, override: Any) -> None:
        launch = resolve_launch(SYSTEM, override)
        assert (launch.model, launch.model_source) == ("sonnet", ModelSource.SYSTEM_DEFAULT)

    def test_an_agent_override_beats_the_settings_default(self) -> None:
        launch = resolve_launch(SYSTEM, " claude-haiku-4-5 ")
        assert (launch.model, launch.model_source) == ("claude-haiku-4-5", ModelSource.AGENT_OVERRIDE)
        assert launch.args() == ["--model", "claude-haiku-4-5"]

    def test_the_effort_follows_the_model(self) -> None:
        defaults = AgentDefaults("H", HarnessSource.SYSTEM_DEFAULT, "opus", ModelSource.SYSTEM_DEFAULT, "high")
        launch = resolve_launch(defaults, "sonnet")
        assert launch.args() == ["--model", "sonnet", "--effort", "high"]
        assert launch.record()["model_choice"] == {"model": "sonnet", "source": "agent_override", "effort": "high"}

    def test_the_environment_goes_on_top_of_the_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOPX_SERVER_ONLY", "kept")
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        defaults = AgentDefaults("H", HarnessSource.SYSTEM_DEFAULT, "opus", ModelSource.SYSTEM_DEFAULT, "",
                                 (("TZ", "UTC"), ("LANG", "C.UTF-8")))
        launch = resolve_launch(defaults)
        env = launch.environment()
        assert env is not None
        assert (env["LOOPX_SERVER_ONLY"], env["TZ"], env["LANG"]) == ("kept", "UTC", "C.UTF-8")
        assert os.environ["TZ"] == "Asia/Shanghai"  # the server's own environment is untouched
        # The record names the variables and hashes their values; it never holds a value.
        record = launch.record()
        assert record["env"] == {"sha256": launch.env_sha256, "keys": {"TZ": _sha("UTC"), "LANG": _sha("C.UTF-8")}}
        assert "C.UTF-8" not in json.dumps(record)
        changed = resolve_launch(AgentDefaults("H", HarnessSource.SYSTEM_DEFAULT, "opus", ModelSource.SYSTEM_DEFAULT,
                                               "", (("TZ", "UTC"), ("LANG", "en_US.UTF-8"))))
        assert changed.env_sha256 != launch.env_sha256


# --- Validation ---


class TestValidation:
    def test_a_harness_has_at_most_20000_characters(self) -> None:
        assert agent_settings.HARNESS_MAX_CHARS == 20_000
        assert agent_settings.harness_prompt("\n\n" + "x" * 20_000 + "\n  \n") == "x" * 20_000
        with pytest.raises(SettingsError, match="at most 20000"):
            agent_settings.harness_prompt("x" * 20_001)
        with pytest.raises(SettingsError, match="string"):
            agent_settings.harness_prompt(3)

    @pytest.mark.parametrize("name", ["opus", "sonnet", "claude-opus-4-5-20251101", "opus[1m]", "us.anthropic.x:0"])
    def test_model_names(self, name: str) -> None:
        assert agent_settings.model_name(f" {name} ") == name

    @pytest.mark.parametrize("name", ["--dangerously-skip-permissions", "-x", "two words", "a" * 201, 5])
    def test_bad_model_names(self, name: Any) -> None:
        with pytest.raises(SettingsError):
            agent_settings.model_name(name)

    def test_effort_levels(self) -> None:
        assert agent_settings.EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")
        assert agent_settings.effort_level("") == "" and agent_settings.effort_level(None) == ""
        with pytest.raises(SettingsError, match="one of"):
            agent_settings.effort_level("extreme")

    def test_environment_entries(self) -> None:
        env = agent_settings.environment([{"key": "LANG", "value": "C.UTF-8"}, {"key": "_X1", "value": ""}])
        assert env == {"LANG": "C.UTF-8", "_X1": ""}

    @pytest.mark.parametrize("key", ["lang", "1LANG", "LANG-X", "LANG X", ""])
    def test_environment_names_must_be_upper_snake_case(self, key: str) -> None:
        with pytest.raises(SettingsError, match=r"\[A-Z_\]"):
            agent_settings.environment([{"key": key, "value": "v"}])

    @pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "GH_TOKEN", "CLIENT_SECRET", "DB_PASSWORD",
                                     "AWS_CREDENTIALS", "KEY"])
    def test_credential_like_names_are_refused(self, key: str) -> None:
        with pytest.raises(SettingsError, match="secrets do not belong"):
            agent_settings.environment([{"key": key, "value": "v"}])

    def test_at_most_50_entries(self) -> None:
        entries = [{"key": f"V{i}", "value": "x"} for i in range(50)]
        assert len(agent_settings.environment(entries)) == 50
        with pytest.raises(SettingsError, match="at most 50"):
            agent_settings.environment([*entries, {"key": "V50", "value": "x"}])

    @pytest.mark.parametrize("raw", [
        {"LANG": "C"}, [["LANG", "C"]], [{"key": "LANG"}], [{"key": "LANG", "value": 1}],
        [{"key": "LANG", "value": "a\x00b"}], [{"key": "LANG", "value": "x", "extra": 1}],
        [{"key": "LANG", "value": "a"}, {"key": "LANG", "value": "b"}],
    ])
    def test_malformed_environments(self, raw: Any) -> None:
        with pytest.raises(SettingsError):
            agent_settings.environment(raw)


# --- The settings API ---


class _Settings:
    """An in-memory stand-in for the system_settings table."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.writes = 0

    async def get_settings(self, keys: Any, **_: Any) -> dict[str, str]:
        return {key: value for key, value in self.rows.items() if key in keys}

    async def put_settings(self, values: dict[str, str | None], **_: Any) -> None:
        self.writes += 1
        for key, value in values.items():
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value


@pytest.fixture
def settings_table(monkeypatch: pytest.MonkeyPatch) -> _Settings:
    from loopx.db import system_settings

    table = _Settings()
    monkeypatch.setattr(system_settings, "get_settings", table.get_settings)
    monkeypatch.setattr(system_settings, "put_settings", table.put_settings)
    return table


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    import tempfile
    from pathlib import Path

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=TaskRouter(runtime_root=Path(tempfile.mkdtemp())))
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    writer = _Writer()
    asyncio.run(server._route_api(method, path, {}, raw, writer))
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


class TestSettingsApi:
    def test_get_shows_the_builtin_defaults(self, settings_table: _Settings) -> None:
        status, body = _call("GET", "/api/settings/agents")
        assert status == 200
        assert body["default_harness_prompt"] == DEFAULT_HARNESS_PROMPT == body["builtin_harness_prompt"]
        assert (body["harness_source"], body["harness_max_chars"]) == ("builtin_default", 20_000)
        assert (body["default_model"], body["model_source"], body["default_effort"]) == ("opus", "builtin_default", "")
        assert body["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
        assert (body["default_env"], body["env_max_entries"]) == ([], 50)

    def test_put_stores_each_given_field(self, settings_table: _Settings) -> None:
        env = [{"key": "TZ", "value": "UTC"}, {"key": "LANG", "value": "C.UTF-8"}]
        status, body = _call("PUT", "/api/settings/agents", {
            "default_harness_prompt": "\nBe careful.\n", "default_model": "sonnet", "default_effort": "high",
            "default_env": env,
        })
        assert status == 200
        assert (body["default_harness_prompt"], body["harness_source"]) == ("Be careful.", "system_default")
        assert (body["default_model"], body["model_source"], body["default_effort"]) == ("sonnet", "system_default",
                                                                                        "high")
        assert body["default_env"] == env
        assert settings_table.rows[HARNESS_KEY] == "Be careful."
        # A partial PUT keeps the other fields.
        status, body = _call("PUT", "/api/settings/agents", {"default_effort": ""})
        assert (status, body["default_effort"], body["default_model"]) == (200, "", "sonnet")
        assert EFFORT_KEY not in settings_table.rows

    @pytest.mark.parametrize("field", ["default_harness_prompt", "default_model", "default_env"])
    def test_an_empty_value_goes_back_to_the_builtin_default(self, settings_table: _Settings, field: str) -> None:
        _call("PUT", "/api/settings/agents", {"default_harness_prompt": "Mine.", "default_model": "sonnet",
                                              "default_env": [{"key": "TZ", "value": "UTC"}]})
        status, body = _call("PUT", "/api/settings/agents", {field: [] if field == "default_env" else ""})
        assert status == 200
        expected = {"default_harness_prompt": DEFAULT_HARNESS_PROMPT, "default_model": "opus", "default_env": []}
        assert body[field] == expected[field]
        key = {"default_harness_prompt": HARNESS_KEY, "default_model": MODEL_KEY, "default_env": ENV_KEY}[field]
        assert set(settings_table.rows) == {HARNESS_KEY, MODEL_KEY, ENV_KEY} - {key}  # its row is gone

    def test_saving_the_builtin_text_keeps_following_the_builtin(self, settings_table: _Settings) -> None:
        status, body = _call("PUT", "/api/settings/agents", {"default_harness_prompt": DEFAULT_HARNESS_PROMPT})
        assert (status, body["harness_source"]) == (200, "builtin_default")
        assert HARNESS_KEY not in settings_table.rows

    def test_reset_restores_the_builtin_harness(self, settings_table: _Settings) -> None:
        _call("PUT", "/api/settings/agents", {"default_harness_prompt": "Mine.", "default_model": "sonnet"})
        status, body = _call("POST", "/api/settings/agents/reset-harness")
        assert status == 200
        assert (body["default_harness_prompt"], body["harness_source"]) == (DEFAULT_HARNESS_PROMPT, "builtin_default")
        assert body["default_model"] == "sonnet"  # only the harness is reset

    @pytest.mark.parametrize(("body", "message"), [
        ({"default_harness_prompt": "x" * 20_001}, "at most 20000"),
        ({"default_model": "--resume"}, "not a model name"),
        ({"default_effort": "extreme"}, "one of"),
        ({"default_env": [{"key": "GITHUB_TOKEN", "value": "x"}]}, "secrets do not belong"),
        ({"default_env": [{"key": "lang", "value": "x"}]}, "A-Z"),
        ({"default_env": [{"key": f"V{i}", "value": "x"} for i in range(51)]}, "at most 50"),
        ({"default_harness": "typo"}, "unknown field"),
        ({}, "at least one"),
        ([1], "JSON object"),
        (b"not json", "invalid JSON"),
    ])
    def test_refused_values_are_400_and_change_nothing(
        self, settings_table: _Settings, body: Any, message: str,
    ) -> None:
        status, answer = _call("PUT", "/api/settings/agents", body)
        assert status == 400 and message in answer["error"]
        assert settings_table.rows == {} and settings_table.writes == 0

    def test_one_refused_field_refuses_the_valid_ones_too(self, settings_table: _Settings) -> None:
        status, _ = _call("PUT", "/api/settings/agents", {"default_model": "sonnet", "default_effort": "extreme"})
        assert status == 400 and settings_table.writes == 0


# --- The Manager's harness override on its agent config ---


class _Configs:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def set_config(self, agent_id: str, fields: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.updates.append((agent_id, fields))
        return {"agent_id": agent_id, **fields}


@pytest.fixture
def configs(monkeypatch: pytest.MonkeyPatch) -> _Configs:
    from loopx.db import agent_configs

    table = _Configs()
    monkeypatch.setattr(agent_configs, "set_config", table.set_config)
    return table


class TestAgentConfigHarness:
    def test_the_manager_takes_a_harness_override(self, configs: _Configs) -> None:
        status, body = _call("PUT", "/api/agent-configs/manager", {"harness_prompt": "\n\nBe brief.\n"})
        assert status == 200
        assert configs.updates == [("manager", {"harness_prompt": "Be brief."})]
        status, _ = _call("PUT", "/api/agent-configs/manager", {"harness_prompt": ""})  # back to the default
        assert status == 200 and configs.updates[-1] == ("manager", {"harness_prompt": ""})

    @pytest.mark.parametrize("agent", ["planner", "worker", "reviewer"])
    def test_other_agents_refuse_a_harness_override(self, configs: _Configs, agent: str) -> None:
        status, body = _call("PUT", f"/api/agent-configs/{agent}", {"harness_prompt": "x", "system_prompt": "y"})
        assert status == 400
        assert "manager only" in body["error"] and "project" in body["error"]
        assert configs.updates == []

    def test_the_harness_override_is_capped(self, configs: _Configs) -> None:
        status, body = _call("PUT", "/api/agent-configs/manager", {"harness_prompt": "x" * 20_001})
        assert status == 400 and "20000" in body["error"] and configs.updates == []

    def test_a_model_is_checked_and_an_empty_one_clears_the_override(self, configs: _Configs) -> None:
        assert _call("PUT", "/api/agent-configs/worker", {"model": "-p"})[0] == 400
        assert _call("PUT", "/api/agent-configs/worker", {"model": " sonnet "})[0] == 200
        assert _call("PUT", "/api/agent-configs/worker", {"model": ""})[0] == 200
        assert [fields["model"] for _, fields in configs.updates] == ["sonnet", None]
