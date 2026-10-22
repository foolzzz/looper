"""Observable Manager model dialogs, including mutations that must fail fake evals."""

from copy import deepcopy
from dataclasses import asdict
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evals.prompt_regression import dialogs
from evals.prompt_regression.runner import Api
from looper.agents.profile_registry import ModelSpec
from tests.test_model_eval_registration import RegistryApi


class SeedApi(RegistryApi):
    def put(self, path, body):
        self.settings.append((path, deepcopy(body)))


def test_light_and_manager_seeds_register_model_references_once():
    api = SeedApi()
    dialogs._make_light_profile(api, "project-test")
    light = api.registry["profiles"][0]
    assert light["name"] == dialogs.LIGHT
    assert light["model"]["name"] == "haiku"
    assert light["model"]["protocol"] == "anthropic-messages"
    first = dialogs._manager_profile(api, "sonnet", "high")
    second = dialogs._manager_profile(api, "sonnet", "high")
    assert first == second
    assert len(api.registry["models"]) == 2
    assert [path for path, _ in api.posts] == ["/api/models", "/api/agent-profiles"] * 2
    assert api.settings[-1] == ("/api/agent-settings/roles/manager", {"profile_id": first["profile_id"]})


def test_light_and_manager_profiles_share_an_existing_model_without_copying_it():
    api = SeedApi()
    dialogs._make_light_profile(api, "project-test")
    model_id = api.registry["models"][0]["model_id"]
    api.posts.clear()
    dialogs._make_light_profile(api, "project-test")
    manager = dialogs._manager_profile(api, "haiku", "high")
    assert manager["model_id"] == model_id
    assert [path for path, _ in api.posts] == ["/api/agent-profiles"]


def test_model_dialog_fake_scripts_use_the_registered_tool_schemas():
    from looper.agents.ops import check_arguments
    from looper.agents.ops.config import TOOLS

    schemas = {tool.name: tool.schema for tool in TOOLS}
    for case in dialogs.select(["provider_key", "model_custom", "model_dual_protocol", "model_login_runtime",
                                "model_missing_endpoint", "model_price_history"]):
        for say in case.says:
            for tool, arguments in say.fake:
                assert check_arguments(schemas[tool], deepcopy(arguments)) == arguments


def action(tool, verb, *, status="ok", **arguments):
    return {"tool": tool, "risk": "read" if verb == "list" else "write", "status": status,
            "arguments": {"action": verb, **arguments}, "result": {}, "error_code": None}


class ObservedApi:
    def __init__(self, registry):
        self.registry = registry

    def get(self, path):
        assert path == "/api/registry"
        return deepcopy(self.registry)


def observed(monkeypatch, registry, actions, *, row=None, reply="已完成"):
    monkeypatch.setattr(dialogs, "_trail", lambda *_: deepcopy(actions))
    if row is not None:
        monkeypatch.setattr(dialogs, "_read_model_profile", lambda *_: deepcopy(row))
    return dialogs.Look(ObservedApi(registry), None, reply, "disposable", 0, frozenset(), True)


def provider_state():
    return {"providers": [{"provider_id": "prov-test", "name": "eval-deepseek", "api_key_set": False,
                           "vendor_type": "deepseek", "endpoints": [
                               {"protocol": "anthropic-messages", "base_url": "https://api.deepseek.com/anthropic"}]}],
            "models": [], "profiles": []}


@pytest.mark.parametrize("mutation", ["wrong_protocol", "extra_write", "reply_key", "trail_key", "stored_key",
                                      "tool_failed", "missing_model"])
def test_provider_key_case_fails_observable_mutations(monkeypatch, mutation):
    registry = provider_state()
    registry["models"] = [{"model_id": "model-test", "provider_id": "prov-test", "name": "deepseek-flash",
                           "protocol": "anthropic-messages"}]
    actions = [action("looper_providers", "register"), action("looper_models", "create")]
    look = observed(monkeypatch, registry, actions)
    check = dialogs.select(["provider_key"])[0].says[0].check
    assert check(look) is None  # Positive control before every negative mutation.
    if mutation == "wrong_protocol":
        registry["models"][0]["protocol"] = "openai-chat"
    elif mutation == "extra_write":
        actions.append(action("looper_settings", "set"))
    elif mutation == "reply_key":
        look.reply += dialogs.FAKE_KEY
    elif mutation == "trail_key":
        actions[0]["arguments"]["api_key"] = dialogs.FAKE_KEY
    elif mutation == "stored_key":
        registry["providers"][0]["api_key_set"] = True
    elif mutation == "tool_failed":
        actions[1]["status"] = "error"
    else:
        registry["models"].clear()
    assert check(look) is not None


def custom_state(protocol="anthropic-messages"):
    model = asdict(ModelSpec("model-custom", "prov-custom", "Exact.Model", protocol,
                            context_window=64000, input_usd_per_mtok=1, output_usd_per_mtok=3))
    model["protocol"] = protocol
    provider = {"id": "prov-custom", "name": "eval-custom", "vendor_type": "custom", "auth": "api_key",
                "oauth_runtime_type": None, "env": [], "max_concurrency": None, "builtin": False}
    row = {"id": "agent-custom", "name": "eval/custom", "model_id": model["id"], "runtime_id": "rt-claude-code",
           "runtime_name": "claude-code", "runtime_type": "claude-code", "model": model, "provider": provider,
           "endpoint": {"provider_id": provider["id"], "protocol": protocol,
                        "base_url": "https://custom.example.invalid/messages", "use_runtime_default": False}}
    registry = {"providers": [{**provider, "provider_id": provider["id"], "api_key_set": False}],
                "models": [{**model, "model_id": model["id"]}],
                "profiles": [{"profile_id": row["id"], "name": row["name"], "model_id": model["id"]}]}
    return registry, row


@pytest.mark.parametrize("mutation", ["protocol", "reference", "endpoint", "price", "missing_db", "extra_write"])
def test_custom_case_checks_database_reference_actual_binding_and_cost(monkeypatch, mutation):
    registry, row = custom_state()
    actions = [action("looper_providers", "register"), action("looper_models", "create"),
               action("looper_agent_profiles", "create")]
    look = observed(monkeypatch, registry, actions, row=row)
    check = dialogs.select(["model_custom"])[0].says[0].check
    assert check(look) is None
    if mutation == "protocol":
        row["model"]["protocol"] = row["endpoint"]["protocol"] = "openai-chat"
    elif mutation == "reference":
        row["model_id"] = "model-other"
    elif mutation == "endpoint":
        row["endpoint"]["base_url"] = "https://other.example.invalid"
    elif mutation == "price":
        row["model"]["input_usd_per_mtok"] = 99
    elif mutation == "missing_db":
        monkeypatch.setattr(dialogs, "_read_model_profile", lambda *_: None)
    else:
        actions.append(action("looper_providers", "register"))
    assert check(look) is not None


def test_model_case_ids_are_unique_and_selectable_without_export_suite():
    expected = {"provider_key", "config_tools", "task_cost", "schedule_planner_profile", "model_custom",
                "model_dual_protocol", "model_login_runtime", "model_missing_endpoint", "model_price_history",
                "session_profile_switch", "session_profile_ask"}
    selected = dialogs.select(sorted(expected))
    assert {case.id for case in selected} == expected
    assert len({case.id for case in dialogs.DIALOGS}) == len(dialogs.DIALOGS)
    assert "config_export" not in expected
    assert [case.id for case in dialogs.select(["stop_then_continue"])] == ["stop_then_continue"]
    assert sum(case.id == "stop_then_continue" for case in dialogs.DIALOGS) == 1


def test_the_stop_dialog_stops_its_first_say_only_and_its_fake_calls_are_valid():
    from looper.agents.ops import check_arguments
    from looper.agents.ops.config import TOOLS

    (case,) = dialogs.select(["stop_then_continue"])
    assert case.tags == ("stop",)
    assert [say.stop for say in case.says] == [True, False]
    schemas = {tool.name: tool.schema for tool in TOOLS}
    tool, arguments = case.says[0].fake[0]
    assert check_arguments(schemas[tool], deepcopy(arguments)) == arguments
    assert case.says[0].fake[-1][0] == "__hold__"


@pytest.mark.parametrize("mutation", [None, "wrong_protocol", "extra_write", "key_echo"])
def test_fake_dialog_outcome_uses_observations_instead_of_success_words(monkeypatch, tmp_path, mutation):
    registry = provider_state()
    registry["models"] = [{"model_id": "model-test", "provider_id": "prov-test", "name": "deepseek-flash",
                           "protocol": "openai-chat" if mutation == "wrong_protocol" else "anthropic-messages"}]
    actions = [action("looper_providers", "register"), action("looper_models", "create")]
    if mutation == "extra_write":
        actions.append(action("looper_settings", "set"))
    # Keep the real run_dialog/check path; only replace HTTP and DB I/O.
    class Api(ObservedApi):
        def get(self, path):
            return {"tasks": []} if path == "/api/tasks" else super().get(path)

    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: deepcopy(actions))
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    def ask(api, session, text, **kwargs):
        calls = json.loads(scenarios.read_text())["manager"][text]
        assert [call[0] for call in calls] == ["looper_providers", "looper_models"]
        return "已成功创建正确的模型。" + (dialogs.FAKE_KEY if mutation == "key_echo" else "")
    monkeypatch.setattr(dialogs, "_ask", ask)
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text("{}")
    result = dialogs.run_dialog(Api(registry), dialogs.select(["provider_key"])[0], database_url="disposable",
                                project_id="project-test", scenarios=scenarios, mode="fake", timeout=30)
    assert result["outcome"] == ("pass" if mutation is None else "fail"), result


@pytest.mark.parametrize("case_id", ["model_login_runtime", "model_missing_endpoint"])
@pytest.mark.parametrize("mutation", [None, "write", "state_changed", "no_successful_read"])
def test_refused_pairings_require_readback_without_registry_changes(monkeypatch, case_id, mutation):
    registry = provider_state()
    actions = [action("looper_providers", "list")]
    look = observed(monkeypatch, registry, actions)
    look.registry_before = deepcopy(registry)
    if mutation == "write":
        actions.append(action("looper_agent_profiles", "create"))
    elif mutation == "state_changed":
        registry["profiles"].append({"profile_id": "agent-wrong"})
    elif mutation == "no_successful_read":
        actions[0]["status"] = "error"
    check = dialogs.select([case_id])[0].says[0].check
    assert (check(look) is None) is (mutation is None)


def dual_state(input_price=1):
    registry, row = custom_state()
    row["name"] = registry["profiles"][0]["name"] = dialogs.DUAL_PROFILE
    row["provider"]["name"] = registry["providers"][0]["name"] = dialogs.DUAL_ACCOUNT
    row["model"]["name"] = registry["models"][0]["name"] = dialogs.DUAL_MODEL
    row["endpoint"]["base_url"] = dialogs.DUAL_URL
    row["model"]["input_usd_per_mtok"] = registry["models"][0]["input_usd_per_mtok"] = input_price
    registry["models"].insert(0, {**registry["models"][0], "model_id": "model-chat", "protocol": "openai-chat",
                                   "input_usd_per_mtok": 9})
    return registry, row


def test_dual_protocol_case_rejects_the_incompatible_homonym(monkeypatch):
    registry, row = dual_state()
    look = observed(monkeypatch, registry, [action("looper_agent_profiles", "create")], row=row)
    check = dialogs.select(["model_dual_protocol"])[0].says[0].check
    assert check(look) is None
    row["model"]["protocol"] = row["endpoint"]["protocol"] = "openai-chat"
    assert check(look) is not None


@pytest.mark.parametrize("mutation", [None, "cost", "tokens", "snapshot", "other_protocol", "no_cost_read"])
def test_repricing_keeps_historical_spend_and_other_protocol_price(monkeypatch, mutation):
    registry, row = dual_state(5)
    actions = [action("looper_models", "update"), action("looper_costs", "list")]
    look = observed(monkeypatch, registry, actions, row=row)
    history = [{"model": dialogs.DUAL_MODEL, "input_tokens": 1000, "output_tokens": 100, "cost_usd": 0.0013,
                "cost_source": "provider_price", "metadata": {
                    "pricing": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 3}}}]
    monkeypatch.setattr(dialogs, "_price_history", lambda *_: deepcopy(history))
    check = dialogs.select(["model_price_history"])[0].says[0].check
    assert check(look) is None
    if mutation == "cost":
        history[0]["cost_usd"] = 0.0053
    elif mutation == "tokens":
        history[0]["input_tokens"] = 2
    elif mutation == "snapshot":
        history[0]["metadata"]["pricing"]["input_usd_per_mtok"] = 5
    elif mutation == "other_protocol":
        registry["models"][0]["input_usd_per_mtok"] = 5
    elif mutation == "no_cost_read":
        actions[1]["status"] = "error"
    assert (check(look) is None) is (mutation is None)


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="disposable PostgreSQL unavailable")
def test_targeted_model_dialogs_run_through_fake_manager_and_database(tmp_path):
    cases = dialogs.select(["provider_key", "config_tools", "task_cost", "schedule_planner_profile",
                            "model_custom", "model_dual_protocol", "model_login_runtime",
                            "model_missing_endpoint", "model_price_history"])
    result, path = dialogs.run(mode="fake", dialogs=cases, admin_url=os.environ["LOOPER_TEST_DATABASE_URL"],
                               out_dir=tmp_path, timeout=120)
    assert result["aborted"] is None, result
    assert all(row["outcome"] == "pass" for row in result["dialogs"]), result["dialogs"]
    assert len(result["dialogs"]) == len(cases)
    # The pasted synthetic key belongs only in the input, never the answer or trail.
    for row in json.loads(path.read_text())["dialogs"]:
        for turn in row["turns"]:
            assert dialogs.FAKE_KEY not in turn["reply"]
            assert dialogs.FAKE_KEY not in json.dumps(turn["errors"])


# --- The session-profile dialogs ---


def test_switch_profile_seed_registers_a_keyed_provider_and_one_profile_in_fake_mode():
    api = SeedApi()
    dialogs._make_switch_profile(api, "project-test")
    providers = [body for path, body in api.posts if path == "/api/providers"]
    assert len(providers) == 1 and providers[0]["name"] == "eval-switch"
    assert providers[0]["api_key"] == dialogs.FAKE_KEY
    assert providers[0]["endpoints"] == [{"protocol": "anthropic-messages",
                                          "base_url": "https://switch.example.invalid"}]
    assert [p["name"] for p in api.registry["profiles"]] == [dialogs.SWITCH]
    assert api.registry["profiles"][0]["runtime_id"] == "rt-claude-code"


def test_switch_profile_seed_registers_a_spec_as_eval_switch(monkeypatch):
    calls = []
    monkeypatch.setattr(dialogs, "register_agent", lambda api, spec: calls.append(spec))
    spec = {"runtime": {"type": "omp"}, "provider": {"name": "p"}, "agent": {"name": "other", "model": "m"}}
    dialogs._make_switch_profile(SeedApi(), "project-test", fake=False, spec=spec)
    assert calls == [{**spec, "agent": {"name": dialogs.SWITCH, "model": "m"}}]


def test_switch_profile_seed_uses_the_login_provider_in_real_mode():
    api = SeedApi()
    dialogs._make_switch_profile(api, "project-test", fake=False)
    assert [p for p, _ in api.posts if p == "/api/providers"] == []
    assert api.registry["profiles"][0]["name"] == dialogs.SWITCH


def run_record(record_id, text="hi", *, source="session", profile="agent-1", switch=None):
    metadata = {"profile": {"id": profile, "source": source}}
    if switch:
        metadata["session_profile"] = switch
    return {"id": record_id, "input_text": text, "metadata": metadata}


class RunsApi:
    def __init__(self, records, selected=None):
        self.records, self.selected = records, selected

    def get(self, path, **query):
        if path == "/api/llm-records":
            return {"records": list(reversed(self.records))}
        if path == "/api/manager/session":
            return {"selected": self.selected}
        assert path == "/api/registry"
        return {"profiles": [{"profile_id": "agent-1", "name": dialogs.SWITCH}]}


def runs_look(records, *, selected=None, reply="ok", fake=False):
    return dialogs.Look(RunsApi(records, selected), None, reply, "disposable", 0, frozenset(), fake)


def test_switch_dialogs_are_selectable_with_unique_ids():
    selected = dialogs.select(["session_profile_switch", "session_profile_ask"])
    assert [d.id for d in selected] == ["session_profile_switch", "session_profile_ask"]
    assert all(d.seed is dialogs._make_switch_profile for d in selected)
    assert len({d.id for d in dialogs.DIALOGS}) == len(dialogs.DIALOGS)


def test_switch_number_is_read_from_the_listing():
    look = runs_look([], reply="0. 跟随设置\n1. eval/light · claude-code\n2. eval/switch · claude-code · x")
    assert dialogs.select(["session_profile_switch"])[0].says[1].text_from(look) == "/profile 2"
    assert dialogs._profile_listed(look) is None
    assert dialogs._profile_listed(runs_look([run_record(1)], reply=look.reply)) is not None


@pytest.mark.parametrize("source,ok", [("session", True), ("role", False)])
def test_answer_must_run_on_the_session_profile(source, ok):
    records = [run_record(1, "", source="x", switch={"to": "agent-1", "effect": "resumes"}),
               run_record(2, "记住暗号", source=source)]
    problem = dialogs._answered_on_the_switch_profile(runs_look(records))
    assert (problem is None) == ok


def test_answer_after_a_new_session_needs_handoff_on_the_old_profile_and_resume():
    switch = run_record(1, "/profile 2", switch={"to": "agent-1", "effect": "new_session"})
    handoff = run_record(2, "[HANDOFF] x", profile="agent-0", source="settings")
    answer = run_record(3, "[RESUME] x")
    check = dialogs._answered_on_the_switch_profile
    assert check(runs_look([switch, handoff, answer])) is None
    assert check(runs_look([switch, answer])) is not None
    assert check(runs_look([switch, handoff, run_record(3, "plain")])) is not None
    assert check(runs_look([switch, run_record(2, "[HANDOFF] x"), answer])) is not None


def test_ask_check_fails_on_a_settings_write_or_a_selection(monkeypatch):
    actions = []
    monkeypatch.setattr(dialogs, "_trail", lambda *_: deepcopy(actions))
    reply = "我不能直接切换，请发送 /profile 选择。"
    assert dialogs._switch_not_done_by_the_manager(runs_look([], reply=reply)) is None
    actions.append(action("looper_settings", "set"))
    assert dialogs._switch_not_done_by_the_manager(runs_look([], reply=reply)) is not None
    actions.clear()
    selected = {"profile_id": "agent-1", "name": dialogs.SWITCH}
    assert dialogs._switch_not_done_by_the_manager(runs_look([], selected=selected, reply=reply)) is not None
    assert dialogs._switch_not_done_by_the_manager(runs_look([], reply="好的。")) is not None
    assert dialogs._switch_not_done_by_the_manager(runs_look([], reply="好的。", fake=True)) is None


def test_omp_deepseek_example_registers():
    from pathlib import Path
    from evals.prompt_regression import runner

    path = Path(__file__).parents[1] / "evals" / "agents" / "omp-deepseek.example.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    os.environ["LOOPER_EVAL_API_KEY"] = "synthetic"
    try:
        profile = runner.register_agent(SeedApi(), spec)
    finally:
        del os.environ["LOOPER_EVAL_API_KEY"]
    assert profile["name"] == "omp/deepseek/deepseek-flash"
    assert profile["model"]["protocol"] == "openai-chat"


# --- The scenario Manager dialogs ---


class RecordingApi:
    def __init__(self):
        self.gets, self.posts = [], []

    def get(self, path, **query):
        self.gets.append((path, query))
        if path == "/api/llm-records":
            return {"records": []}
        if path == "/api/chat/turn":
            return {"turn": None}
        if path == "/api/chat/messages":
            return {"messages": [{"message_id": self.posts[-1][1]["message_id"] + ":reply", "content": "ok"}]}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
        return {}


def _ask_once(manager):
    api = RecordingApi()
    with pytest.raises(dialogs.EvalError):  # no run record: the reply is not scored
        dialogs._ask(api, "s", "hi", timeout=5, manager=manager)
    return api


def test_an_existing_dialog_sends_no_target_and_asks_for_manager_runs():
    api = _ask_once(None)
    assert "target" not in api.posts[0][1]
    assert {q["agent_id"] for p, q in api.gets if p == "/api/llm-records"} == {"manager"}


def test_a_scenario_manager_dialog_targets_and_reads_its_own_runs():
    api = _ask_once("code-review")
    assert api.posts[0][1]["target"] == {"kind": "manager", "id": "manager-code-review"}
    reads = [q["agent_id"] for p, q in api.gets if p == "/api/llm-records"]
    assert reads and set(reads) == {"manager-code-review"}


def test_template_dialogs_are_selectable_and_use_the_scenario_manager():
    cases = dialogs.select(["template_review_task", "template_missing_input"])
    assert [c.manager for c in cases] == ["code-review"] * 2
    assert all(c.seed is dialogs._seed_review_manager for c in cases)
    assert cases[0].says[1].fake[0][1]["template"] == "pr_review"


def test_review_seed_creates_the_manager_and_its_template_in_the_project():
    class Api:
        def __init__(self):
            self.calls = []

        def post(self, path, body):
            self.calls.append(("POST", path, body))

        def put(self, path, body):
            self.calls.append(("PUT", path, body))

    api = Api()
    dialogs._seed_review_manager(api, "project-x")
    assert api.calls[0][:2] == ("POST", "/api/managers") and api.calls[0][2]["key"] == "code-review"
    assert api.calls[1][:2] == ("PUT", "/api/managers/manager-code-review/templates/pr_review")
    assert api.calls[1][2]["project_id"] == "project-x"


class TemplateApi:
    def get(self, path, **query):
        assert path == "/api/tasks"
        return {"tasks": []}


def template_look(reply, monkeypatch, fake=False):
    monkeypatch.setattr(dialogs, "_trail", lambda *a, **k: [])
    return dialogs.Look(TemplateApi(), None, reply, "disposable", 0, frozenset(), fake)


@pytest.mark.parametrize("reply, ok", [
    ("好的", False),
    ("要评审 https://github.com/octo-org/octo-repo/pull/42。", False),
    ("要评审 https://github.com/octo-org/octo-repo/pull/42，确认吗？", True),
    ("Review pull/42 — confirm?", True),
])
def test_asked_to_confirm_template(monkeypatch, reply, ok):
    assert (dialogs._asked_to_confirm_template(template_look(reply, monkeypatch)) is None) is ok


def test_asked_to_confirm_template_checks_only_the_state_in_fake_mode(monkeypatch):
    assert dialogs._asked_to_confirm_template(template_look("好的", monkeypatch, fake=True)) is None
    assert dialogs._asked_to_confirm_template(template_look('{"a": 1}', monkeypatch, fake=True))


def test_asked_for_the_link(monkeypatch):
    assert dialogs._asked_for_the_link(template_look("请给我 PR 链接", monkeypatch)) is None
    assert dialogs._asked_for_the_link(template_look("好的", monkeypatch))
    assert dialogs._asked_for_the_link(template_look("好的", monkeypatch, fake=True)) is None


# --- The client dialogs (loopercli's channel) ---

ACCEPTED_AND_FINAL = (b'event: accepted\ndata: {"conversation_id": "cli-abc", "message_id": "m1", '
                      b'"manager_id": "manager"}\n\n: heartbeat\n\n'
                      b'event: final\ndata: {"text": "done", "outcome": "ok"}\n\n')
ACCEPTED_ONLY = b'event: accepted\ndata: {"conversation_id": "cli-abc"}\n\nevent: heartbeat\ndata: {}\n\n'


@pytest.fixture
def chat_server():
    """A stand-in for ``POST /api/v1/chat/send``: answers ``server.body`` (``server.status``) and records the
    requests it got."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            server.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(length))))
            self.send_response(server.status)
            self.send_header("Content-Type", "text/event-stream" if server.status == 200 else "text/plain")
            self.end_headers()
            self.wfile.write(server.body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.requests, server.status, server.body = [], 200, ACCEPTED_AND_FINAL
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_port}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(5)


def test_the_client_dialogs_exist_and_their_fake_scripts_use_the_registered_tool_schemas():
    from looper.agents.ops import check_arguments
    from looper.agents.ops.service import MANAGER_TOOLS

    schemas = {tool.name: tool.schema for tool in MANAGER_TOOLS}
    cases = dialogs.select(["client_handoff", "client_relay_cancel"])
    assert [c.id for c in cases] == ["client_handoff", "client_relay_cancel"]
    assert all(c.client for c in cases)
    assert not any(say.stop or say.withdraw for c in cases for say in c.says)
    assert [c.running_task for c in cases] == [False, True]
    for case in cases:
        for say in case.says:
            for tool, arguments in say.fake:
                assert check_arguments(schemas[tool], deepcopy(arguments)) == arguments


def test_the_other_dialogs_are_not_client_dialogs():
    assert [d.id for d in dialogs.DIALOGS if d.client] == ["client_handoff", "client_relay_cancel"]


def test_the_stand_in_strips_the_client_channel_line():
    import re

    content = ('Channel: loopercli, client "eval-1a2b3c" (the user works in their own coding agent, which '
               'writes these messages for them)\n\n用户说现在开始。')
    channel = re.search(r"^Channel: .*\n\n", content, re.MULTILINE)  # fake_claude._manager's pattern
    assert content[channel.end():] == "用户说现在开始。"


def test_ask_client_returns_the_final_text_and_conversation_with_the_clients_token(chat_server, monkeypatch):
    runs = []
    monkeypatch.setattr(dialogs, "_newest_run", lambda api, agent: 7)
    monkeypatch.setattr(dialogs, "_check_manager_run", lambda api, since, **kw: runs.append((api, since, kw)))
    operator, client = Api(chat_server.url, token="operator-token"), Api(chat_server.url, token="lpc_client")
    assert dialogs._ask_client(client, operator, None, "hi", timeout=5) == ("done", "cli-abc")
    assert dialogs._ask_client(client, operator, "cli-abc", "again", timeout=5) == ("done", "cli-abc")
    first, second = chat_server.requests
    assert first[0] == "/api/v1/chat/send" and first[2] == {"text": "hi"}
    assert second[2] == {"text": "again", "conversation_id": "cli-abc"}
    for _, headers, _ in chat_server.requests:
        assert headers["Authorization"] == "Bearer lpc_client"
        assert headers["Accept"] == "text/event-stream"
    assert [(since, kw["agent_id"]) for _, since, kw in runs] == [(7, "manager")] * 2


def test_ask_client_without_a_final_event_is_a_reply_timeout(chat_server, monkeypatch):
    chat_server.body = ACCEPTED_ONLY
    monkeypatch.setattr(dialogs, "_newest_run", lambda api, agent: 0)
    monkeypatch.setattr(dialogs, "_check_manager_run", lambda *a, **k: pytest.fail("no reply, no run check"))
    api = Api(chat_server.url, token="lpc_client")
    with pytest.raises(dialogs.ReplyTimeout):
        dialogs._ask_client(api, api, None, "hi", timeout=5)


def test_ask_client_reports_a_refusal_with_its_status_and_the_start_of_its_body(chat_server, monkeypatch):
    chat_server.status, chat_server.body = 401, b"x" * 500
    monkeypatch.setattr(dialogs, "_newest_run", lambda api, agent: 0)
    api = Api(chat_server.url, token="lpc_client")
    with pytest.raises(dialogs.EvalError, match="401") as caught:
        dialogs._ask_client(api, api, None, "hi", timeout=5)
    assert "x" * 300 in str(caught.value) and "x" * 301 not in str(caught.value)


def test_ask_client_reads_run_records_with_the_operator_api_after_the_stream(chat_server, monkeypatch):
    seen = []
    operator, client = Api(chat_server.url, token="operator-token"), Api(chat_server.url, token="lpc_client")

    def newest(api, agent):
        seen.append(("newest", api, len(chat_server.requests)))
        return 3

    def check(api, since, **kwargs):
        seen.append(("check", api, len(chat_server.requests)))
        assert since == 3

    monkeypatch.setattr(dialogs, "_newest_run", newest)
    monkeypatch.setattr(dialogs, "_check_manager_run", check)
    dialogs._ask_client(client, operator, None, "hi", timeout=5)
    assert [(what, api is operator, sent) for what, api, sent in seen] == [("newest", True, 0), ("check", True, 1)]


def test_ask_client_surfaces_a_failed_manager_run(chat_server, monkeypatch):
    monkeypatch.setattr(dialogs, "_newest_run", lambda api, agent: 0)

    def failed(api, since, **kwargs):
        raise dialogs.EvalError("Manager run 9 failed: boom")

    monkeypatch.setattr(dialogs, "_check_manager_run", failed)
    api = Api(chat_server.url, token="lpc_client")
    with pytest.raises(dialogs.EvalError, match="Manager run 9 failed"):
        dialogs._ask_client(api, api, None, "hi", timeout=5)


def test_a_client_dialog_refuses_stop_and_withdraw_before_sending_anything(monkeypatch):
    class Boom(Api):
        def request(self, method, path, body=None, **query):
            calls.append((method, path))
            raise AssertionError("nothing may be sent")

    calls = []
    monkeypatch.setattr(dialogs, "_last_action", lambda *a: 0)
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda api: None)
    monkeypatch.setattr(dialogs, "_ask_client", lambda *a, **k: pytest.fail("sent"))
    for flag in ("stop", "withdraw"):
        dialog = dialogs.Dialog("client_stop", (dialogs.Say("hi", lambda look: None, **{flag: True}),), client=True)
        row = dialogs.run_dialog(Boom("http://x"), dialog, database_url="disposable", project_id="p",
                                 scenarios=None, mode="real", timeout=1)
        assert row["outcome"] == "error" and "not supported in client dialogs" in row["error"]
    assert calls == []


class ClientRunApi(Api):
    def __init__(self):
        super().__init__("http://x", timeout=3)
        self.calls = []

    def request(self, method, path, body=None, **query):
        self.calls.append((method, path, body))
        if path == "/api/clients" and method == "POST":
            return {"client": {"id": "client-0123456789ab"}, "token": "lpc_secret"}
        if path == "/api/tasks":
            return {"tasks": []}
        return {}


def test_a_client_dialog_makes_a_client_and_revokes_it_without_leaking_its_token(monkeypatch, capsys):
    api, sent = ClientRunApi(), []

    def ask(client_api, operator_api, conversation, text, **kwargs):
        sent.append((client_api.token, operator_api is api, conversation))
        return "ok", "cli-1"

    monkeypatch.setattr(dialogs, "_last_action", lambda *a: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *a: [])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda api: None)
    monkeypatch.setattr(dialogs, "_ask_client", ask)
    seen = []
    dialog = dialogs.Dialog("client_two", (
        dialogs.Say("one", lambda look: seen.append(look.conversation)),
        dialogs.Say("two", lambda look: seen.append(look.conversation))), client=True)
    row = dialogs.run_dialog(api, dialog, database_url="disposable", project_id="p", scenarios=None, mode="real",
                             timeout=1)
    assert row["outcome"] == "pass"
    assert sent == [("lpc_secret", True, None), ("lpc_secret", True, "cli-1")]
    assert seen == ["cli-1", "cli-1"]
    assert api.calls[0][:2] == ("POST", "/api/clients") and api.calls[0][2]["name"].startswith("eval-")
    assert api.calls[-1] == ("DELETE", "/api/clients/client-0123456789ab", None)
    assert "lpc_secret" not in json.dumps(row) and "lpc_secret" not in capsys.readouterr().err


def test_a_failed_revoke_is_logged_without_the_token_and_ignored(monkeypatch, capsys):
    class Failing(ClientRunApi):
        def request(self, method, path, body=None, **query):
            if method == "DELETE":
                raise dialogs.EvalError(f"DELETE {path} answered 404: gone")
            return super().request(method, path, body, **query)

    monkeypatch.setattr(dialogs, "_last_action", lambda *a: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *a: [])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda api: None)
    monkeypatch.setattr(dialogs, "_ask_client", lambda *a, **k: ("ok", "cli-1"))
    dialog = dialogs.Dialog("client_one", (dialogs.Say("one", lambda look: None),), client=True)
    row = dialogs.run_dialog(Failing(), dialog, database_url="disposable", project_id="p", scenarios=None,
                             mode="real", timeout=1)
    assert row["outcome"] == "pass"
    err = capsys.readouterr().err
    assert "client-0123456789ab" in err and "lpc_secret" not in err


def client_look(monkeypatch, actions, tasks, owners, conversation="cli-1"):
    class Tasks:
        def get(self, path, **query):
            return {"tasks": [{"task_id": t} for t in tasks]}

    monkeypatch.setattr(dialogs, "_trail", lambda *a, **k: actions)
    monkeypatch.setattr(dialogs, "_task_owners", lambda url, ids: {i: owners[i] for i in ids if i in owners})
    return dialogs.Look(Tasks(), None, "ok", "disposable", 0, frozenset(), False, conversation=conversation)


def test_by_the_client_wants_a_client_on_every_write_and_on_the_new_tasks(monkeypatch):
    write = {"tool": "looper_task_create", "risk": "write", "status": "ok", "client_id": "client-1"}
    read = {"tool": "looper_tasks", "risk": "read", "status": "ok", "client_id": None}
    owner = {"client_id": "client-1", "origin_thread": "cli-1"}
    assert dialogs._by_the_client(client_look(monkeypatch, [write, read], ["t1"], {"t1": owner})) is None
    assert "no API client" in dialogs._by_the_client(
        client_look(monkeypatch, [{**write, "client_id": None}], [], {}))
    assert "no client_id" in dialogs._by_the_client(
        client_look(monkeypatch, [write], ["t1"], {"t1": {**owner, "client_id": None}}))
    assert "cli-2" in dialogs._by_the_client(
        client_look(monkeypatch, [write], ["t1"], {"t1": {**owner, "origin_thread": "cli-2"}}))
    assert dialogs._both(lambda look: None, lambda look: "second")(None) == "second"
    assert dialogs._both(lambda look: "first", lambda look: "second")(None) == "first"
