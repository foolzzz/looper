"""Manager-only eval selection at the CLI and registry boundary, without a server."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import dialogs, runner
from tests.test_model_eval_registration import RegistryApi


@pytest.mark.parametrize("option,value", [
    ("--manager-model", "opus"), ("--manager-effort", "high"), ("--register-agent", "all.json"),
])
def test_manager_agent_rejects_conflicting_options(option, value, capsys):
    for arguments in (["--manager-agent", "manager.json", option, value],
                      [option, value, "--manager-agent", "manager.json"]):
        with pytest.raises(SystemExit) as error:
            cli._parser().parse_args(["dialogs", *arguments])
        assert error.value.code == 2
        message = capsys.readouterr().err
        assert "--manager-agent" in message and option in message


def test_existing_manager_model_and_effort_can_still_be_combined():
    args = cli._parser().parse_args(["dialogs", "--manager-model", "opus", "--manager-effort", "xhigh"])
    assert (args.manager_model, args.manager_effort) == ("opus", "xhigh")


def test_manager_agent_cli_refuses_fake_mode_before_reading_spec(capsys):
    assert cli.main(["dialogs", "--mode", "fake", "--pg-url", "unused",
                     "--manager-agent", "missing.json"]) == 2
    assert "--manager-agent needs --mode real; the api runtime's fake runs are its own tests" in capsys.readouterr().err


def test_manager_agent_run_refuses_fake_mode_before_setup(tmp_path):
    with pytest.raises(runner.EvalError, match="--manager-agent needs --mode real"):
        dialogs.run(mode="fake", dialogs=(), admin_url="unused", out_dir=tmp_path / "results", manager_agent={})
    assert not (tmp_path / "results").exists()


class DialogApi(RegistryApi):
    def __init__(self):
        super().__init__()
        self.registry["runtimes"].append({"runtime_id": "rt-api", "name": "api", "type": "api", "builtin": True})
        runner.register_agent(self, {
            "runtime": {"type": "claude-code"}, "provider": {"provider_id": "prov-claude-subscription"},
            "model": {"name": "sonnet", "protocol": "anthropic-messages"},
            "agent": {"name": "claude-code/sonnet"},
        })
        self.posts.clear()
        self.roles = []

    def post(self, path, body):
        if path == "/api/v1/agent_settings/update_role":
            self.roles.append(deepcopy(body))
            return {"status": "ok", "result": {}}
        if path == "/api/v1/project_admin/create":
            return {"status": "ok", "result": {"project": {"project_id": "eval-project"}}}
        return super().post(path, body)


@pytest.mark.parametrize("runtime", ["api", "codex"])
@pytest.mark.parametrize("budget", [None, 0.17])
def test_manager_spec_registers_once_per_case_and_only_selects_the_manager(runtime, budget, monkeypatch, tmp_path):
    apis = {}
    expected_budget = 0.25 if budget is None else budget

    @contextmanager
    def database(_):
        api = DialogApi()
        name = str(id(api))
        apis[name] = api
        yield name, None

    @contextmanager
    def server(*, database_url, **_):
        yield apis[database_url]

    def answer(api, dialog, **_):
        # This is what every case observes before sending its first message.
        assert api.settings == [{"profile_id": "agent-0"}]
        assert api.roles == [{"role": "manager", "values": {"profile_id": "agent-1", "run_budget_usd": expected_budget}}]
        return {"id": dialog.id, "outcome": "pass", "success": True, "turns": [], "problems": []}

    monkeypatch.setenv("LOOPER_EVAL_API_KEY", "test-key")
    monkeypatch.setattr(dialogs, "fresh_database", database)
    monkeypatch.setattr(dialogs, "running_server", server)
    monkeypatch.setattr(dialogs, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(dialogs, "_usage", lambda _: (0.0, {}))
    monkeypatch.setattr(dialogs, "_git", lambda: {})
    monkeypatch.setattr(dialogs, "run_dialog", answer)
    spec_path = Path(f"evals/agents/{runtime}-cpa.example.json")
    spec = json.loads(spec_path.read_text())
    assert "api_key" not in spec["provider"]
    assert cli.main([
        "dialogs", "--mode", "real", "--pg-url", "unused", "--model", "sonnet",
        "--manager-agent", str(spec_path), "--dialogs", "status,ask_project",
        "--reps", "2", "--jobs", "2", "--out-dir", str(tmp_path),
        "--case-budget-usd", "1.0", "--total-cap-usd", "4",
        *(["--max-budget-usd", str(budget)] if budget is not None else []),
    ]) == 0
    result = json.loads(next(tmp_path.glob("*-dialogs-real.json")).read_text())
    assert result["aborted"] is None and not result["stopped_by_cap"]
    assert result["manager_model"] == spec["model"]["name"]
    assert result["manager_agent"] == spec["agent"]["name"]
    assert (result["agent"], result["model"]) == ("claude-code/sonnet", "sonnet")
    assert len(result["dialogs"]) == len(apis) == 4
    for api in apis.values():
        profiles = [body for path, body in api.posts if path == "/api/v1/profiles/create"]
        assert len(profiles) == 1 and profiles[0]["name"] == spec["agent"]["name"]
        assert api.settings == [{"profile_id": "agent-0"}]
        assert api.roles == [{"role": "manager", "values": {"profile_id": "agent-1", "run_budget_usd": expected_budget}}]
    assert "test-key" not in json.dumps(result)


@pytest.mark.parametrize("runtime,explicit_switch", [("api", False), ("codex", False), ("api", True)])
def test_real_profile_switch_keeps_the_manager_binding_unless_explicitly_overridden(
    runtime, explicit_switch, monkeypatch, tmp_path,
):
    api = DialogApi()

    @contextmanager
    def database(_):
        yield "unused", None

    @contextmanager
    def server(**_):
        yield api

    original_post = api.post

    def post(path, body):
        if path == "/api/v1/tasks/page":
            return {"status": "ok", "result": {"tasks": []}}
        return original_post(path, body)

    monkeypatch.setattr(api, "post", post)
    monkeypatch.setenv("LOOPER_EVAL_API_KEY", "test-key")
    monkeypatch.setattr(dialogs, "fresh_database", database)
    monkeypatch.setattr(dialogs, "running_server", server)
    monkeypatch.setattr(dialogs, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(dialogs, "_usage", lambda _: (0.0, {}))
    monkeypatch.setattr(dialogs, "_git", lambda: {})
    monkeypatch.setattr(dialogs, "_newest_run", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda _: None)
    spec = json.loads(Path(f"evals/agents/{runtime}-cpa.example.json").read_text())
    spec["agent"].update(agent_teams=False, custom_prompt="Keep the comparison instructions.")
    switch = {"runtime": {"type": "claude-code"},
              "provider": {"provider_id": "prov-claude-subscription"},
              "model": {"name": "opus", "protocol": "anthropic-messages"},
              "agent": {"name": "explicit switch", "effort": "xhigh"}} if explicit_switch else None
    # Exercise the real per-case setup and dialog seed without making model requests.
    case = replace(dialogs.select(["session_profile_switch"])[0], says=())
    result, _ = dialogs.run(mode="real", dialogs=(case,), admin_url="unused", out_dir=tmp_path,
                            model="sonnet", manager_agent=spec, switch_agent=switch)
    assert result["aborted"] is None
    assert result["dialogs"][0]["outcome"] == "pass"
    profiles = api.post("/api/v1/registry/view", {})["result"]["profiles"]
    selected = next(p for p in profiles if p["name"] == dialogs.SWITCH)
    manager = next(p for p in profiles if p["name"] == spec["agent"]["name"])
    if explicit_switch:
        assert selected["runtime_id"] == "rt-claude-code"
        assert selected["model"]["name"] == "opus" and selected["effort"] == "xhigh"
        assert selected["provider_id"] == "prov-claude-subscription"
    else:
        assert selected["profile_id"] != manager["profile_id"]
        for field in ("runtime_id", "provider_id", "model_id", "model", "effort", "agent_teams", "custom_prompt"):
            assert selected.get(field) == manager.get(field), field
        assert len(api.registry["providers"]) == 2 and len(api.registry["models"]) == 2
    assert api.roles == [{"role": "manager", "values": {"profile_id": manager["profile_id"], "run_budget_usd": None}}]
    assert api.settings == [{"profile_id": "agent-0"}]


@pytest.mark.parametrize("selected_id,recorded_id,ok", [
    ("agent-1", "agent-1", True),
    ("agent-0", "agent-1", False),
    (None, "agent-1", False),
    ("agent-1", "agent-0", False),
])
def test_switch_profile_check_uses_id_when_display_name_has_effort_suffix(
    selected_id, recorded_id, ok, monkeypatch,
):
    api = DialogApi()
    manager = {**api.registry["profiles"][0], "effort": "high"}
    dialogs._make_switch_profile(api, "eval-project", fake=False, manager_profile=manager)
    selected = {"profile_id": selected_id, "name": f"{dialogs.SWITCH} · high"} if selected_id else None
    monkeypatch.setattr(dialogs, "_selected", lambda _: selected)
    monkeypatch.setattr(dialogs, "_manager_runs", lambda _: [
        {"metadata": {"session_profile": {"to": recorded_id}}},
    ])
    look = dialogs.Look(api, None, "ok", "unused", 0, frozenset(), False)
    assert (dialogs._switch_selected(look) is None) is ok


@pytest.mark.parametrize("steps,completed,outcome", [
    ([{}], None, "fail"),  # No rows returned at all.
    ([{}], 0, "fail"),
    ([{}, {}], 1, "fail"),
    ([{}, {}], [2], "fail"),  # A later answer to identical text cannot fill turn 1.
    ([{}, {}], 2, "pass"),
    ([{"code_only": True}], None, "pass"),
    ([{"code_only": True}, {"stop": True}, {"withdraw": True}, {}], 1, "pass"),
])
def test_api_dialog_requires_completed_model_turns(steps, completed, outcome, monkeypatch):
    import asyncpg

    asked = 0
    model_steps = [i for i, flags in enumerate(steps, 1)
                   if not (flags.get("code_only") or flags.get("stop") or flags.get("withdraw"))]
    completed_steps = completed if isinstance(completed, list) else model_steps[:completed or 0]

    class Connection:
        async def fetch(self, query, *_):
            if "SELECT input_text" in query:
                return [{"input_text": "step", "id": i} for i in completed_steps]
            return [] if completed is None else [{
                "completed_turns": len(completed_steps), "turns": len(completed_steps),
                "stored_turns": len(completed_steps),
                "protocol": "openai-responses", "expected_protocol": "openai-responses", "has_items": True,
            }]

        async def close(self):
            pass

    async def connect(_):
        return Connection()

    class Api:
        def post(self, path, body):
            assert path == "/api/v1/tasks/page"
            return {"result": {"tasks": []}}

    def ask(*_, **__):
        nonlocal asked
        asked += 1
        return "ok"

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(dialogs, "_newest_run", lambda *_: asked)
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    monkeypatch.setattr(dialogs, "_ask", ask)
    case = dialogs.Dialog("readback", tuple(dialogs.Say("step", lambda _: None, **flags) for flags in steps))
    row = dialogs.run_dialog(Api(), case, database_url="unused", project_id="p", scenarios=None,
                             mode="real", timeout=1, manager_profile={"runtime_type": "api"})
    assert row["outcome"] == outcome, row
    if outcome == "fail":
        missing = next(i for i in model_steps if i not in completed_steps)
        assert row["problems"] == [
            f"no completed API Manager record for user turn {missing}: 'step'"]
    else:
        assert row["problems"] == []


@pytest.mark.parametrize("manager_runtime,switch_runtime,api_turns", [
    ("api", "codex", [5]),
    ("api", "api", [3, 5]),
    ("api", None, [3, 5]),  # The default real switch retains the Manager runtime.
    ("codex", "api", [3]),
    ("codex", "codex", []),
])
def test_switch_dialog_requires_only_api_answered_user_turns(
    manager_runtime, switch_runtime, api_turns, monkeypatch,
):
    import asyncpg

    case = dialogs.select(["session_profile_switch"])[0]
    # The profile behavior has its own checks; keep the actual switch points and readback here.
    case = replace(case, says=tuple(replace(say, check=lambda _: None, text_from=None) for say in case.says))
    reads = []
    asked = 0

    class Connection:
        async def fetch(self, query, *_):
            reads.append(query)
            if "SELECT input_text" in query:
                return [{"input_text": "[RESUME] Earlier context.\n\n" + case.says[i - 1].text, "id": i}
                        for i in api_turns]
            assert manager_runtime == "api", "retired API transcripts have no active API session"
            return [{"completed_turns": len(api_turns), "turns": len(api_turns), "stored_turns": len(api_turns),
                     "protocol": "openai-responses", "expected_protocol": "openai-responses", "has_items": True}]

        async def close(self):
            pass

    async def connect(_):
        return Connection()

    def ask(*_, **__):
        nonlocal asked
        asked += 1
        return "ok"

    api = DialogApi()
    post = api.post
    monkeypatch.setattr(api, "post", lambda path, body: {"result": {"tasks": []}}
                        if path == "/api/v1/tasks/page" else post(path, body))
    monkeypatch.setenv("LOOPER_EVAL_API_KEY", "test-key")
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(dialogs, "_newest_run", lambda *_: asked)
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    monkeypatch.setattr(dialogs, "_ask", ask)
    profile = {**api.registry["profiles"][0], "runtime_type": manager_runtime}
    switch = json.loads(Path(f"evals/agents/{switch_runtime}-cpa.example.json").read_text()) if switch_runtime else None
    row = dialogs.run_dialog(api, case, database_url="unused", project_id="p", scenarios=None,
                             mode="real", timeout=1, manager_profile=profile, switch_spec=switch)
    assert row["outcome"] == "pass", row
    assert len(row["turns"]) == 5 and row["problems"] == []
    assert bool(reads) == bool(api_turns)


def test_cpa_eval_instructions_distinguish_measured_gate_results_from_pending_checks():
    readme = Path("evals/README.md").read_text()
    cpa = readme.split("#### The api runtime on CPA\n", 1)[1].split("#### Pi\n", 1)[0]
    cpa = " ".join(cpa.split())
    # Measured results close D11 without turning harness errors or reruns into full-run passes.
    for measured in (
        "`gpt-6-astra` for both Manager specs",
        "other roles used Claude Code sonnet. Full-dialog runs used `--reps 2 --jobs 2`.",
        "one rep | 4/4 pass | $0.44 |",
        "Codex Manager control, all dialogs | 78/84 scored passes, plus 4 harness errors "
        "(`stop_then_continue` and `withdraw_then_continue`, both reps) | $13.68 |",
        "api Manager, all dialogs | 82/88 passes | $9.06 |",
        "On dialogs scored by both runs, recorded cost was $6.82 for Codex and $4.53 for api.",
        "D11 is met: every dialog's api pass count was at least the control's minus one.",
        "those control errors remain errors.",
        "`cancel_confirmation`, `config_tools`, `provider_key`, every `schedule_*` dialog and "
        "`client_relay_cancel` passed 2/2.",
        "that dialog passed 2/2 for both Managers; those focused reruns are separate from "
        "the full-run totals above.",
    ):
        assert measured in cpa
    assert "D11 remains unverified" not in cpa
    # The dialog scores and costs do not establish these independent capability checks.
    assert (
        "Per-request token deltas, session-item and `api_sessions.protocol` readbacks, and the Opus "
        "descriptive comparison are not established by these measurements. Remaining capability "
        "checks are listed in RFC §8.4 and §15."
    ) in cpa
    assert "This full comparison does not replace the focused real Claude CLI handoff-prompt comparison." in cpa
