"""What every agent inherits: the harness prompt, the Agent Profile, the environment and the per-run cap.

Each resolves through system -> role -> project -> role in a project, a later
layer winning; the Manager has only the first two, and a task's choice of
profile for one of its roles wins over them all. The environment merges key by
key, and a Reviewer without a harness of its own at a role layer takes the
Workers' at that layer. A run's effort is its profile's: no layer sets one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import agent_settings
from looper.agents.profile_registry import BUILTIN_PROFILE, BUILTIN_PROVIDER, BUILTIN_RUNTIME, AgentProfile
from looper.agents.agent_settings import (
    BUILTIN_SETTINGS,
    DEFAULT_HARNESS_PROMPT,
    AgentType,
    Launch,
    Layer,
    LayerValues,
    SettingsError,
    index_layers,
    resolve,
)
from looper.agents.http_server import AgentHTTPServer
from looper.agents.runtimes.claude_code import AGENT_TEAMS, TEAMMATE_MODE_ARGS
from looper.agents.tiers.task import TaskRouter

ORDER = (Layer.SYSTEM, Layer.ROLE, Layer.PROJECT, Layer.ROLE_PROJECT)
TASK_ROLES = ("planner", "worker", "reviewer")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _place(layer: Layer, role: str) -> tuple[Layer, AgentType | None]:
    return layer, AgentType(role) if layer in (Layer.ROLE, Layer.ROLE_PROJECT, Layer.TASK) else None


def _profile(model: str, effort: str | None = None) -> AgentProfile:
    """A profile of the built-in runtime and provider with ``model``."""
    return AgentProfile(f"agent-{model}", f"claude-code/{model}", BUILTIN_RUNTIME, BUILTIN_PROVIDER, model, effort)


# A distinct value of each field at each layer.
VALUES: dict[str, dict[Layer, Any]] = {
    "harness_prompt": {layer: f"HARNESS {layer.value}" for layer in ORDER},
    "profile": {layer: _profile(f"model-{layer.value}") for layer in ORDER},
    "run_budget_usd": dict(zip(ORDER, (1.0, 2.0, 3.0, 4.0))),
    "env": {layer: (("SHARED", f"from {layer.value}"),) for layer in ORDER},
}


def _resolved(settings: agent_settings.AgentSettings, field: str) -> tuple[Any, Layer]:
    if field == "harness_prompt":
        return settings.harness.text, settings.harness.source
    if field == "profile":
        return settings.profile, settings.profile_source
    if field == "run_budget_usd":
        return settings.max_budget_usd, settings.max_budget_source
    return settings.env, dict(settings.env_sources).get("SHARED", Layer.BUILTIN)


def _row(profile: AgentProfile) -> dict[str, Any]:
    """``profile`` as the database gives it (``db.profile_registry.PROFILE_JSON``)."""
    return {"id": profile.id, "name": profile.name, "model": profile.model, "effort": profile.effort,
            "builtin": False, "runtime_id": profile.runtime.id, "runtime_name": profile.runtime.name,
            "runtime_type": profile.runtime.type, "runtime_executable": profile.runtime.executable,
            "runtime_capabilities": sorted(c.value for c in profile.runtime.capabilities),
            "provider_id": profile.provider.id, "provider_name": profile.provider.name,
            "provider_auth": profile.provider.auth.value,
            "provider_protocols": sorted(p.value for p in profile.provider.protocols),
            "provider_oauth_runtime_type": profile.provider.oauth_runtime_type, "provider_base_url": "",
            "provider_models": []}


# --- Resolution ---


class TestPrecedence:
    @pytest.mark.parametrize("role", TASK_ROLES)
    @pytest.mark.parametrize("upto", ORDER)
    @pytest.mark.parametrize("field", list(VALUES))
    def test_each_layer_wins_over_the_ones_before_and_an_unset_later_one_inherits(
        self, field: str, upto: Layer, role: str,
    ) -> None:
        layers = {_place(layer, role): LayerValues(**{field: VALUES[field][layer]})
                  for layer in ORDER[:ORDER.index(upto) + 1]}
        assert _resolved(resolve(role, layers, "proj-a"), field) == (VALUES[field][upto], upto)

    @pytest.mark.parametrize("field", list(VALUES))
    def test_the_manager_has_only_the_system_and_its_role_layer(self, field: str) -> None:
        layers = {_place(layer, "manager"): LayerValues(**{field: VALUES[field][layer]}) for layer in ORDER}
        assert _resolved(resolve("manager", layers, "proj-a"), field) == (VALUES[field][Layer.ROLE], Layer.ROLE)
        only_project = {(Layer.PROJECT, None): LayerValues(**{field: VALUES[field][Layer.PROJECT]})}
        assert _resolved(resolve("manager", only_project, "proj-a"), field)[1] is Layer.BUILTIN

    def test_nothing_set_is_the_builtin_default(self) -> None:
        for role in AgentType:
            assert resolve(role, {}, "proj-a") == BUILTIN_SETTINGS
        assert (BUILTIN_SETTINGS.harness.text, BUILTIN_SETTINGS.profile, BUILTIN_SETTINGS.model,
                BUILTIN_SETTINGS.effort) == (DEFAULT_HARNESS_PROMPT, BUILTIN_PROFILE, "opus", "")
        assert (BUILTIN_SETTINGS.env, BUILTIN_SETTINGS.max_budget_usd) == ((), None)

    def test_a_project_layer_counts_only_inside_its_project(self) -> None:
        rows = [{"scope": "project", "project_id": "proj-a", "profile": _row(_profile("a-model"))},
                {"scope": "role_project", "role": "worker", "project_id": "proj-a",
                 "profile": _row(_profile("a-worker"))}]
        assert resolve("worker", index_layers(rows, "proj-a"), "proj-a").model == "a-worker"
        assert resolve("worker", index_layers(rows, "proj-b"), "proj-b").model == "opus"
        assert resolve("worker", index_layers(rows, None)).model == "opus"

    @pytest.mark.parametrize("role", TASK_ROLES)
    def test_a_tasks_profile_wins_over_every_layer_for_its_role_only(self, role: str) -> None:
        layers = {_place(layer, role): LayerValues(profile=_profile(f"model-{layer.value}", effort="low"))
                  for layer in ORDER}
        layers[(Layer.TASK, AgentType(role))] = LayerValues(profile=_profile("task-model", effort="high"))
        chosen = resolve(role, layers, "proj-a")
        assert (chosen.model, chosen.profile_source, chosen.effort) == ("task-model", Layer.TASK, "high")
        for other in set(TASK_ROLES) - {role}:
            assert resolve(other, layers, "proj-a").profile_source is not Layer.TASK
        # The Manager spans every task.
        assert resolve("manager", {(Layer.TASK, AgentType.MANAGER): LayerValues(profile=_profile("x"))}).model == "opus"

    def test_a_runs_effort_is_its_profiles(self) -> None:
        layers = {(Layer.SYSTEM, None): LayerValues(profile=_profile("opus", effort="max")),
                  (Layer.ROLE, AgentType.WORKER): LayerValues(profile=_profile("sonnet", effort="low")),
                  (Layer.ROLE, AgentType.REVIEWER): LayerValues(profile=_profile("haiku"))}
        assert (resolve("worker", layers).effort, resolve("worker", layers).effort_source) == ("low", Layer.PROFILE)
        assert (resolve("planner", layers).effort, resolve("planner", layers).effort_source) == ("max", Layer.PROFILE)
        # A profile without an effort runs with the runtime's own, whatever the profiles above have.
        assert (resolve("reviewer", layers).effort, resolve("reviewer", layers).effort_source) == ("", Layer.BUILTIN)

    @pytest.mark.parametrize("layer", [Layer.ROLE, Layer.ROLE_PROJECT])
    def test_a_role_layer_reaches_only_its_role(self, layer: Layer) -> None:
        layers = {_place(layer, "worker"): LayerValues(profile=_profile("worker-only", effort="max"),
                                                       run_budget_usd=0.5, env=(("ONLY", "w"),))}
        worker = resolve("worker", layers, "proj-a")
        assert (worker.model, worker.effort, worker.max_budget_usd, worker.env) == (
            "worker-only", "max", 0.5, (("ONLY", "w"),))
        for other in ("manager", "planner", "reviewer"):
            assert resolve(other, layers, "proj-a").model == "opus"
            assert resolve(other, layers, "proj-a").env == ()

    @pytest.mark.parametrize("before_rename", [False, True], ids=["current", "before-the-rename"])
    def test_a_system_harness_equal_to_the_builtin_one_is_the_builtin(self, before_rename: bool) -> None:
        """Also the built-in as it read when the product was LoopX, so a stored copy follows the renamed one."""
        text = DEFAULT_HARNESS_PROMPT.replace("Looper", "LoopX") if before_rename else DEFAULT_HARNESS_PROMPT
        assert "LoopX" in text if before_rename else "Looper" in text
        layers = {(Layer.SYSTEM, None): LayerValues(harness_prompt=text)}
        assert resolve("worker", layers).harness.source is Layer.BUILTIN
        assert resolve("worker", layers).harness.text == DEFAULT_HARNESS_PROMPT


class TestEnvironment:
    def test_layers_add_override_and_unset_keys_in_order(self) -> None:
        layers = {
            (Layer.SYSTEM, None): LayerValues(env=(("TZ", "UTC"), ("LANG", "C"), ("GONE", "x"))),
            (Layer.ROLE, AgentType.WORKER): LayerValues(env=(("LANG", "C.UTF-8"), ("ROLE_ONLY", "1"))),
            (Layer.PROJECT, None): LayerValues(env=(("GONE", None), ("NEVER_SET", None), ("PROJ", "p"))),
            (Layer.ROLE_PROJECT, AgentType.WORKER): LayerValues(env=(("ROLE_ONLY", None), ("GONE", "back"))),
        }
        worker = resolve("worker", layers, "proj-a")
        assert worker.env == (("TZ", "UTC"), ("LANG", "C.UTF-8"), ("PROJ", "p"), ("GONE", "back"))
        assert dict(worker.env_sources) == {"TZ": Layer.SYSTEM, "LANG": Layer.ROLE, "PROJ": Layer.PROJECT,
                                            "GONE": Layer.ROLE_PROJECT}
        # The Planner of the project has no Worker layers: the system's, minus what the project unsets.
        planner = resolve("planner", layers, "proj-a")
        assert planner.env == (("TZ", "UTC"), ("LANG", "C"), ("PROJ", "p"))

    def test_the_variables_go_on_top_of_the_servers_and_the_record_holds_no_value(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LOOPER_SERVER_ONLY", "kept")
        monkeypatch.setenv("TZ", "Asia/Shanghai")
        launch = Launch(resolve("worker", {(Layer.SYSTEM, None): LayerValues(env=(("TZ", "UTC"), ("LANG", "C.UTF-8")))}))
        env = launch.environment()
        assert env is not None and (env["LOOPER_SERVER_ONLY"], env["TZ"], env["LANG"]) == ("kept", "UTC", "C.UTF-8")
        assert os.environ["TZ"] == "Asia/Shanghai"
        record = launch.record()
        assert record["env"] == {"sha256": launch.env_sha256, "keys": {"TZ": _sha("UTC"), "LANG": _sha("C.UTF-8")}}
        assert "C.UTF-8" not in json.dumps(record)
        # Nothing to change for a profile without agent teams, on a server without their variable; the built-in
        # profile has them.
        monkeypatch.delenv(AGENT_TEAMS, raising=False)
        plain = Launch(resolve("worker", {(Layer.SYSTEM, None): LayerValues(profile=_profile("opus"))}))
        assert plain.environment() is None and plain.env_sha256 is None
        assert Launch().environment() == {**os.environ, AGENT_TEAMS: "1"}


class TestReviewerHarness:
    @pytest.mark.parametrize("layer", [Layer.ROLE, Layer.ROLE_PROJECT])
    def test_a_reviewer_without_its_own_follows_the_workers_at_that_layer(self, layer: Layer) -> None:
        layers = {(Layer.SYSTEM, None): LayerValues(harness_prompt="SYSTEM"),
                  _place(layer, "worker"): LayerValues(harness_prompt="WORKER", profile=_profile("worker-model")),
                  _place(layer, "planner"): LayerValues(harness_prompt="PLANNER")}
        reviewer = resolve("reviewer", layers, "proj-a")
        assert (reviewer.harness.text, reviewer.harness.source, reviewer.harness.from_role) == (
            "WORKER", layer, AgentType.WORKER)
        assert reviewer.model == "opus"  # only the harness follows the Workers
        assert reviewer.harness.record()["from_role"] == "worker"
        # Its own harness at that layer wins, and a Planner's is never taken.
        layers[_place(layer, "reviewer")] = LayerValues(harness_prompt="REVIEWER")
        own = resolve("reviewer", layers, "proj-a").harness
        assert (own.text, own.from_role) == ("REVIEWER", None)
        del layers[_place(layer, "worker")]
        del layers[_place(layer, "reviewer")]
        assert resolve("reviewer", layers, "proj-a").harness.text == "SYSTEM"

    def test_a_later_layer_beats_the_workers_harness_at_an_earlier_one(self) -> None:
        layers = {(Layer.ROLE, AgentType.WORKER): LayerValues(harness_prompt="WORKER ROLE"),
                  (Layer.PROJECT, None): LayerValues(harness_prompt="PROJECT")}
        assert resolve("reviewer", layers, "proj-a").harness.source is Layer.PROJECT


class TestRecord:
    def test_a_run_records_each_settings_layer(self) -> None:
        layers = {(Layer.SYSTEM, None): LayerValues(env=(("TZ", "UTC"),)),
                  (Layer.ROLE, AgentType.WORKER): LayerValues(profile=_profile("sonnet", effort="high")),
                  (Layer.PROJECT, None): LayerValues(run_budget_usd=0.5),
                  (Layer.ROLE_PROJECT, AgentType.WORKER): LayerValues(harness_prompt="H", env=(("LANG", "C"),))}
        settings = resolve("worker", layers, "proj-a")
        launch = Launch(settings)
        record = launch.record()
        assert record["model_choice"] == {"model": "sonnet", "source": "role", "effort": "high"}
        assert record["profile"] == {"id": "agent-sonnet", "name": "claude-code/sonnet", "model": "sonnet",
                                     "runtime": {"id": "rt-claude-code", "name": "claude-code", "type": "claude-code"},
                                     "provider": {"id": "prov-claude-subscription", "name": "claude-subscription",
                                                  "auth": "oauth"}, "source": "role"}
        assert record["settings_sources"] == {"harness": "role_project", "profile": "role", "effort": "profile",
                                              "env": {"TZ": "system", "LANG": "role_project"},
                                              "run_budget_usd": "project"}
        assert record["max_budget_usd"] == 0.5
        assert settings.harness.record() == {"source": "role_project", "sha256": _sha("H"), "project_id": "proj-a"}
        from looper.agents.claude_cli import permission_args
        assert launch.args() == [*permission_args(), "--model", "sonnet", "--effort", "high",
                                 "--max-budget-usd", "0.5"]

    def test_the_builtin_launch(self) -> None:
        from looper.agents.claude_cli import permission_args

        assert Launch().args() == [*permission_args(), "--model", "opus", *TEAMMATE_MODE_ARGS]  # it has agent teams
        assert Launch().record() == {
            "profile": {**BUILTIN_PROFILE.record(), "source": "builtin"},
            "model_choice": {"model": "opus", "source": "builtin", "effort": None},
            "settings_sources": {"harness": "builtin", "profile": "builtin", "effort": "builtin", "env": {},
                                 "run_budget_usd": "builtin"},
        }


class TestStoredRows:
    def test_an_unusable_stored_value_is_ignored(self) -> None:
        values = LayerValues.from_row({
            "scope": "system", "harness_prompt": "\n\n  Mine.\n", "profile": {"id": "agent-x"},
            "env": [{"key": "lang", "value": "x"}], "run_budget_usd": -1,
        })
        assert values == LayerValues(harness_prompt="  Mine.")

    def test_a_null_env_value_unsets_except_at_the_system_layer(self) -> None:
        row = {"env": [{"key": "TZ", "value": None}]}
        assert LayerValues.from_row({**row, "scope": "role", "role": "worker"}).env == (("TZ", None),)
        assert LayerValues.from_row({**row, "scope": "system"}).env == ()

    def test_unreadable_layers_give_the_builtin_settings_with_the_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from looper.db import agent_setting_layers

        async def _down(*_: Any, **__: Any) -> list[dict[str, Any]]:
            raise ConnectionError("database is down")

        monkeypatch.setattr(agent_setting_layers, "list_layers", _down)
        settings = asyncio.run(agent_settings.load_agent_settings("worker", "proj-a"))
        assert settings == BUILTIN_SETTINGS and settings.error and "ConnectionError" in settings.error


# --- Validation ---


class TestValidation:
    @pytest.mark.parametrize("name", ["opus", "sonnet", "claude-opus-4-5-20251101", "opus[1m]", "us.anthropic.x:0"])
    def test_model_names(self, name: str) -> None:
        assert agent_settings.model_name(f" {name} ") == name

    @pytest.mark.parametrize("name", ["--dangerously-skip-permissions", "-x", "two words", "a" * 201, 5])
    def test_bad_model_names(self, name: Any) -> None:
        with pytest.raises(SettingsError):
            agent_settings.model_name(name)

    @pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "GH_TOKEN", "CLIENT_SECRET", "DB_PASSWORD",
                                     "AWS_CREDENTIALS", "KEY"])
    def test_credential_like_names_are_refused(self, key: str) -> None:
        with pytest.raises(SettingsError, match="secrets do not belong"):
            agent_settings.environment([{"key": key, "value": "v"}])

    @pytest.mark.parametrize(("scope", "role"), [(Layer.SYSTEM, None), (Layer.ROLE, AgentType.WORKER),
                                                 (Layer.PROJECT, None), (Layer.ROLE_PROJECT, AgentType.PLANNER)])
    def test_the_agent_teams_variable_is_refused_in_every_layer_but_a_stored_one_still_loads(
        self, scope: Layer, role: AgentType | None,
    ) -> None:
        env = [{"key": "LANG", "value": "C"}, {"key": "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "value": "1"}]
        with pytest.raises(SettingsError, match="Agent Profile's agent teams switch"):
            agent_settings.layer_update({"env": env}, scope, role)
        # A layer stored before the switch keeps its other variables; a run removes this one (runtimes.claude_code).
        stored = LayerValues.from_row({"scope": scope.value, "env": env})
        assert stored.env == (("LANG", "C"), ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "1"))

    @pytest.mark.parametrize("raw", [
        {"LANG": "C"}, [["LANG", "C"]], [{"key": "LANG"}], [{"key": "LANG", "value": 1}],
        [{"key": "LANG", "value": "a\x00b"}], [{"key": "LANG", "value": "x", "extra": 1}],
        [{"key": "LANG", "value": "a"}, {"key": "LANG", "value": "b"}], [{"key": "LANG", "value": None}],
    ])
    def test_malformed_environments(self, raw: Any) -> None:
        with pytest.raises(SettingsError):
            agent_settings.environment(raw)


# --- The settings API (an in-memory layer table) ---


# The registered profiles of the stand-in table, by id.
PROFILES = {profile.id: profile for profile in (BUILTIN_PROFILE, _profile("sonnet", effort="high"), _profile("haiku"),
                                                _profile("opus[1m]"))}


class _Layers:
    """An in-memory stand-in for ``agent_setting_layers``, with one project, ``proj-a``, and ``PROFILES``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
        self.writes = 0

    async def list_layers(self, project_id: str | None = None, **_: Any) -> list[dict[str, Any]]:
        return [{"scope": s, "role": r, "project_id": p, **values,
                 "profile": _row(PROFILES[values["profile_id"]]) if values["profile_id"] else None}
                for (s, r, p), values in self.rows.items() if p in (None, project_id)]

    async def put_layer(self, scope: str, role: str | None, project_id: str | None, values: dict[str, Any],
                        **_: Any) -> None:
        self.writes += 1
        row = self.rows.setdefault((scope, role, project_id), {
            "harness_prompt": None, "profile_id": None, "env": [], "run_budget_usd": None, "max_concurrency": None})
        row.update({k: (list(v or []) if k == "env" else v) for k, v in values.items()})
        if not any(row.values()):
            del self.rows[(scope, role, project_id)]


@pytest.fixture
def layers(monkeypatch: pytest.MonkeyPatch) -> _Layers:
    from looper.db import agent_setting_layers, profile_registry, projects

    table = _Layers()
    monkeypatch.setattr(agent_setting_layers, "list_layers", table.list_layers)
    monkeypatch.setattr(agent_setting_layers, "put_layer", table.put_layer)

    async def get_profile(profile_id: str, **_: Any) -> dict[str, Any] | None:
        return _row(PROFILES[profile_id]) if profile_id in PROFILES else None

    monkeypatch.setattr(profile_registry, "get_profile", get_profile)

    async def get_project(project_id: str, **_: Any) -> dict[str, Any] | None:
        return {"id": project_id} if project_id == "proj-a" else None

    monkeypatch.setattr(projects, "get_project", get_project)
    return table


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


def _call(method: str, path: str, body: Any = None, *, router: Any = None, pool: Any = None) -> tuple[int, Any]:
    import tempfile
    from pathlib import Path

    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=router or TaskRouter(runtime_root=Path(tempfile.mkdtemp())), worker_pool=pool)
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    writer = _Writer()
    asyncio.run(server._route_api(method, path, {}, raw, writer))
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


UNSET = {"harness_prompt": None, "profile_id": None, "env": [], "run_budget_usd": None, "max_concurrency": None}


def _now(profile: AgentProfile, source: str) -> dict[str, Any]:
    """An effective view's profile."""
    return {"value": {"profile_id": profile.id, "name": profile.name, "model": profile.model, "runtime": "claude-code",
                      "provider": "claude-subscription"}, "source": source}


class TestSettingsApi:
    def test_get_shows_the_builtin_defaults(self, layers: _Layers) -> None:
        status, body = _call("GET", "/api/agent-settings")
        assert status == 200
        assert body["limits"]["builtin_harness_prompt"] == DEFAULT_HARNESS_PROMPT
        assert body["system"]["values"] == UNSET and set(body["roles"]) == {r.value for r in AgentType}
        for view in (body["system"], *body["roles"].values()):
            effective = view["effective"]
            assert effective["harness_prompt"] == {"value": DEFAULT_HARNESS_PROMPT, "source": "builtin"}
            assert effective["profile"] == _now(BUILTIN_PROFILE, "builtin")
            assert (effective["effort"], effective["env"]) == ({"value": None, "source": "builtin"}, [])

    def test_each_page_edits_its_layer_and_shows_what_applies(self, layers: _Layers) -> None:
        sonnet, haiku, opus_1m = PROFILES["agent-sonnet"], PROFILES["agent-haiku"], PROFILES["agent-opus[1m]"]
        status, body = _call("PUT", "/api/agent-settings/system", {
            "harness_prompt": "\nBe careful.\n", "profile_id": sonnet.id, "env": [{"key": "TZ", "value": "UTC"}]})
        assert status == 200 and body["system"]["values"] == {
            "harness_prompt": "Be careful.", "profile_id": sonnet.id, "env": [{"key": "TZ", "value": "UTC"}],
            "run_budget_usd": None, "max_concurrency": None}
        status, body = _call("PUT", "/api/agent-settings/roles/worker", {"profile_id": haiku.id, "run_budget_usd": 2})
        assert status == 200
        worker = body["roles"]["worker"]["effective"]
        assert (worker["profile"], worker["run_budget_usd"]) == (_now(haiku, "role"), {"value": 2.0, "source": "role"})
        assert worker["harness_prompt"] == {"value": "Be careful.", "source": "system"}
        assert worker["effort"] == {"value": None, "source": "builtin"}  # haiku's: none
        assert body["roles"]["planner"]["effective"]["profile"] == _now(sonnet, "system")
        assert body["roles"]["planner"]["effective"]["effort"] == {"value": "high", "source": "profile"}

        status, body = _call("PUT", "/api/projects/proj-a/agent-settings", {"profile_id": opus_1m.id, "env": [
            {"key": "TZ", "value": None}]})
        assert status == 200 and body["project"]["values"]["profile_id"] == opus_1m.id
        status, body = _call("PUT", "/api/projects/proj-a/agent-settings/roles/worker", {"harness_prompt": "W."})
        assert status == 200 and body["roles"]["worker"]["values"]["harness_prompt"] == "W."
        planner, worker, reviewer = (body["roles"][r]["effective"] for r in TASK_ROLES)
        assert planner["profile"] == _now(opus_1m, "project") and planner["env"] == []
        assert worker["harness_prompt"] == {"value": "W.", "source": "role_project"}
        assert reviewer["harness_prompt"] == {"value": "W.", "source": "role_project", "from_role": "worker"}
        assert _call("GET", "/api/projects/proj-a/agent-settings")[1] == body

    def test_an_empty_value_or_the_builtin_text_unsets(self, layers: _Layers) -> None:
        _call("PUT", "/api/agent-settings/system", {"harness_prompt": "Mine.", "profile_id": "agent-sonnet",
                                                    "run_budget_usd": 1})
        status, body = _call("PUT", "/api/agent-settings/system", {"profile_id": "", "run_budget_usd": None,
                                                                   "harness_prompt": DEFAULT_HARNESS_PROMPT})
        assert status == 200 and body["system"]["values"] == UNSET
        assert layers.rows == {}  # a layer with nothing set has no row
        # At the role layer, the built-in text is a value of its own.
        _call("PUT", "/api/agent-settings/roles/manager", {"harness_prompt": DEFAULT_HARNESS_PROMPT})
        assert layers.rows[("role", "manager", None)]["harness_prompt"] == DEFAULT_HARNESS_PROMPT

    @pytest.mark.parametrize(("path", "body", "status", "message"), [
        ("/api/agent-settings/system", {"harness_prompt": "x" * 20_001}, 400, "at most 20000"),
        ("/api/agent-settings/system", {"model": "sonnet"}, 400, "part of the Agent Profile"),
        ("/api/agent-settings/system", {"profile_id": "agent-nowhere"}, 400, "no Agent Profile 'agent-nowhere'"),
        ("/api/agent-settings/system", {"profile_id": 5}, 400, "profile_id must be the id"),
        # The effort is the Agent Profile's: a layer refuses one, valid or not, with or without a profile.
        ("/api/agent-settings/system", {"effort": "high"}, 400, "effort is part of the Agent Profile"),
        ("/api/agent-settings/roles/worker", {"effort": None}, 400, "effort is part of the Agent Profile"),
        ("/api/projects/proj-a/agent-settings/roles/worker", {"effort": "low"}, 400,
         "effort is part of the Agent Profile"),
        ("/api/agent-settings/system", {"env": [{"key": "GITHUB_TOKEN", "value": "x"}]}, 400, "secrets"),
        ("/api/agent-settings/system", {"env": [{"key": "TZ", "value": None}]}, 400, "at most 4000"),
        ("/api/agent-settings/system", {"run_budget_usd": 0}, 400, "more than 0"),
        ("/api/agent-settings/system", {"profile_id": "agent-sonnet", "effort": "high"}, 400,
         "choose or create a profile with the effort you want"),
        ("/api/agent-settings/system", {"default_model": "sonnet"}, 400, "unknown field"),
        ("/api/agent-settings/system", {}, 400, "at least one"),
        ("/api/agent-settings/system", b"not json", 400, "invalid JSON"),
        ("/api/agent-settings/roles/boss", {"profile_id": "agent-sonnet"}, 404, "no role"),
        ("/api/projects/proj-a/agent-settings/roles/manager", {"profile_id": "agent-sonnet"}, 400, "spans every project"),
        ("/api/projects/proj-zzz/agent-settings", {"profile_id": "agent-sonnet"}, 404, "not found"),
        ("/api/agent-settings/system", {"max_concurrency": 2}, 400, "planner, worker or reviewer role layer"),
        ("/api/agent-settings/roles/manager", {"max_concurrency": 2}, 400, "planner, worker or reviewer role layer"),
        ("/api/projects/proj-a/agent-settings", {"max_concurrency": 2}, 400, "planner, worker or reviewer role layer"),
        ("/api/agent-settings/roles/worker", {"max_concurrency": 0}, 400, "from 1 to 64"),
        ("/api/agent-settings/roles/worker", {"max_concurrency": 65}, 400, "from 1 to 64"),
        ("/api/agent-settings/roles/worker", {"max_concurrency": 2.5}, 400, "from 1 to 64"),
        ("/api/agent-settings/roles/worker", {"max_concurrency": "3"}, 400, "from 1 to 64"),
        ("/api/agent-settings/roles/worker", {"max_concurrency": True}, 400, "from 1 to 64"),
        ("/api/projects/proj-a/agent-settings/roles/planner", {"max_concurrency": 99}, 400, "from 1 to 64"),
    ])
    def test_refused_updates_change_nothing(
        self, layers: _Layers, path: str, body: Any, status: int, message: str,
    ) -> None:
        answer_status, answer = _call("PUT", path, body)
        assert answer_status == status and message in answer["error"]
        assert layers.writes == 0
        assert _call("GET", "/api/projects/proj-zzz/agent-settings")[0] == 404


class TestConcurrencyLimits:
    """``max_concurrency``: how many of a task role run at once in one project, per role and per role in a
    project."""

    def test_each_task_role_has_a_builtin_limit_a_role_and_a_project_override_it(self, layers: _Layers) -> None:
        body = _call("GET", "/api/agent-settings")[1]
        assert {role: body["roles"][role]["effective"]["max_concurrency"] for role in TASK_ROLES} == {
            "planner": {"value": 8, "source": "builtin"}, "worker": {"value": 16, "source": "builtin"},
            "reviewer": {"value": 8, "source": "builtin"}}
        assert "max_concurrency" not in body["roles"]["manager"]["effective"]
        assert "max_concurrency" not in body["system"]["effective"]

        status, body = _call("PUT", "/api/agent-settings/roles/worker", {"max_concurrency": 3})
        assert status == 200 and body["roles"]["worker"]["values"]["max_concurrency"] == 3
        assert body["roles"]["worker"]["effective"]["max_concurrency"] == {"value": 3, "source": "role"}
        status, body = _call("PUT", "/api/projects/proj-a/agent-settings/roles/worker", {"max_concurrency": 5})
        assert status == 200 and body["roles"]["worker"]["effective"]["max_concurrency"] == {
            "value": 5, "source": "role_project"}
        assert body["roles"]["reviewer"]["effective"]["max_concurrency"] == {"value": 8, "source": "builtin"}
        # Cleared, the project follows the role layer again.
        body = _call("PUT", "/api/projects/proj-a/agent-settings/roles/worker", {"max_concurrency": None})[1]
        assert body["roles"]["worker"]["effective"]["max_concurrency"] == {"value": 3, "source": "role"}
        assert ("role_project", "worker", "proj-a") not in layers.rows

    @pytest.mark.parametrize(("path", "body", "rechecked"), [
        ("/api/agent-settings/roles/worker", {"max_concurrency": 4}, True),
        ("/api/projects/proj-a/agent-settings/roles/planner", {"max_concurrency": None}, True),
        ("/api/agent-settings/roles/worker", {"run_budget_usd": 1}, False),
        ("/api/agent-settings/roles/worker", {"max_concurrency": 0}, False),  # refused: nothing changed
    ])
    def test_a_changed_limit_tells_the_router_and_the_pool_to_start_what_it_allows(
        self, layers: _Layers, path: str, body: dict[str, Any], rechecked: bool,
    ) -> None:
        from unittest.mock import AsyncMock

        router, pool = SimpleNamespace(recheck=AsyncMock()), SimpleNamespace(recheck=AsyncMock())
        _call("PUT", path, body, router=router, pool=pool)
        assert (router.recheck.await_count, pool.recheck.await_count) == ((1, 1) if rechecked else (0, 0))


class TestMovedFields:
    @pytest.mark.parametrize("field", ["harness_prompt", "model", "max_concurrency"])
    def test_a_role_config_refuses_its_old_settings(self, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
        from looper.db import role_configs

        updates: list[Any] = []

        async def set_config(*args: Any, **_: Any) -> dict[str, Any]:
            updates.append(args)
            return {}

        monkeypatch.setattr(role_configs, "set_config", set_config)
        status, body = _call("PUT", "/api/roles/worker", {field: "x", "system_prompt": "y"})
        assert status == 400 and "/api/agent-settings/roles/worker" in body["error"] and updates == []
