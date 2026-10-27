"""Manager-only eval selection at the CLI and registry boundary, without a server."""

from contextlib import contextmanager
from copy import deepcopy
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
def test_manager_spec_registers_once_per_case_and_only_selects_the_manager(runtime, monkeypatch, tmp_path):
    apis = {}

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
        assert api.roles == [{"role": "manager", "values": {"profile_id": "agent-1"}}]
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
        assert api.roles == [{"role": "manager", "values": {"profile_id": "agent-1"}}]
    assert "test-key" not in json.dumps(result)
