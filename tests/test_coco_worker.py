"""Coco Worker launches through local stand-ins, without the installed CLI or login."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import agent_settings
from looper.agents.agent_settings import AgentSettings
from looper.agents.llmrec import control
from looper.agents.profile_registry import Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType, adapter, coco_login
from looper.agents.tiers import worker
from test_agent_planner_worker import _step_envelope, _worker_row

FIXTURE = Path(__file__).parent / "fixtures" / "coco_exec" / "sample.jsonl"
MODEL = "GPT-5.5"
MODEL_ERROR = ("Error: thread/start: thread/start failed: model 'GPT-5.5' is unavailable because "
               "its metadata could not be resolved (code -32600)")


def _settings(executable: str) -> AgentSettings:
    runtime = adapter("coco")
    provider = ProviderSpec("prov-coco-login", "Coco login", VendorType.COCO_LOGIN, AuthKind.OAUTH, "coco")
    model = ModelSpec("model-coco", provider.id, MODEL, ApiProtocol.OPENAI_RESPONSES)
    resolved = ResolvedModel(model, provider, Endpoint(provider.id, model.protocol, ""))
    profile = ResolvedAgentProfile("agent-coco", "Coco worker",
        RuntimeSpec("rt-coco", runtime.label, "coco", executable, runtime.capabilities),
        model.id, resolved_model=resolved)
    return AgentSettings(profile=profile)


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stream: str = "", fail: bool = False,
             logged_in: bool = True, catalog_failure: str | None = None) -> Path:
    capture = tmp_path / "run.json"
    executable = fake_claude(tmp_path / "coco", f'''
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ['login', 'status']:
    print({'Logged in using Trae' if logged_in else 'Not logged in'!r})
    raise SystemExit({0 if logged_in else 1})
if sys.argv[1:] == ['models', '--json']:
    print({'invalid JSON' if catalog_failure == 'invalid-json' else '[{"name":"GPT-5.5","config_name":"gpt-5.5"}]'!r})
    raise SystemExit({1 if catalog_failure == 'failed-command' else 0})
if not Path(os.environ['TRAE_HOME'], 'cli', 'auth.json').is_symlink():
    raise SystemExit(3)
prompt = sys.stdin.read()
if not prompt:
    raise SystemExit(3)
Path({str(capture)!r}).write_text(json.dumps({{'prompt': prompt, 'home': os.environ['TRAE_HOME']}}))
if {fail!r}:
    print({MODEL_ERROR!r}, file=sys.stderr)
    raise SystemExit(1)
print({stream!r}, end='')
''')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "operator-coco"))
    monkeypatch.setattr(coco_login, "recorded_problem", AsyncMock(return_value=None))
    coco_login.forget_probe()
    coco_login.forget_models()
    settings = _settings(executable)

    async def load(role: Any, *args: Any, **kwargs: Any) -> AgentSettings:
        return await agent_settings._check_profile_settings(settings, role)

    from looper.db import profile_registry

    monkeypatch.setattr(profile_registry, "list_models", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "load_agent_settings", load)
    monkeypatch.setattr(control, "_control", control.ProxyControl(tmp_path / "runtime", "", enabled=True))
    _worker_row(monkeypatch, {"system_prompt": "ROLE", "timeout_seconds": 10, "max_turns": 0})
    return capture


def _run(tmp_path: Path) -> dict[str, Any]:
    work = tmp_path / "work"
    work.mkdir()

    async def run() -> dict[str, Any]:
        pool = worker.WorkerPool(config=worker.WorkerConfig(), runtime_root=tmp_path / "runtime")
        return await pool._execute(_step_envelope(work, verify=False), is_verify=False)

    try:
        return asyncio.run(asyncio.wait_for(run(), 20))
    finally:
        coco_login.forget_probe()
        coco_login.forget_models()


def test_worker_uses_coco_home_and_last_message_as_its_report(
    agent_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    # The recorded "done" is plain text; a Worker must still supply its JSON status report.
    final = json.dumps({"status": "completed", "changed_files": {}, "summary": "finished the step"})
    last_message = next(event["item"] for event in reversed(events)
                        if event.get("item", {}).get("type") == "agent_message")
    last_message["text"] = final
    capture = _prepare(tmp_path, monkeypatch, stream="\n".join(json.dumps(event) for event in events) + "\n")
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    path_ran = tmp_path / "path-coco-ran"
    fake_claude(path_bin / "coco", (
        "from pathlib import Path\n"
        f"Path({str(path_ran)!r}).touch()\n"
        "print('Not logged in')\nraise SystemExit(1)\n"
    ))
    # The registered absolute path is outside PATH; neither probe may consult PATH's logged-out CLI.
    monkeypatch.setenv("PATH", str(path_bin) + os.pathsep + os.defpath)
    result = _run(tmp_path)
    assert result["status"] == "completed" and result["summary"] == "finished the step"
    assert not path_ran.exists()
    assert json.loads(capture.read_text())["prompt"]
    started = agent_db.start_record.await_args.kwargs["metadata"]
    assert started["profile"]["runtime"]["type"] == "coco" and started["commit_attribution"] == "trae"
    assert started["llmrec"]["reason"] == "not_covered"
    finished = agent_db.finish_record.await_args.kwargs
    assert finished["output_text"] == final and finished["error"] is None
    assert not (finished.get("metadata") or {}).get("model_reported")


@pytest.mark.parametrize("catalog_failure", [None, "failed-command", "invalid-json"])
def test_worker_keeps_coco_unknown_model_stderr_in_its_failure(
    agent_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_failure: str | None,
) -> None:
    _prepare(tmp_path, monkeypatch, fail=True, catalog_failure=catalog_failure)
    result = _run(tmp_path)
    assert result["status"] == "failed"
    assert result["error"] == "Coco (TRAE CLI) exited with code 1"
    assert result["stderr"].strip() == MODEL_ERROR


def test_logged_out_coco_is_refused_before_the_worker_starts(
    agent_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _prepare(tmp_path, monkeypatch, logged_in=False)
    status = coco_login.LoginStatus(coco_login.LoginState.NOT_LOGGED_IN, "not logged in")
    monkeypatch.setattr(coco_login, "cached_probe", lambda executable: status)
    result = _run(tmp_path)
    assert result["status"] == "failed" and "not logged in" in result["error"]
    assert "coco login" in result["error"] and not capture.exists()
    agent_db.start_record.assert_not_awaited()
