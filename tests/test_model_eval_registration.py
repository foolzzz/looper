"""Eval registration uses the public model-reference contract, without a live server."""

from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from evals.prompt_regression import runner
from looper.agents import registry_migration
from looper.agents.profile_registry import ModelSpec
from looper.agents.provider_presets import presets_view


class RegistryApi:
    """Stateful HTTP boundary: rejects the retired provider/profile write shapes."""

    def __init__(self):
        self.registry = {
            "runtimes": [{"runtime_id": f"rt-{name}", "name": name, "type": name, "builtin": True}
                         for name in ("claude-code", "omp", "codex")],
            "providers": [{"provider_id": "prov-claude-subscription", "name": "subscription",
                           "vendor_type": "claude-subscription", "auth": "oauth",
                           "oauth_runtime_type": "claude-code", "env": [],
                           "endpoints": [{"protocol": "anthropic-messages", "base_url": ""}]}],
            "models": [], "profiles": [], "provider_presets": presets_view(),
        }
        self.posts = []
        self.settings = []

    def post(self, path, body):
        if path == "/api/v1/registry/view":
            assert body == {}
            return {"status": "ok", "result": deepcopy(self.registry)}
        if path == "/api/v1/agent_settings/update_system":
            self.settings.append(deepcopy(body["values"]))
            return {"status": "ok", "result": {}}
        self.posts.append((path, deepcopy(body)))
        if path == "/api/providers":
            assert not {"protocols", "models", "base_url", "api_key_env", "auth"} & body.keys()
            provider = {**body, "provider_id": f"prov-{len(self.registry['providers'])}",
                        "api_key": "***", "auth": "api_key", "oauth_runtime_type": None}
            self.registry["providers"].append(provider)
            return {"provider": deepcopy(provider)}
        if path == "/api/models":
            ModelSpec.from_entry({"id": "draft", **body})
            provider = next(p for p in self.registry["providers"] if p["provider_id"] == body["provider_id"])
            assert body["protocol"] in {e["protocol"] for e in provider["endpoints"]}
            assert not any(all(m[k] == body[k] for k in ("provider_id", "name", "protocol"))
                           for m in self.registry["models"])
            model = {**body, "model_id": f"model-{len(self.registry['models'])}"}
            self.registry["models"].append(model)
            return {"model": deepcopy(model)}
        assert path == "/api/agent-profiles"
        assert set(body) <= {"name", "runtime_id", "model_id", "effort", "agent_teams", "custom_prompt"}
        model = next(m for m in self.registry["models"] if m["model_id"] == body["model_id"])
        profile = {**body, "profile_id": f"agent-{len(self.registry['profiles'])}", "builtin": False,
                   "model": {k: model[k] for k in ("model_id", "name", "protocol", "provider_id")},
                   "provider_id": model["provider_id"],
                   "cost": {"source": "provider_price", "equivalent": False, "unpriced": []}}
        self.registry["profiles"].append(profile)
        return {"profile": deepcopy(profile)}


def explicit_spec():
    return {"runtime": {"type": "omp"},
            "provider": {"name": "test account", "vendor_type": "custom", "api_key_env": "EVAL_TEST_KEY",
                         "endpoints": [{"protocol": "openai-chat", "base_url": "https://example.invalid/v1"}]},
            "model": {"name": "Exact.Model", "protocol": "openai-chat", "context_window": 64000,
                      "input_usd_per_mtok": 2, "output_usd_per_mtok": 4,
                      "efforts": ["high"], "background_model": "Small.Model"},
            "agent": {"name": "eval agent", "effort": "high", "agent_teams": False,
                      "custom_prompt": "test instructions"}}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("EVAL_TEST_KEY", "synthetic-eval-secret")
    return RegistryApi()


def test_four_layer_registration_and_readback_keep_model_settings_and_secrets_separate(api, capsys):
    spec = explicit_spec()
    original = deepcopy(spec)
    profile = runner.register_agent(api, spec)
    assert spec == original
    assert [path for path, _ in api.posts] == ["/api/providers", "/api/models", "/api/agent-profiles"]
    assert api.posts[0][1]["api_key"] == "synthetic-eval-secret"
    saved = api.post("/api/v1/registry/view", {})["result"]
    assert saved["models"][0] == {**spec["model"], "provider_id": "prov-1", "model_id": profile["model_id"]}
    chosen = runner.select_agent(api, agent=profile["name"], model=None)
    assert chosen["model"]["name"] == "Exact.Model"
    assert chosen["model"]["protocol"] == "openai-chat"
    assert chosen["cost"] == profile["cost"]
    assert api.settings[-1] == {"profile_id": chosen["profile_id"]}
    assert "synthetic-eval-secret" not in json.dumps((spec, profile, chosen, saved))
    assert "synthetic-eval-secret" not in str(capsys.readouterr())
    assert "api_key_env" not in json.dumps(api.posts)


@pytest.mark.parametrize("reference", ["id", "id_object", "composite"])
def test_saved_model_references_do_not_create_or_copy_models(api, reference):
    first = runner.register_agent(api, explicit_spec())
    spec = {"runtime": {"type": "omp"}, "provider": {"provider_id": first["provider_id"]},
            "model": first["model_id"] if reference == "id" else {"model_id": first["model_id"]}
            if reference == "id_object" else {"provider_id": first["provider_id"], "name": "Exact.Model",
                                               "protocol": "openai-chat"},
            "agent": {"name": "second"}}
    api.posts.clear()
    second = runner.register_agent(api, spec)
    assert second["model_id"] == first["model_id"]
    assert [path for path, _ in api.posts] == ["/api/agent-profiles"]


@pytest.mark.parametrize("reference", [{"model_id": "missing"},
    {"provider_id": "another", "name": "Exact.Model", "protocol": "openai-chat"}])
def test_bad_model_reference_fails_before_profile_creation(api, reference):
    profile = runner.register_agent(api, explicit_spec())
    api.posts.clear()
    with pytest.raises(runner.EvalError):
        runner.register_agent(api, {"runtime": {"type": "omp"},
                                   "provider": {"provider_id": profile["provider_id"]},
                                   "model": reference, "agent": {"name": "bad"}})
    assert not api.posts


def test_model_id_from_another_account_is_rejected(api):
    first = runner.register_agent(api, explicit_spec())
    spec = explicit_spec()
    spec["provider"]["name"] = "other account"
    spec["agent"]["name"] = "other agent"
    other = runner.register_agent(api, spec)
    api.posts.clear()
    with pytest.raises(runner.EvalError, match="different provider"):
        runner.register_agent(api, {"runtime": {"type": "omp"},
                                   "provider": {"provider_id": other["provider_id"]},
                                   "model": first["model_id"], "agent": {"name": "bad"}})
    assert not api.posts


def legacy_spec(runtime="omp", protocols=None):
    return {"runtime": {"type": runtime},
            "provider": {"name": "old account", "auth": "api_key", "api_key_env": "EVAL_TEST_KEY",
                         "base_url": "https://example.invalid/v1",
                         "protocols": protocols or ["anthropic-messages", "openai-chat", "openai-responses"],
                         "models": [{"name": "Exact.Model", "context_window": 32000,
                                     "input_usd_per_mtok": 1.5}]},
            "agent": {"name": "old agent", "model": "Exact.Model", "effort": "high"}}


@pytest.mark.parametrize("runtime,protocol", [("omp", "openai-responses"),
    ("codex", "openai-responses"), ("claude-code", "anthropic-messages")])
def test_legacy_registration_uses_shared_converter_and_patched_protocol_priority(api, monkeypatch, runtime, protocol):
    calls = []
    original = registry_migration.convert

    def convert(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(registry_migration, "convert", convert)
    profile = runner.register_agent(api, legacy_spec(runtime))
    assert len(calls) == 1
    assert profile["model"]["protocol"] == protocol
    models = api.post("/api/v1/registry/view", {})["result"]["models"]
    assert {m["protocol"] for m in models} == {"anthropic-messages", "openai-chat", "openai-responses"}
    assert all(m["context_window"] == 32000 and m["input_usd_per_mtok"] == 1.5 for m in models)
    assert "model" not in api.posts[-1][1] and "provider_id" not in api.posts[-1][1]


def test_incompatible_legacy_protocol_is_rejected_before_writes(api):
    with pytest.raises(runner.EvalError, match="incompatible_protocol"):
        runner.register_agent(api, legacy_spec("codex", ["openai-chat"]))
    assert not api.posts


def test_legacy_runtime_default_url_becomes_an_explicit_endpoint(api):
    spec = legacy_spec("codex", ["openai-responses"])
    spec["provider"]["base_url"] = ""
    profile = runner.register_agent(api, spec)
    assert profile["model"]["protocol"] == "openai-responses"
    assert api.posts[0][1]["endpoints"] == [{"protocol": "openai-responses",
                                           "base_url": "https://api.openai.com/v1"}]


def test_legacy_spec_can_reference_saved_login_account(api):
    profile = runner.register_agent(api, {
        "runtime": {"type": "claude-code"}, "provider": {"provider_id": "prov-claude-subscription"},
        "agent": {"name": "legacy login", "model": "sonnet", "effort": "high"}})
    assert profile["model"]["name"] == "sonnet"
    assert profile["model"]["protocol"] == "anthropic-messages"
    assert [path for path, _ in api.posts] == ["/api/models", "/api/agent-profiles"]


def test_missing_key_fails_without_writing_or_mutating_spec(api, monkeypatch):
    monkeypatch.delenv("EVAL_TEST_KEY")
    spec = explicit_spec()
    original = deepcopy(spec)
    with pytest.raises(runner.EvalError, match="EVAL_TEST_KEY"):
        runner.register_agent(api, spec)
    assert not api.posts and spec == original


def test_inline_api_key_is_rejected(api):
    spec = explicit_spec()
    spec["provider"]["api_key"] = "synthetic-inline-secret"
    with pytest.raises(runner.EvalError, match="api_key_env") as error:
        runner.register_agent(api, spec)
    assert "synthetic-inline-secret" not in str(error.value)
    assert not api.posts


@pytest.mark.parametrize("truncated", [False, True])
def test_provider_registration_errors_never_echo_credentials(api, monkeypatch, truncated):
    original_post = api.post

    def rejected(path, body):
        if path == "/api/v1/registry/view":
            return original_post(path, body)
        assert path == "/api/providers"
        # HTTP errors are clipped by Api.request before the registration helper sees them.
        detail = body["api_key"][:10] if truncated else json.dumps(body)
        raise runner.EvalError(f"bad request: {detail}")

    monkeypatch.setattr(api, "post", rejected)
    with pytest.raises(runner.EvalError) as error:
        runner.register_agent(api, explicit_spec())
    assert "synthetic-" not in str(error.value)


def test_presets_register_each_apis_own_catalog_and_select_messages_model(api):
    preset = next(p for p in api.registry["provider_presets"] if p["preset_id"] == "opencode")
    profile = runner.register_provider_agent(api, runner.ProviderChoice("opencode", api_key="synthetic-key"))
    expected = {(m["name"], protocol) for entry in preset["apis"] for protocol in entry["protocols"]
                for m in runner.api_models(preset, entry)}
    assert {(m["name"], m["protocol"]) for m in api.registry["models"]} == expected
    messages = next(entry for entry in preset["apis"] if "anthropic-messages" in entry["protocols"])
    assert profile["model"]["name"] == runner.api_models(preset, messages)[0]["name"]
    assert profile["model"]["protocol"] == "anthropic-messages"
    assert len(api.posts[0][1]["endpoints"]) == sum(len(entry["protocols"]) for entry in preset["apis"])


@pytest.mark.parametrize("preset", presets_view(), ids=lambda preset: preset["preset_id"])
def test_each_preset_selects_a_messages_model_and_keeps_budget(api, preset):
    profile = runner.select_agent(api, agent=None, model=None, run_budget_usd=0.25,
                                  provider=runner.ProviderChoice(preset["preset_id"], api_key="synthetic-key"))
    assert profile["model"]["protocol"] == "anthropic-messages"
    assert api.settings[-1] == {"profile_id": profile["profile_id"], "run_budget_usd": 0.25}


def test_preset_override_changes_only_messages_endpoint(api):
    runner.register_provider_agent(api, runner.ProviderChoice("opencode", base_url="https://proxy.invalid",
                                                             api_key="synthetic-key"))
    endpoints = api.posts[0][1]["endpoints"]
    assert next(e for e in endpoints if e["protocol"] == "anthropic-messages")["base_url"] == "https://proxy.invalid"
    assert all(e["base_url"] != "https://proxy.invalid" for e in endpoints if e["protocol"] != "anthropic-messages")


def test_preset_rejects_model_from_another_api_before_writes(api):
    preset = next(p for p in api.registry["provider_presets"] if p["preset_id"] == "opencode")
    responses = next(entry for entry in preset["apis"] if "openai-responses" in entry["protocols"])
    model = runner.api_models(preset, responses)[0]["name"]
    with pytest.raises(runner.EvalError, match="Messages catalog"):
        runner.register_provider_agent(api, runner.ProviderChoice("opencode", model=model, api_key="synthetic-key"))
    assert not api.posts


def test_model_option_creates_subscription_model_once_and_reuses_profile(api):
    first = runner.select_agent(api, agent=None, model="sonnet")
    second = runner.select_agent(api, agent=None, model="sonnet")
    assert first == second
    assert first["model"]["protocol"] == "anthropic-messages"
    assert first["provider_id"] == "prov-claude-subscription"
    assert [path for path, _ in api.posts] == ["/api/models", "/api/agent-profiles"]


def test_run_copies_only_model_reference_and_reports_identity_and_cost(api, monkeypatch, tmp_path):
    profile = runner.register_agent(api, explicit_spec())
    api.posts.clear()

    @contextmanager
    def database(*args):
        yield "unused", 0.0

    @contextmanager
    def server(**kwargs):
        yield api

    monkeypatch.setattr(runner, "fresh_database", database)
    monkeypatch.setattr(runner, "running_server", server)
    monkeypatch.setattr(runner, "install_claude", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_git", lambda: {})
    monkeypatch.setattr(runner, "run_suite", lambda *args, **kwargs: ([], False))
    task = next(task for task in runner.task_list.TASKS if task.planner_may_choose)
    result, path = runner.run(mode="real", tasks=[task], admin_url="unused", out_dir=tmp_path,
                              agent=profile["name"])
    assert result["aborted"] is None
    assert api.posts[0][1] == {"name": runner.CHOSEN_PROFILE, "runtime_id": "rt-omp",
                                "model_id": profile["model_id"], "effort": "high"}
    assert result["model"] == "Exact.Model"
    assert result["protocol"] == "openai-chat"
    assert result["cost_basis"] == profile["cost"]
    assert json.loads(path.read_text()) == result
    assert "synthetic-eval-secret" not in path.read_text()
