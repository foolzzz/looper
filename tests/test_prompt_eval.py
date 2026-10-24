"""The prompt regression eval (``evals/prompt_regression``): its own contract, and the task set in fake mode.

The fake-mode run needs PostgreSQL: it runs only when LOOPER_TEST_DATABASE_URL
points at a server where it may create (and then drop) a database of its own.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals.prompt_regression import __main__ as cli
from evals.prompt_regression import compare, fake_claude, metrics, runner
from evals.prompt_regression import tasks as task_list
from evals.prompt_regression.dialogs import DIALOGS as MANAGER_DIALOGS
from evals.prompt_regression.tasks import TASKS, EvalTask
from looper.agents import agent_settings, profile_registry
from looper.agents.runtimes import ApiProtocol, AuthKind
from looper.agents.runtimes.pi import PiRuntime, PiStream
from looper.agents.tiers.planner import PlannerAgent, PlannerTurnError
from looper.agents.tiers.planner_protocol import TurnKind, WaitPolicy, parse_plan, parse_vault
from looper.agents.tiers.worker import WorkerConfig, WorkerPool, judge_result
from looper.agents.waits import WaitLimits
from tests.db_support import DSN


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


# --- Contract violations, read where the agent system rejects a reply ------------------------------------


def _planner_activity(tmp_path: Path, replies: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    """The activity the real ``PlannerAgent._ask`` logs for these replies, and the error it raised, if any."""
    planner = PlannerAgent(task_id="task-0123456789ab", runtime_root=tmp_path)
    activity: list[dict[str, Any]] = []
    answers = iter(replies)

    async def turn(_message: str) -> str:
        return next(answers)

    async def log(kind: str, summary: str, **_: Any) -> None:
        activity.append({"kind": kind, "summary": summary})

    planner._turn = turn  # type: ignore[method-assign]
    planner._activity = log  # type: ignore[method-assign]
    try:
        asyncio.run(planner._ask(TurnKind.PLAN, "[PLAN] plan it", parse_plan))
    except PlannerTurnError as exc:
        # Every caller logs the error once, as an "error" activity that quotes it.
        activity.append({"kind": "error", "summary": f"Task failed: Planning failed: {exc}"})
        return activity, str(exc)
    return activity, None


_PLAN = '```json\n{"steps": [{"id": "s1", "instruction": "do it", "acceptance_criteria": ["done"], ' \
        '"depends_on": [], "review": false}]}\n```'


@pytest.mark.parametrize(("replies", "expected"), [
    ([_PLAN], 0),
    (["no json block", _PLAN], 1),
    (["no json block", "still none"], 2),
])
def test_planner_violations_count_the_replies_its_parser_rejected(
    tmp_path: Path, replies: list[str], expected: int,
) -> None:
    activity, _ = _planner_activity(tmp_path, replies)
    activity.append({"kind": "phase", "summary": "Plan created: 1 step(s)"})
    assert metrics.planner_violations(activity) == expected


def test_flow_problems_check_the_least_and_the_most() -> None:
    expect = {"min_steps": 2, "max_plan_changes": 0}
    assert metrics.flow_problems(expect, {"steps": 2, "plan_changes": 0}) == []
    assert len(metrics.flow_problems(expect, {"steps": 1, "plan_changes": 1})) == 2


@pytest.mark.parametrize(("text", "is_verify"), [
    ('Done.\n{"status": "completed", "changed_files": {}, "deleted_files": [], "summary": "ok"}', False),
    ('{"status": "failed", "changed_files": {}, "deleted_files": [], "summary": "could not"}', False),
    ("I did it, the file is there.", False),
    ('{"status": "done", "changed_files": {}, "deleted_files": []}', False),
    ('{"status": "completed", "changed_files": ["a.txt"], "deleted_files": []}', False),
    ('{"passed": true, "findings": [], "summary": "ok"}', True),
    ('{"passed": false, "findings": ["x"], "summary": "no"}', True),
    ("Looks good to me.", True),
    ('{"passed": "yes"}', True),
])
def test_report_violation_agrees_with_judge_result(tmp_path: Path, text: str, is_verify: bool) -> None:
    judged = judge_result({"type": "result", "subtype": "success", "result": text}, work_dir=tmp_path,
                          is_verify=is_verify)
    format_failures = ("Worker did not end with the JSON status report", "Reviewer did not end with the JSON verdict",
                       "Worker reported an unknown status", "Worker report has malformed")
    rejected = any(str(judged.get("summary") or judged.get("error") or "").startswith(prefix)
                   for prefix in format_failures)
    assert metrics.report_violation(text, is_verify=is_verify) is rejected


# --- The Planner's own file changes ---------------------------------------------------------------------


def test_edit_tool_paths_are_relative_and_never_local_paths(tmp_path: Path) -> None:
    def tool(name: str, path: str) -> dict[str, Any]:
        return {"event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": {"file_path": path}}]}}}

    patch = {"event": {"type": "assistant", "message": {"content": [  # a Codex patch, naming each file it changes
        {"type": "tool_use", "name": "apply_patch", "input": {"changes": [
            {"path": str(tmp_path / "e.txt"), "kind": "add"}, {"path": str(tmp_path / "f.txt"), "kind": "update"}]}}]}}}
    def omp(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:  # OMP's tools, by their own arguments
        return {"event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": tool_input}]}}}

    events = [tool("Write", str(tmp_path / "a.txt")), tool("Read", str(tmp_path / "b.txt")),
              tool("Edit", "sub/c.txt"), tool("Write", "/elsewhere/secret/d.txt"), patch,
              omp("write", {"path": "g.txt", "content": "x"}), omp("write", {"path": "agent://Helper"}),
              omp("edit", {"input": "[h.txt#A1B2]\nPUT 1:\n+x\n"}), omp("read", {"path": "i.txt"})]
    assert metrics.edit_tool_paths(events, tmp_path) == [
        "a.txt", "sub/c.txt", "<outside the working directory>/d.txt", "e.txt", "f.txt", "g.txt", "h.txt"]


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize("location", ["relative", "absolute", "outside"])
@pytest.mark.parametrize("failed", [False, True])
def test_pi_file_edits_reach_the_planners_metrics(
    tmp_path: Path, tool: str, location: str, failed: bool,
) -> None:
    path = {"relative": "sub/plan.md", "absolute": str(tmp_path / "sub/plan.md"),
            "outside": "/elsewhere/private/plan.md"}[location]
    arguments = {"path": path, **({"content": "new"} if tool == "write" else
                                 {"oldText": "old", "newText": "new"})}
    stream = PiStream(model="selected")
    raw = [
        {"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "edit-1", "name": tool, "arguments": arguments}]}},
        {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "edit-1",
            "isError": failed, "content": [{"type": "text", "text": "denied" if failed else "done"}]}},
    ]
    events = [{"event": event} for row in raw for event in stream.translate(row)]
    expected = [] if failed else ["<outside the working directory>/plan.md" if location == "outside"
                                 else "sub/plan.md"]
    assert metrics.edit_tool_paths(events, tmp_path) == expected
    # No filesystem diff can hide a missed tool: the actual task metric must see the event alone.
    result = metrics.task_metrics(
        detail={"status": "completed", "steps": []}, activity=[],
        records=[_record(1, "planner-task-1")], events={1: events}, changes={}, work_dir=tmp_path,
        check_problem=None, expect={"max_planner_modified_files": 0}, now=0.0)
    assert result["planner_edit_tool_paths"] == expected
    assert result["planner_modified_files"] == int(not failed)
    assert bool(result["problems"]) is (not failed)


# --- Explicit eval Agent Profiles ----------------------------------------------------------------------


_PI_SPECS = [("pi-cpa.example.json", "openai-responses", "gpt-6.1-sol"),
             ("pi-deepseek.example.json", "openai-chat", "deepseek-flash")]


@pytest.fixture
def eval_registry(tmp_path: Path) -> Any:
    """In-memory persistence and scripted Pi subprocesses; registration, settings and adapters are real."""
    from fake_claude import fake_claude as executable

    pi = executable(tmp_path / "pi", """import json, sys
from pathlib import Path
role = sys.stdin.read()
if role == 'worker':
    Path('done.txt').write_text('done\\n')
reply = {'worker': json.dumps({'status': 'completed', 'changed_files': {'done.txt': 'done'},
                              'deleted_files': [], 'summary': 'ok'}),
         'reviewer': json.dumps({'passed': True}), 'planner': 'planned', 'manager': 'ok'}[role]
model = sys.argv[sys.argv.index('--model') + 1]
print(json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'model': model,
                 'content': [{'type': 'text', 'text': reply}], 'stopReason': 'stop'}}))
print(json.dumps({'type': 'agent_settled', 'aborted': False}))
""")

    class Registry:
        def __init__(self) -> None:
            self.providers: dict[str, Any] = {}
            self.endpoints: dict[tuple[str, ApiProtocol], Any] = {}
            self.models: dict[str, Any] = {}
            self.profiles = [{"profile_id": "builtin", "name": "claude-code/opus", "builtin": True}]
            self.layers: dict[Any, Any] = {}
            self.posts: list[tuple[str, Any]] = []
            self.launches: list[tuple[str, str, str]] = []
            self.records: list[dict[str, Any]] = []
            self.events: dict[int, list[dict[str, Any]]] = {}
            self.messages: list[dict[str, Any]] = []
            self.work = tmp_path
            self.runtime = profile_registry.RuntimeSpec("rt-pi", "pi", "pi", pi, PiRuntime.capabilities)

        def launch(self, role: str) -> str:
            kind = agent_settings.AgentType(role)
            launch = agent_settings.Launch(agent_settings.resolve(kind, self.layers), role=kind)
            prompt = tmp_path / "prompt.md"
            prompt.write_text("Disposable eval stand-in", encoding="utf-8")
            command = launch.runtime.run_command(launch.executable("must-not-run-claude"), cwd=self.work,
                                                 prompt_file=prompt, launch=launch)
            done = subprocess.run(command, cwd=self.work, env=launch.environment(), input=role,
                                  capture_output=True, text=True, check=True, timeout=10)

            async def read() -> list[dict[str, Any]]:
                stdout = asyncio.StreamReader()
                stdout.feed_data(done.stdout.encode())
                stdout.feed_eof()
                return [event async for event in launch.runtime.parse(stdout, model=launch.model)]

            events = asyncio.run(read())
            record_id = len(self.records) + 1
            self.records.append({**_record(record_id, f"{role}-eval", step_id="s1" if role == "worker" else None),
                                 "model": launch.model})
            self.events[record_id] = [{"seq": i, "event": event} for i, event in enumerate(events, 1)]
            self.launches.append((role, launch.runtime.type, launch.model))
            return next(event["result"] for event in events if event["type"] == "result")

        def get(self, path: str, **_: Any) -> dict[str, Any]:
            if path == "/api/registry":
                return {"runtimes": [{"runtime_id": "rt-claude-code", "name": "claude-code",
                                      "type": "claude-code", "builtin": True},
                                     {"runtime_id": "rt-pi", "name": "pi", "type": "pi", "builtin": True}],
                        "providers": [], "profiles": self.profiles,
                        "models": [{"model_id": resolved.model.id, "provider_id": resolved.provider.id,
                                    "name": resolved.model.name, "protocol": resolved.model.protocol.value}
                                   for resolved in self.models.values()]}
            if path == "/api/tasks":
                return {"tasks": []}
            if path == "/api/tasks/task-eval":
                return {"status": "completed", "steps": [{"id": "s1"}]}
            if path == "/api/tasks/task-eval/activity":
                return {"activity": []}
            if path == "/api/llm-records":
                return {"records": self.records}
            if path.endswith("/events"):
                return {"events": self.events[int(path.split("/")[-2])]}
            assert path == "/api/chat/messages", path
            return {"messages": self.messages}

        def post(self, path: str, body: Any) -> dict[str, Any]:
            if path == "/api/v1/agent_settings/update_system":
                body = body["values"]
                profile = next(p for p in self.profiles if p["profile_id"] == body["profile_id"])
                chosen = profile_registry.ResolvedAgentProfile(profile["profile_id"], profile["name"], self.runtime,
                    profile["model_id"], resolved_model=self.models[profile["model_id"]])
                profile_registry.check_roles(chosen, [role.value for role in agent_settings.AgentType])
                self.layers[agent_settings.Layer.SYSTEM, None] = agent_settings.LayerValues(profile=chosen)
                return {"status": "ok", "result": {}}
            self.posts.append((path, body))
            if path == "/api/providers":
                assert "api_key_env" not in body
                fields, key = profile_registry._provider_fields(body, None)
                account = profile_registry.provider_spec({**fields, "id": "provider-eval"})
                self.providers[account.id] = dataclasses.replace(account, api_key=key)
                for entry in fields["endpoints"]:
                    endpoint = profile_registry.Endpoint(account.id, ApiProtocol(entry["protocol"]), entry["base_url"])
                    self.endpoints[account.id, endpoint.protocol] = endpoint
                return {"provider": {"provider_id": account.id}}
            if path == "/api/models":
                model = profile_registry.ModelSpec.from_entry({**body, "id": f"model-{len(self.models)}"})
                resolved = profile_registry.ResolvedModel(model, self.providers[model.provider_id],
                                                          self.endpoints[model.provider_id, model.protocol])
                self.models[model.id] = resolved
                return {"model": {"model_id": model.id, "provider_id": model.provider_id,
                                  "name": model.name, "protocol": model.protocol.value}}
            if path == "/api/agent-profiles":
                assert body["runtime_id"] == self.runtime.id
                resolved = self.models[body["model_id"]]
                resolved.check_runtime(self.runtime, body.get("effort"))
                profile = {"profile_id": "profile-eval", **body,
                           "model": {"model_id": resolved.model.id, "provider_id": resolved.provider.id,
                                     "name": resolved.model.name, "protocol": resolved.model.protocol.value}}
                self.profiles.append(profile)
                return {"profile": profile}
            if path == "/api/projects":
                self.work = Path(body["work_dir"])
                return {"project_id": "project-eval"}
            if path == "/api/tasks/create":
                self.work = Path(body["project"]["work_dir"])
                return {"task_id": "task-eval", "project_id": "project-eval"}
            if path == "/api/tasks/start":
                for role in ("planner", "worker", "reviewer"):
                    self.launch(role)
                return {}
            assert path == "/api/chat/send", path
            self.messages.append({"message_id": f"{body['message_id']}:reply", "content": self.launch("manager")})
            return {}

    return Registry()


@pytest.mark.parametrize(("filename", "protocol", "model"), _PI_SPECS)
def test_pi_eval_specs_register_without_exposing_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    eval_registry: Any, filename: str, protocol: str, model: str,
) -> None:
    spec_path = runner.REPO_ROOT / "evals" / "agents" / filename
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["runtime"] == {"type": "pi"}
    assert spec["provider"]["auth"] == "api_key"
    assert spec["provider"]["protocols"] == [protocol]
    assert spec["agent"]["model"] == model
    assert "api_key" not in spec["provider"]
    assert spec["provider"]["base_url"] == ("http://127.0.0.1:8080/v1" if protocol == "openai-responses"
                                               else "https://api.deepseek.com/v1")
    original = json.dumps(spec)
    secret = "sk-disposable-eval-secret"
    variable = spec["provider"]["api_key_env"]
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(runner.EvalError, match=variable):
        runner.register_agent(eval_registry, spec)
    assert eval_registry.posts == []
    monkeypatch.setenv(variable, secret)
    registered = runner.register_agent(eval_registry, spec)
    chosen = runner.select_agent(eval_registry, agent=registered["name"], model=None)
    assert chosen["runtime_id"] == "rt-pi"
    assert chosen["model"] == {"model_id": chosen["model_id"], "provider_id": "provider-eval",
                               "name": model, "protocol": protocol}
    account = eval_registry.providers["provider-eval"]
    assert account.api_key == secret
    assert eval_registry.endpoints[account.id, ApiProtocol(protocol)].base_url == spec["provider"]["base_url"]
    assert list(eval_registry.models) == [chosen["model_id"]]
    assert eval_registry.models[chosen["model_id"]].model.protocol is ApiProtocol(protocol)
    assert json.dumps(spec) == original  # adding the key must not mutate a shareable spec
    report = {"agent": chosen, "tasks": []}
    saved = tmp_path / "result.json"
    runner.write_result(report, saved, roots=(tmp_path,), rows="tasks")
    print(compare.markdown({"mode": "fake", "agent": chosen["name"], "git": {}, "tasks": []}))
    captured = capsys.readouterr()
    assert secret not in saved.read_text() + captured.out + captured.err + repr(chosen)


def test_registering_pi_without_its_runtime_does_not_fall_back_to_claude(
    monkeypatch: pytest.MonkeyPatch, eval_registry: Any,
) -> None:
    registry = eval_registry.get("/api/registry")
    registry["runtimes"] = [runtime for runtime in registry["runtimes"] if runtime["type"] != "pi"]
    monkeypatch.setattr(eval_registry, "get", lambda _: registry)
    spec = json.loads((runner.REPO_ROOT / "evals/agents/pi-cpa.example.json").read_text(encoding="utf-8"))
    with pytest.raises(runner.EvalError, match="no 'pi' runtime"):
        runner.register_agent(eval_registry, spec)
    assert eval_registry.posts == [] and eval_registry.launches == []


@pytest.mark.parametrize(("filename", "protocol", "model"), _PI_SPECS)
@pytest.mark.parametrize("suite", ["task", "dialog"])
def test_registered_pi_runs_a_fake_eval_task_or_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eval_registry: Any,
    capsys: pytest.CaptureFixture[str], filename: str, protocol: str, model: str, suite: str,
) -> None:
    from evals.prompt_regression import dialogs

    spec = json.loads((runner.REPO_ROOT / "evals" / "agents" / filename).read_text(encoding="utf-8"))
    secret = "sk-disposable-eval-secret"
    monkeypatch.setenv(spec["provider"]["api_key_env"], secret)

    @contextmanager
    def database(_: str) -> Any:
        yield "disposable", 0.0

    @contextmanager
    def server(**_: Any) -> Any:
        yield eval_registry

    harness = runner if suite == "task" else dialogs
    monkeypatch.setattr(harness, "fresh_database", database)
    monkeypatch.setattr(harness, "running_server", server)
    monkeypatch.setattr(harness, "_git", lambda: {})
    if suite == "task":
        task = EvalTask("pi_smoke", "Create done.txt.", lambda work: None if
                        (work / "done.txt").read_text() == "done\n" else "wrong file", expect={"min_reviews": 1})
        result, path = runner.run(mode="fake", tasks=(task,), register=spec,
                                  admin_url="disposable", out_dir=tmp_path / "results", task_timeout=10)
        roles = ["planner", "worker", "reviewer"]
    else:
        monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
        monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
        monkeypatch.setattr(dialogs, "_usage", lambda *_: (0.0, {
            "manager": sorted({record["model"] for record in eval_registry.records})}))
        dialog = dialogs.Dialog("pi_smoke", (dialogs.Say("Status?", lambda look: None if look.reply == "ok"
                                                        else "wrong reply"),))
        result, path = dialogs.run(mode="fake", dialogs=(dialog,), register=spec,
                                   admin_url="disposable", out_dir=tmp_path / "results", timeout=10)
        roles = ["manager"]
        assert result["manager_agent"] == spec["agent"]["name"]
    assert result["aborted"] is None
    (row,) = result["tasks" if suite == "task" else "dialogs"]
    assert row["outcome"] == "pass", row
    assert row["models"] == {role: [model] for role in roles}
    assert eval_registry.launches == [(role, "pi", model) for role in roles]
    assert result["agent"] == spec["agent"]["name"]
    captured = capsys.readouterr()
    assert secret not in path.read_text(encoding="utf-8") + captured.out + captured.err


def test_changed_files_are_the_planners_only_while_no_step_ran() -> None:
    windows = {"planner": [(100.0, 110.0), (130.0, 140.0)], "worker": [(110.05, 129.0)],
               "reviewer": [(141.0, 150.0)]}
    # Written as a Planner turn starts, which the measured clock offset may put up to the tolerance before it.
    changes = {"in-plan-turn.txt": 105.0, "at-plan-turn-start.txt": 129.95, "before-plan-turn.txt": 129.85,
               "by-worker.txt": 120.0, "near-worker-start.txt": 109.98, "after-all.txt": 160.0}
    assert metrics.planner_changed_files(changes, windows) == ["at-plan-turn-start.txt", "in-plan-turn.txt"]
    # The database clock runs 50 s ahead of the local one: the windows move back by 50 s.
    shifted = {name: [(a + 50, b + 50) for a, b in spans] for name, spans in windows.items()}
    assert metrics.planner_changed_files(changes, shifted, clock_offset=50.0) == [
        "at-plan-turn-start.txt", "in-plan-turn.txt"]


def test_files_a_steps_merge_left_are_not_the_planners(tmp_path: Path) -> None:
    # Looper merges an accepted step's worktree during or just after a Planner turn: what it wrote is not the
    # Planner's, unless something changed it again afterwards.
    work = tmp_path / "work"
    work.mkdir()
    (work / "start.py").write_text("start\n")
    runner.init_repository(work)

    def git(*args: str) -> str:
        return runner._git_in(work, *args)

    git("checkout", "-q", "-b", "step")
    for name in ("merged.py", "changed-later.py"):
        (work / name).write_text("the step's\n")
    git("add", "-A")
    git("commit", "-q", "-m", "the step's work")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "step", "-m", "Merge s1\n\nLooper-Task: task-1\nLooper-Step: s1")
    (work / "changed-later.py").write_text("written after the merge\n")
    (work / "untracked.txt").write_text("new\n")
    assert runner.merged_by_looper(work, "task-1") == {"merged.py"}
    assert runner.merged_by_looper(work, "task-2") == set()  # another task's merges are not this one's


@pytest.mark.parametrize(("depends_on", "pairs"), [
    ({"s1": [], "s2": [], "s3": ["s1", "s2"], "s4": ["s3"]}, 1),  # s1 and s2 only
    ({"s1": [], "s2": ["s1"], "s3": ["s2"]}, 0),  # a chain: s3 needs s1 through s2
    ({"s1": [], "s2": [], "s3": []}, 3),
], ids=["diamond", "chain", "flat"])
def test_independent_pairs_count_steps_with_no_dependency_path(depends_on: dict[str, list[str]], pairs: int) -> None:
    steps = [{"id": step_id, "depends_on": needs} for step_id, needs in depends_on.items()]
    assert metrics.independent_pairs(steps) == pairs


@pytest.mark.parametrize(("depends_on", "pairs"), [
    ({"api": [], "server": ["api"], "client": ["api"], "check": ["server", "client"]}, 1),
    ({"api": [], "server": ["api"], "client": ["api"]}, 0),  # no step brings the two together
    ({"server": [], "client": [], "check": ["server", "client"]}, 0),  # no interface they share
], ids=["interface first", "no integration", "no interface"])
def test_bracketed_pairs_need_a_shared_step_before_and_a_step_after(depends_on: dict[str, list[str]],
                                                                    pairs: int) -> None:
    steps = [{"id": step_id, "depends_on": needs} for step_id, needs in depends_on.items()]
    assert metrics.bracketed_pairs(steps) == pairs


@pytest.mark.parametrize(("steps", "dependents"), [
    # An interface step every platform step (each as its role) depends on, and an integration after them.
    ({"api": ([], None), "be": (["api"], "backend_rd"), "web": (["api"], "fe_rd"), "and": (["api"], "android_rd"),
      "check": (["be", "web", "and"], None)}, 3),
    # The back end pins the interface itself: the clients depend on it, the back end on nothing.
    ({"be": ([], "backend_rd"), "web": (["be"], "fe_rd"), "and": (["be"], "android_rd")}, 2),
    ({"be": ([], "backend_rd"), "web": ([], "fe_rd")}, 0),  # nothing shared
    ({"api": ([], None), "be": (["api"], None), "web": (["api"], None)}, 0),  # no step runs as a role
], ids=["interface step", "back end first", "flat", "no roles"])
def test_interface_dependents_count_the_role_steps_built_on_one_step(steps: dict[str, Any], dependents: int) -> None:
    rows = [{"id": step_id, "depends_on": needs, "role_key": role, "status": "verified"}
            for step_id, (needs, role) in steps.items()]
    assert metrics.interface_dependents(rows) == dependents
    assert metrics.interface_dependents([{**row, "status": "skipped"} for row in rows]) == 0


def test_a_discipline_counts_once_it_has_a_role_of_its_own_that_names_its_conventions() -> None:
    web = metrics.Discipline("web", ("web", "fe"), ("fetch", "dom", "es module"))
    ios = metrics.Discipline("ios", ("ios",), ("swift", "codable", "struct"))

    def role(role_id: str, prompt: str, kind: str = "work") -> dict[str, str]:
        return {"id": role_id, "kind": kind, "role_prompt": prompt}

    strong = [role("fe_rd", "An ES module that uses fetch."), role("ios_rd", "Swift: a Codable struct.")]
    assert metrics.discipline_roles(strong, (web, ios)) == 2
    assert metrics.discipline_roles([role("fe_rd", "Write the web client.")], (web, ios)) == 0  # no convention
    assert metrics.discipline_roles([role("fe_rd", "Uses fetch.")], (web,)) == 0  # one is not enough
    assert metrics.discipline_roles([role("fe_ios_rd", "fetch, the DOM; Swift, Codable")], (web, ios)) == 0
    assert metrics.discipline_roles([role("fe_review", "fetch and the DOM", "review")], (web,)) == 0
    assert metrics.discipline_roles(strong, ()) == 0


@pytest.mark.parametrize(("code", "found"), [
    ("k3v9q2x7m4p8w1z6r5t0", False),
    ("k3v9q2x7m4p8w1z6r5t1", True),  # a different digest
    ("482913", True),  # too short
    ("K3V9Q2X7M4P8W1Z6R5T0", True),  # upper case
    (None, True),  # missing
])
def test_the_consult_check_takes_only_the_code_whose_digest_the_first_step_wrote(
        tmp_path: Path, code: str | None, found: bool) -> None:
    import hashlib

    task = next(t for t in TASKS if t.id == "room_consult")
    (tmp_path / "code.sha256").write_text(hashlib.sha256(b"k3v9q2x7m4p8w1z6r5t0").hexdigest() + "\n")
    if code is not None:
        (tmp_path / "code.txt").write_text(code + "\n")
    assert (task.check(tmp_path) is not None) is found
    # The code is out of reach of trying every one against its digest (a 6-digit number's took a second), so only
    # the first step's session can give it; the requirement asks for that code.
    assert len(task_list.CODE_ALPHABET) ** task_list.CODE_LENGTH >= 2 ** 100
    assert f"{task_list.CODE_LENGTH} lowercase letters and digits" in task.requirement


@pytest.mark.parametrize(("delivery", "fields"), [
    ("empty", ("name", "count")),
    ("answer_only", ("name", "count")),
    ("interface", ("name", "count")),
    ("interface", ("label", "quantity")),
])
def test_fake_room_consumer_needs_the_delivered_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery: str, fields: tuple[str, str],
) -> None:
    task = task_list.select(["room_interface"])[0]
    script = task.fake
    name, count = fields
    store = script["attempts"]["s1"][0]["files"]["store.py"]
    (tmp_path / "store.py").write_text(store.replace('"name"', repr(name)).replace('"count"', repr(count)))
    messages = [] if delivery == "empty" else [{"kind": "answer", "body": "Yes: only the id."}]
    if delivery == "interface":
        messages.insert(0, {"kind": "interface", "body": f'add_item takes {{"{name}": str, "{count}": int}}'})

    def rpc(_method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = {"messages": messages, "more": False, "unread": {}} if params["name"] == "looper_room_inbox" else {}
        return {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps({"status": "ok", "result": result})}],
            "isError": False}}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fake_claude, "_mcp", lambda _args: rpc)
    monkeypatch.setattr(fake_claude.time, "sleep", lambda _seconds: None)
    fake_claude._worker(script, "Your step: s2", SimpleNamespace(readline=lambda: "start"), [])
    problem = task.check(tmp_path)
    if delivery == "interface":
        assert problem is None
    else:
        assert problem is not None


@pytest.mark.parametrize("code", ["k3v9q2x7m4p8w1z6r5t0", "k3v9q2x7m4p8w1z6r5t1"])
@pytest.mark.parametrize("delivery", ["consult", "answer", "empty"])
def test_fake_room_consumer_writes_only_the_delivered_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, delivery: str,
) -> None:
    task = task_list.select(["room_consult"])[0]
    script = {**task.fake, "consult_answer": code}
    (tmp_path / "code.sha256").write_text(script["attempts"]["s1"][0]["files"]["code.sha256"])
    messages = [] if delivery == "empty" else [
        {"kind": "answer", "via": delivery, "body": "an older answer"},
        {"kind": "answer", "via": delivery, "body": script["consult_answer"]},
    ]
    if delivery == "consult":
        messages.append({"kind": "answer", "body": "an answer from another seat"})
    messages.append({"kind": "interface", "body": "another message"})

    def rpc(_method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = {"messages": messages} if params["name"] == "looper_room_inbox" else {}
        return {"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": json.dumps({"status": "ok", "result": result})}],
            "isError": False}}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(fake_claude, "_mcp", lambda _args: rpc)
    monkeypatch.setattr(fake_claude.time, "sleep", lambda _seconds: None)
    fake_claude._worker(script, "Your step: s2", SimpleNamespace(readline=lambda: "start"), [])
    assert (tmp_path / "code.txt").read_text() == (code if delivery != "empty" else "") + "\n"
    assert (task.check(tmp_path) is None) is (delivery != "empty" and code == task.fake["consult_answer"])


@pytest.mark.parametrize(("task_id", "correct"), [
    ("reviewer_elsewhere", [True]),
    ("reviewer_elsewhere_wrong", [False, True]),
])
def test_fake_worker_reports_the_files_in_its_other_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str, correct: list[bool],
) -> None:
    task = task_list.select([task_id])[0]
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    for name, content in task.files.items():
        (work_dir / name).write_text(content)
    runner.init_repository(work_dir)
    monkeypatch.chdir(work_dir)
    replies: list[str] = []
    monkeypatch.setattr(fake_claude, "_answer", lambda _session, text, _cost: replies.append(text))
    for attempt, expected in enumerate(correct, 1):
        prompt = "Your step: s1" + (f"\nbeing retried (attempt {attempt})" if attempt > 1 else "")
        fake_claude._worker(task.fake, prompt, SimpleNamespace(readline=lambda: "start"), [])
        judged = judge_result({"subtype": "success", "result": replies[-1]}, work_dir=work_dir, is_verify=False)
        assert judged["status"] == "completed"
        assert judged["changed_files"] == {"summary.txt": "written"}
        assert (task.check(work_dir) is None) is expected
        assert not (work_dir / "summary.txt").exists()
        # Location guidance must cause the read: without it even the correct sibling file fails review.
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path / "runtime")
        for guidance in (True, False):
            result = dict(judged)
            if not guidance:
                result.pop("found_elsewhere", None)
            review_prompt = pool._build_verify_prompt({"work_result": result})
            stdin = SimpleNamespace(readline=lambda: json.dumps({"message": {"content": review_prompt}}))
            fake_claude._reviewer(task.fake, "The step under review: s1", stdin)
            assert json.loads(replies[-1])["passed"] is (expected and guidance)
    elsewhere = work_dir.with_name(work_dir.name + "-feat")
    assert runner._git_in(work_dir, "branch", "--show-current") == "main"
    assert runner._git_in(elsewhere, "branch", "--show-current") == "feat"
    assert int(runner._git_in(elsewhere, "rev-list", "--count", "main..HEAD")) == len(correct)

@pytest.mark.parametrize(("contents", "passed"), [(None, False), ("north south", False), ("north south east", True)])
def test_fake_reviewer_reads_the_file_in_its_cwd_without_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None, passed: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    if contents is not None:
        (tmp_path / "summary.txt").write_text(contents)
    replies: list[str] = []
    monkeypatch.setattr(fake_claude, "_answer", lambda _session, text, _cost: replies.append(text))
    script = {"review_reads": {"s1": {"file": "summary.txt", "contains": ["north", "south", "east"]}}}
    stdin = SimpleNamespace(readline=lambda: json.dumps({"message": {"content": "Summary: step s1 attempt 99"}}))
    fake_claude._reviewer(script, "The step under review: s1", stdin)
    assert json.loads(replies[-1])["passed"] is passed


def test_the_multi_platform_check_reads_the_interface_the_parts_must_agree_on(tmp_path: Path) -> None:
    from evals.prompt_regression.tasks import TASKS

    task = next(t for t in TASKS if t.id == "roles_multi_platform")
    for name, content in {**task.files, **{n: c for a in task.fake["attempts"].values() for n, c in a[0]["files"].items()}
                          }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content, encoding="utf-8")
    assert task.check(tmp_path) is None
    interface = tmp_path / "api" / "tip_api.json"
    interface.write_text('{"path": "/api/tip", "query": "day", "fields": {"day": "int", "tip": "string"}}')
    assert task.check(tmp_path) is None  # the fields with their types
    # A contract without its path, its query parameter or its fields fails, in either form of the fields.
    for fields in (["day", "tip"], {"day": "int", "tip": "string"}):
        for drop in ("path", "query", "fields"):
            contract = {key: value for key, value in {"path": "/api/tip", "query": "day", "fields": fields}.items()
                        if key != drop}
            interface.write_text(json.dumps(contract))
            assert task.check(tmp_path) is not None, (fields, drop)
        for empty in ("path", "query", "fields"):
            interface.write_text(json.dumps({"path": "/api/tip", "query": "day", "fields": fields,
                                             empty: type({"path": "", "query": "", "fields": fields}[empty])()}))
            assert task.check(tmp_path) is not None, (fields, empty)
    interface.write_text('{"path": "/api/tip", "query": "day", "fields": ["day", "tip", "author"]}')
    assert task.check(tmp_path) is not None
    interface.write_text('{"path": "/api/tip", "query": "d", "fields": ["day", "tip"]}')
    assert task.check(tmp_path) is not None


def test_a_final_summary_needs_every_field_heading() -> None:
    full = "## Delivered\na\n\n## How to use\nb\n\n## Verification\nc\n\n## Open issues\nnone"
    assert metrics.final_summary({"result_summary": full}) == 1
    assert metrics.final_summary({"result_summary": full.replace("## Verification\nc\n\n", "")}) == 0
    assert metrics.final_summary({"result_summary": "calc is built"}) == 0


def test_most_at_once_counts_overlapping_runs_but_not_touching_ones() -> None:
    assert metrics.most_at_once([(0.0, 10.0), (5.0, 12.0), (11.0, 20.0)]) == 2
    assert metrics.most_at_once([(0.0, 10.0), (10.0, 20.0)]) == 1
    assert metrics.most_at_once([]) == 0


# --- One task's metrics ---------------------------------------------------------------------------------


def _record(record_id: int, agent_id: str, *, input_text: str = "", step_id: str | None = None,
            start: str = "2026-10-13T10:00:00+00:00", end: str = "2026-10-13T10:00:05+00:00") -> dict[str, Any]:
    return {"id": record_id, "agent_id": agent_id, "input_text": input_text, "status": "completed",
            "metadata": {"step_id": step_id} if step_id else {}, "created_at": start, "finished_at": end}


def _result(text: str) -> list[dict[str, Any]]:
    return [{"seq": 1, "event": {"type": "result", "subtype": "success", "result": text}}]


def test_task_metrics_preserve_actual_runtime_and_unknown_usage(tmp_path):
    record = _record(1, "planner-task")
    record.update(session_id="session", metadata={
        "profile": {"runtime": {"type": "dsh"}}, "system_prompt_mode": "append",
        "command": ["env", "HOME=/disposable", "/bin/dsh", "--profile", "headless", "-"],
    })
    result = metrics.task_metrics(detail={"status": "completed"}, activity=[], records=[record],
        events={}, changes={}, work_dir=tmp_path, check_problem=None, expect={}, now=0)
    assert result["runtime_evidence"] == [{"role": "planner", "runtime": "dsh", "executable": "dsh",
        "system_prompt_mode": "append", "session_present": True, "root_usage_complete": None,
        "status": "completed", "report_valid": None, "verdict_passed": None,
        "tool_calls": [], "permission_denials": []}]


@pytest.mark.parametrize("text,valid,passed", [('{"passed": true}', True, True),
    ('{"passed": false}', True, False), ('{"passed": "yes"}', False, None), ('no verdict', False, None)])
def test_task_metrics_preserve_reviewer_contract_and_tool_evidence(tmp_path, text, valid, passed):
    record = _record(1, "reviewer-step")
    record["metadata"]["permission_denials"] = [{"id": "denied", "tool": "write", "input": '{"path":"PLAN.md"}'}]
    events = [{"event": {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "config.json"}}]}}},
        *_result(text)]
    result = metrics.task_metrics(detail={"status": "completed"}, activity=[], records=[record],
        events={1: events}, changes={}, work_dir=tmp_path, check_problem=None, expect={}, now=0)
    evidence = result["runtime_evidence"][0]
    assert evidence["report_valid"] is valid
    assert evidence["verdict_passed"] is passed
    assert evidence["tool_calls"] == [{"id": "read-1", "name": "read"}]
    assert evidence["permission_denials"] == [{"id": "denied", "tool": "write"}]


@pytest.mark.parametrize("end,expected", [("2026-10-13T10:00:04+00:00", "verified"),
    ("2026-10-13T10:00:06+00:00", "violations"), (None, "unknown")])
def test_dependency_order_checks_api_run_timestamps(end, expected):
    steps = [{"id": "first"}, {"id": "second", "depends_on": ["first"]}]
    records = [_record(1, "worker-a", step_id="first", end=end),
               _record(2, "worker-b", step_id="second", start="2026-10-13T10:00:05+00:00")]
    result = metrics.dependency_order(steps, records)
    assert result == {"edges": 1, "verified": int(expected == "verified"),
                      "violations": int(expected == "violations"), "unknown": int(expected == "unknown")}


@pytest.mark.parametrize(("outside", "has_fact", "closeout", "found"), [
    (False, True, True, False),
    (True, True, True, True),  # a note outside the task's own
    (False, False, True, True),  # the note lacks the fact
    (False, True, False, True),  # no closeout
])
def test_vault_metrics_use_only_the_tasks_commits_and_check_their_notes(
    tmp_path: Path, outside: bool, has_fact: bool, closeout: bool, found: bool,
) -> None:
    task = task_list.select(["vault_closeout_keeps"])[0]
    path = "codex-mem/projects/calc.md"
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text("CALC_LOCALE=C" if has_fact else "a task summary", encoding="utf-8")
    # Two task commits touch one note; neither the seed nor the other task (even a similar id) belongs to it.
    log = (f"aaa\0looper: task-1 planner: tests\n\n{path}\n"
           f"bbb\0looper: task-10 planner: private\n\nmine/diary.md\n"
           "ccc\0eval: the task's files\n\ncodex-mem/AGENTS.md\nmine/diary.md\n"
           f"ddd\0looper: task-1 worker@s1: more tests\n\n{path}\n"
           + ("mine/diary.md\n" if outside else ""))
    seen: list[list[str]] = []

    def check(vault_dir: Path, paths: list[str]) -> str | None:
        assert vault_dir == tmp_path
        seen.append(paths)
        assert task.vault_check is not None
        return task.vault_check(vault_dir, paths)

    result = metrics.task_metrics(
        detail={"status": "completed", "steps": [{}]},
        activity=[{"kind": "phase", "summary": "Vault closeout: kept the test quirk"}] if closeout else [],
        records=[_record(1, "planner-task-1", input_text="[VAULT] Keep lasting notes")], events={}, changes={},
        work_dir=tmp_path, check_problem=None,
        expect={**task.expect, "min_vault_closeout": 1}, now=0.0,
        task_id="task-1", vault_dir=tmp_path, vault_log=log, vault_check=check)
    assert (result["vault_writes"], result["vault_outside"], result["vault_closeout"]) == (2, int(outside), int(closeout))
    assert result["planner_turns"] == {"vault": 1}
    assert bool(result["problems"]) is found
    assert result["success"] is (not found)
    assert seen == [sorted([path, "mine/diary.md"] if outside else [path])]


def test_vault_metrics_with_no_task_commits_do_not_count_other_tasks(tmp_path: Path) -> None:
    task = task_list.select(["vault_closeout_keeps"])[0]
    result = metrics.task_metrics(
        detail={"status": "completed", "steps": [{}]}, activity=[], records=[], events={}, changes={},
        work_dir=tmp_path, check_problem=None, expect=task.expect, now=0.0, task_id="task-1", vault_dir=tmp_path,
        vault_log="aaa\0looper: task-2 planner: a note\n\ncodex-mem/projects/calc.md\n",
        vault_check=task.vault_check)
    assert (result["vault_writes"], result["vault_outside"], result["vault_closeout"]) == (0, 0, 0)
    assert len(result["problems"]) == 2 and result["success"] is False  # the note check and the write count


@pytest.mark.parametrize("task_id", ["vault_closeout_keeps", "vault_closeout_nothing"])
def test_fake_planner_vault_turn_calls_only_its_script_and_replies_with_the_contract(
    monkeypatch: pytest.MonkeyPatch, task_id: str,
) -> None:
    task = task_list.select([task_id])[0]
    calls: list[tuple[str, dict[str, Any]]] = []
    connected: list[bool] = []
    replies: list[str] = []

    def mcp(args: list[str]) -> Any:
        assert args == ["stand-in"]
        connected.append(True)
        return lambda method, params: calls.append((method, params))

    monkeypatch.setattr(fake_claude, "_mcp", mcp)
    monkeypatch.setattr(fake_claude, "_answer", lambda _session, text, _cost, **_: replies.append(text))
    messages = iter([json.dumps({"message": {"content": "[VAULT] Keep lasting notes"}}), None])
    fake_claude._planner(task.fake, ["stand-in"], SimpleNamespace(readline=lambda: next(messages)))
    expected = task.fake.get("vault_calls") or []
    assert calls == [("tools/call", {"name": name, "arguments": arguments}) for name, arguments in expected]
    assert connected == ([True] if expected else [])
    assert len(replies) == 1
    assert parse_vault(replies[0]) == task.fake.get("vault_reply", "nothing worth keeping")


def test_fake_worker_carries_the_test_quirk_in_its_learnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = task_list.select(["vault_closeout_keeps"])[0]
    monkeypatch.chdir(tmp_path)
    replies: list[str] = []
    monkeypatch.setattr(fake_claude, "_answer", lambda _session, text, _cost: replies.append(text))
    fake_claude._worker(task.fake, "Your step: s1", SimpleNamespace(readline=lambda: "start"), [])
    result = judge_result({"subtype": "success", "result": replies[0]}, work_dir=tmp_path, is_verify=False)
    assert result["learnings"] == ["run_tests.py needs CALC_LOCALE=C"]
    assert task.check(tmp_path) is None


@pytest.mark.parametrize("with_vault", [False, True])
def test_one_eval_vault_is_configured_before_the_suite_and_shared_by_its_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_vault: bool,
) -> None:
    root = tmp_path / "eval"
    root.mkdir()
    tasks = task_list.VAULT_TASKS if with_vault else task_list.select(["single_step"])
    initialized: list[Path] = []
    configured: list[dict[str, Any]] = []
    seen: list[Path | None] = []

    @contextmanager
    def database(*_: Any) -> Any:
        yield "unused", 0.0

    @contextmanager
    def server(**_: Any) -> Any:
        yield SimpleNamespace(put=lambda path, body: configured.append(body) if path == "/api/knowledge-vault"
                              else pytest.fail(f"unexpected API call: {path}"))

    def run_task(_api: Any, task: EvalTask, _work: Path, *, vault_dir: Path | None, **_: Any) -> dict[str, Any]:
        if with_vault:
            assert initialized == [root / "vault"]
            assert configured == [{"path": str(root / "vault"), "writable": ["codex-mem"],
                                   "rules_file": "codex-mem/AGENTS.md"}]
            assert vault_dir is not None
            assert {p.relative_to(vault_dir).as_posix(): p.read_text() for p in vault_dir.rglob("*.md")} == \
                task_list.EVAL_VAULT
        else:
            assert not initialized and not configured and not (root / "vault").exists()
        seen.append(vault_dir)
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.0}

    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_: str(root))
    monkeypatch.setattr(runner, "_git", lambda: {})
    monkeypatch.setattr(runner, "fresh_database", database)
    monkeypatch.setattr(runner, "running_server", server)
    monkeypatch.setattr(runner, "init_repository", initialized.append)
    monkeypatch.setattr(runner, "select_agent", lambda *_, **__: {})
    monkeypatch.setattr(runner, "run_task", run_task)
    # Keep results in memory: this test starts no server and writes no run report.
    monkeypatch.setattr(runner, "write_result", lambda result, *_, **__: result)
    result, _ = runner.run(mode="fake", tasks=tasks, admin_url="unused", out_dir=tmp_path,
                           reps=2, jobs=2, keep_work_dirs=True)
    assert result["aborted"] is None
    assert seen == [root / "vault" if with_vault else None] * (len(tasks) * 2)


@pytest.mark.parametrize("task_id", ["vault_closeout_keeps", "vault_closeout_nothing"])
def test_vault_tasks_enable_the_project_and_read_back_their_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str,
) -> None:
    task = task_list.select([task_id])[0]
    work = tmp_path / "work"
    vault = tmp_path / "vault"
    note = vault / "codex-mem/projects/calc.md"
    note.parent.mkdir(parents=True)
    note.write_text("CALC_LOCALE=C", encoding="utf-8")
    posts: list[tuple[str, Any]] = []
    logs: list[Path] = []

    def post(path: str, body: Any) -> dict[str, Any]:
        posts.append((path, body))
        if path == "/api/tasks/start":
            for name, content in task.fake["attempts"]["s1"][0]["files"].items():
                (work / name).write_text(content, encoding="utf-8")
        return {"task_id": "task-1"}

    def get(path: str, **_: Any) -> dict[str, Any]:
        if path == "/api/tasks/task-1":
            return {"status": "completed", "steps": [{}]}
        if path == "/api/llm-records":
            return {"records": []}
        return {"activity": [{"kind": "phase", "summary": "Vault closeout: done"}]}

    def git(directory: Path, *args: str) -> str:
        logs.append(directory)
        assert args == ("log", "--format=%H%x00%s", "--name-only")
        return "abc\0looper: task-1 planner: tests\n\ncodex-mem/projects/calc.md\n" if task.vault_check else ""

    monkeypatch.setattr(runner, "_git_in", git)
    api = SimpleNamespace(post=post, get=get)
    row = runner.run_task(api, task, work, timeout=1, poll=0, vault_dir=vault)
    assert posts[0][1]["project"]["vault"] == "on"
    assert logs == [vault]
    assert row["outcome"] == "pass", row["problems"]
    assert (row["vault_writes"], row["vault_outside"], row["vault_closeout"]) == (int(bool(task.vault_check)), 0, 1)


def test_vault_task_without_an_eval_vault_is_rejected_before_creation(tmp_path: Path) -> None:
    task = task_list.select(["vault_closeout_nothing"])[0]
    with pytest.raises(runner.EvalError, match="server has no eval vault"):
        runner.run_task(SimpleNamespace(), task, tmp_path, timeout=1, poll=0)


def test_the_planners_refused_calls_are_counted_and_its_file_changes_can_be_bounded(tmp_path: Path) -> None:
    refused = {"permission_denials": [{"tool": "Bash", "input": "echo x > PLAN.md"}, {"tool": "Bash", "input": "touch a"}]}
    records = [{**_record(1, "planner-task-1", input_text="[PLAN] Plan the task"), "metadata": refused},
               {**_record(2, "worker-a", step_id="step-1"), "metadata": {"permission_denials": [{"tool": "x"}]}}]
    write = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": str(tmp_path / "PLAN.md")}}]}}
    result = metrics.task_metrics(
        detail={"status": "completed", "steps": [{"id": "step-1"}]}, activity=[], records=records,
        events={1: [{"seq": 1, "event": write}]}, changes={}, work_dir=tmp_path, check_problem=None,
        expect={"max_planner_modified_files": 0}, now=0.0)
    assert result["planner_denials"] == 2  # the Planner's only
    assert result["problems"]
    # A Write its runtime refused changed nothing.
    records[0]["metadata"] = {"permission_denials": [{"tool": "Write", "input": "{}", "id": "toolu_w"}]}
    write["message"]["content"][0]["id"] = "toolu_w"
    refused = metrics.task_metrics(
        detail={"status": "completed", "steps": [{"id": "step-1"}]}, activity=[], records=records,
        events={1: [{"seq": 1, "event": write}]}, changes={}, work_dir=tmp_path, check_problem=None,
        expect={"max_planner_modified_files": 0}, now=0.0)
    assert (refused["planner_modified_files"], refused["problems"]) == (0, [])
    # A Write the run does not have, answered with an error, changed nothing either.
    records[0]["metadata"] = {}
    disabled = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_w",
                                                         "is_error": True, "content": "Write is disabled"}]}}
    assert metrics.edit_tool_paths([{"event": write}, {"event": disabled}], tmp_path) == []
    assert metrics.edit_tool_paths([{"event": write}], tmp_path) == ["PLAN.md"]
    # An event whose message is a string (not a turn's) is skipped, not an error.
    texts = [{"event": {"type": "user", "message": "a note"}}, {"event": {"type": "assistant", "message": "a note"}}]
    assert metrics.edit_tool_paths([*texts, {"event": write}], tmp_path) == ["PLAN.md"]


def test_task_metrics_read_the_flow_cost_and_violations(tmp_path: Path) -> None:
    good = '{"status": "completed", "changed_files": {}, "deleted_files": [], "summary": "ok"}'
    records = [
        _record(1, "planner-task-1", input_text="[PLAN] Plan the task"),
        _record(2, "planner-task-1", input_text="[PLAN] Your last reply broke the reply contract: x"),
        _record(3, "worker-a", step_id="step-1"),
        _record(4, "planner-task-1", input_text="[DECISION] Step s1"),
        _record(5, "worker-b", step_id="step-1"),
        _record(6, "planner-task-1", input_text="[CHAT] A message from the user."),
        _record(7, "reviewer-c"),
    ]
    records[2].update(input_tokens=100, output_tokens=20, cache_read_tokens=3000, cache_creation_tokens=None,
                      status="failed", model="claude-haiku")
    records[4].update(input_tokens=50, output_tokens=5, cache_read_tokens=None, cache_creation_tokens=400,
                      model="claude-haiku")
    events = {3: _result("no report"), 5: _result(good), 7: _result('{"passed": true}')}
    detail = {"status": "completed", "steps": [{"id": "step-1"}],
              "cost": {"cost_usd": 0.0123, "runs": 7, "by_role": {"planner": {"cost_usd": 0.004, "runs": 4}}}}
    activity = [{"kind": "error", "summary": "Planner reply broke the PLAN contract (the reply has no ```json "
                                             "fenced block); asking again"}]
    result = metrics.task_metrics(
        detail=detail, activity=activity, records=records, events=events, changes={}, work_dir=tmp_path,
        check_problem=None, expect={"min_retries": 1, "min_reviews": 2, "max_contract_violations": 1}, now=0.0,
    )
    assert result["success"] is False
    assert len(result["problems"]) == 2  # too few reviews, too many contract violations
    assert (result["steps"], result["retries"], result["reviews"], result["chat_turns"]) == (1, 1, 1, 1)
    assert result["planner_turns"] == {"plan": 1, "repair": 1, "decision": 1, "chat": 1}
    assert result["contract_violations"] == {"planner": 1, "worker": 1, "reviewer": 0, "total": 2}
    assert (result["cost_usd"], result["cost_by_role"], result["planner_modified_files"]) == (
        0.0123, {"planner": 0.004}, 0)
    # Tokens are summed whether or not a run's cost is known.
    assert result["tokens"] == {"input": 150, "output": 25, "cache_read": 3000, "cache_creation": 400}
    # The runs that ended in an error, by their record's status, and the models each role's runs reported.
    assert result["run_errors"] == 1
    assert result["models"] == {"planner": [], "worker": ["claude-haiku"], "reviewer": []}


# --- Comparison -----------------------------------------------------------------------------------------


def _run(**tasks: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "real", "model": "haiku", "git": {"commit": "abcdef1234567", "dirty": False},
            "tasks": [{"id": task_id, **values} for task_id, values in tasks.items()]}


_OK = {"success": True, "steps": 1, "retries": 0, "reviews": 0, "contract_violations": {"total": 0},
       "planner_modified_files": 0, "cost_usd": 0.02}


def test_comparison_marks_what_changed_and_sums_the_run() -> None:
    base = _run(single_step=_OK, chat_correction=_OK)
    tokens = {"input": 1200, "output": 2500, "cache_read": 30000, "cache_creation": 800}
    new = _run(single_step={**_OK, "cost_usd": 0.03, "tokens": tokens},
               chat_correction={**_OK, "success": False, "contract_violations": {"total": 2},
                                "problems": ["color.txt holds 'red', expected 'blue'"]})
    new["git"]["dirty"] = True
    table = compare.markdown(new, base)
    lines = table.splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 (1 rep) → real/haiku @ abcdef123+local changes (1 rep)"
    # A run from before tokens were read has none: only the new side shows them.
    # A run from before gates were counted has none either.
    assert ("| single_step | 1/1 | 1.0 | – | 0.0 | 0.0 | – | – | 0.0 | 0.0 | – → **32k / 2.5k** | 0.0200 → **0.0300** |"
            in lines)
    assert "| chat_correction | 1/1 → **0/1** | 1.0 | – | 0.0 | 0.0 | – | – | 0.0 → **2.0** | 0.0 | – | 0.0200 |" in lines
    assert ("| **total** | 2/2 (100%, 34–100%) → **1/2 (50%, 9–91%)** | 2.0 | – | 0.0 | 0.0 | – | – | 0.0 → **2.0** "
            "| 0.0 | – → **32k / 2.5k** | 0.0400 → **0.0500** |" in lines)
    assert "- pass rate changed: chat_correction 1/1 → 0/1" in lines
    assert "- the base and the new run ran each case once: a pass or fail per case is a single sample" in lines
    assert "- chat_correction: color.txt holds 'red', expected 'blue'" in lines


def test_comparison_without_a_base_and_with_skipped_tasks() -> None:
    new = _run(single_step=_OK, chat_correction={"skipped": "the spending cap was reached", "cost_usd": 0.0})
    new.update(stopped_by_cap=True, caps={"total_cap_usd": 0.8})
    lines = compare.markdown(new).splitlines()
    assert lines[0].endswith("(no earlier run to compare with)")
    assert ("| chat_correction | skipped | skipped | skipped | skipped | skipped | skipped | skipped | skipped "
            "| skipped | skipped | skipped |" in lines)
    assert "| **total** | 1/1 (100%, 21–100%) | 1.0 | – | 0.0 | 0.0 | – | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    assert "- stopped at the spending cap (0.8 USD)" in lines


def _reps(task_id: str, *rows: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": task_id, "rep": rep, **row} for rep, row in enumerate(rows, start=1)]


_PASS = {**_OK, "outcome": "pass"}
_FAIL = {**_OK, "outcome": "fail", "success": False, "steps": 2, "cost_usd": 0.04, "problems": ["sum.txt is missing"]}
_TIMEOUT = {"outcome": "error", "success": False, "error_class": "timeout", "error": "cancelled after 900 s",
            "steps": 7, "cost_usd": 0.5}


def test_comparison_of_repeated_runs_counts_the_scored_reps_and_keeps_errors_apart() -> None:
    # A base of one rep per task (schema v1: no rep, no outcome; a task cancelled at its timeout is an error).
    base = _run(single_step=_OK, dependent_steps={**_OK, "success": False, "timed_out": True},
                chat_correction=_OK)
    new = {**_run(), "tasks": [*_reps("single_step", _PASS, _PASS, _PASS),
                               *_reps("dependent_steps", _PASS, _FAIL, _TIMEOUT),
                               *_reps("interface_first", _PASS, {"skipped": "the spending cap was reached"},
                                      {"outcome": "error", "success": False, "error_class": "harness",
                                       "error": "EvalError: GET /api/tasks/t answered 500"})]}
    lines = compare.markdown(new, base).splitlines()
    assert lines[0] == "Prompt eval: real/haiku @ abcdef123 (1 rep) → real/haiku @ abcdef123 (3 reps)"
    assert "| single_step | 1/1 → **3/3** | 1.0 | – | 0.0 | 0.0 | – | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    # The mean is over the scored reps: the pass and the fail, not the error.
    assert ("| dependent_steps | 0/0 · 1 err → **1/2 · 1 err** | – → **1.5** | – | – → **0.0** | – → **0.0** "
            "| – | – | – → **0.0** | – → **0.0** | – | – → **0.0300** |" in lines)
    assert "| interface_first | 1/1 · 1 err | 1.0 | – | 0.0 | 0.0 | – | – | 0.0 | 0.0 | – | 0.0200 |" in lines
    # The total adds up the tasks' means (one pass through the set), over the tasks both runs have; the new run's
    # own total follows, as it has a task the base lacks.
    assert ("| **total (2 shared tasks)** | 1/1 (100%, 21–100%) · 1 timeout → **4/5 (80%, 38–96%) · 1 timeout** "
            "| 1.0 → **2.5** | – | 0.0 | 0.0 | – | – | 0.0 | 0.0 | – | 0.0200 → **0.0500** |" in lines)
    assert ("| **total (all 3 tasks of the new run)** | 5/6 (83%, 44–97%) · 1 harness, 1 timeout | 3.5 | – | 0.0 "
            "| 0.0 | – | – | 0.0 | 0.0 | – | 0.0700 |" in lines)
    assert "- pass rate changed: dependent_steps 0/0 → 1/2" in lines
    assert "- the base ran each case once: a pass or fail per case is a single sample" in lines
    assert "- no baseline: interface_first" in lines
    assert "- in the base, not in the new run: chat_correction" in lines
    assert "- dependent_steps (rep 2): sum.txt is missing" in lines
    assert "- dependent_steps (rep 3): error (timeout): cancelled after 900 s" in lines
    assert "- interface_first (rep 3): error (harness): EvalError: GET /api/tasks/t answered 500" in lines


def test_against_a_base_of_more_tasks_the_total_is_over_the_shared_ones() -> None:
    # A base of seven tasks, six passed; the new run has one of them, twice.
    base = _run(**{f"task_{i}": _OK if i < 6 else {**_OK, "success": False} for i in range(7)})
    base["tasks"][0]["id"] = "single_step"
    new = {**_run(), "tasks": _reps("single_step", _PASS, {**_PASS, "cost_usd": 0.04})}
    lines = compare.markdown(new, base).splitlines()
    assert ("| **total (1 shared task)** | 1/1 (100%, 21–100%) → **2/2 (100%, 34–100%)** | 1.0 | – | 0.0 | 0.0 | – | – "
            "| 0.0 | 0.0 | – | 0.0200 → **0.0300** |" in lines)
    assert not any(line.startswith("| **total (all") for line in lines)  # the new run has no task of its own
    assert "- in the base, not in the new run: task_1, task_2, task_3, task_4, task_5, task_6" in lines


def test_against_a_base_of_other_tasks_there_is_no_shared_total() -> None:
    base = _run(dependent_steps=_OK, chat_correction=_OK)
    new = {**_run(), "tasks": _reps("single_step", _PASS, _PASS)}
    lines = compare.markdown(new, base).splitlines()
    assert not any(line.startswith("| **total (0 shared") for line in lines)
    assert ("| **total (all 1 task of the new run)** | 2/2 (100%, 34–100%) | 1.0 | – | 0.0 | 0.0 | – | – | 0.0 | 0.0 | – "
            "| 0.0200 |" in lines)
    assert "- no baseline: single_step" in lines
    assert "- in the base, not in the new run: dependent_steps, chat_correction" in lines


def test_comparison_says_which_tasks_ran_with_user_gates_other_than_the_base() -> None:
    # Unset is on since user gates G2: a base from before (no per-task record) or one run with them off compares
    # another contract.
    old = _run(single_step=_OK, gate_ambiguous=_OK)
    off = {**_run(), "tasks": [{"id": "single_step", **_PASS, "user_gates": "off"},
                               {"id": "gate_ambiguous", **_PASS, "user_gates": "on"}]}
    on = {**_run(), "tasks": [{"id": "single_step", **_PASS, "user_gates": "on"},
                              {"id": "gate_ambiguous", **_PASS, "user_gates": "on"}]}
    assert ("- the base does not record user gates per task (a run before user gates G2, where an unset setting was "
            "off): single_step, gate_ambiguous") in compare.markdown(on, old).splitlines()
    lines = compare.markdown(on, off).splitlines()
    assert "- user gates differ from the base, so these rows compare different contracts: single_step (off → on)" \
        in lines
    assert not any("user gates" in line for line in compare.markdown(on, on).splitlines())


@pytest.mark.parametrize(("task_gates", "run_gates", "effective"), [
    (False, None, "on"), (False, "off", "off"), (False, "on", "on"), (True, None, "on"), (True, "off", "on")])
def test_each_row_records_whether_its_planner_could_open_gates(task_gates: bool, run_gates: str | None,
                                                               effective: str) -> None:
    task = next(t for t in TASKS if t.user_gates == task_gates)
    assert runner.effective_user_gates(task, run_gates) == effective


def test_comparison_of_dialogs_shows_success_and_cost() -> None:
    run = {"mode": "fake", "agent": "claude-code/opus", "manager_agent": "claude-code/opus",
           "git": {"commit": "abcdef1234567", "dirty": False},
           "dialogs": _reps("status", {"outcome": "pass", "success": True, "cost_usd": 0.001},
                            {"outcome": "error", "success": False, "error_class": "timeout",
                             "error": "no reply to 'hi' within 60 s", "cost_usd": 0.002})}
    old = {**run, "dialogs": [{"id": "status", "success": True, "cost_usd": 0.003}]}  # schema v1
    lines = compare.markdown(run, old).splitlines()
    assert lines[0] == "Manager dialogs: fake/claude-code/opus @ abcdef123 (1 rep) → fake/claude-code/opus @ abcdef123 (2 reps)"
    assert "| dialog | success | cost (USD) |" in lines
    assert "| status | 1/1 → **1/1 · 1 err** | 0.0030 → **0.0010** |" in lines
    assert "- status (rep 2): error (timeout): no reply to 'hi' within 60 s" in lines


@pytest.mark.parametrize(("passes", "scored", "interval"), [
    (8, 10, (0.4902, 0.9433)),
    (41, 48, (0.7283, 0.9275)),
    (0, 10, (0.0, 3.8415 / 13.8415)),  # 0 of n: from 0 to z² / (n + z²)
    (10, 10, (10 / 13.8415, 1.0)),  # n of n: from n / (n + z²) to 1
    (0, 0, None),
])
def test_the_wilson_interval(passes: int, scored: int, interval: tuple[float, float] | None) -> None:
    found = compare.wilson(passes, scored)
    assert found == pytest.approx(interval, abs=1e-4) if interval else found is None


@pytest.mark.parametrize(("row", "outcome", "error_class"), [
    ({"outcome": "pass", "success": True}, "pass", None),
    ({"outcome": "error", "success": False, "error_class": "harness"}, "error", "harness"),
    ({"success": True}, "pass", None),  # schema v1
    ({"success": False}, "fail", None),
    ({"success": False, "timed_out": True}, "error", "timeout"),
    ({"success": False, "stopped_by_cap": True}, "error", "spending_cap"),
    ({"skipped": "the spending cap was reached"}, "skipped", None),
])
def test_a_rows_outcome_is_read_from_its_typed_fields(row: dict[str, Any], outcome: str,
                                                       error_class: str | None) -> None:
    assert (compare.outcome(row), compare.error_class(row)) == (outcome, error_class)


@pytest.mark.parametrize(("rows", "aborted", "code"), [
    ([_PASS, _PASS], None, 0),
    ([_PASS, _FAIL], None, 1),
    ([_PASS, _TIMEOUT], None, 1),
    ([_PASS, {"skipped": "the spending cap was reached"}], None, 1),
    ([_PASS], "KeyboardInterrupt", 2),
])
def test_the_exit_code_is_zero_only_when_every_row_ran_and_passed(rows: list[dict[str, Any]], aborted: str | None,
                                                                  code: int) -> None:
    assert cli.exit_code({"tasks": [{"id": "t", **row} for row in rows], "aborted": aborted}) == code


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_reps_must_be_a_whole_number_of_at_least_one(value: str) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["run", "--reps", value])
    assert cli._parser().parse_args(["dialogs", "--reps", "3"]).reps == 3


@pytest.mark.parametrize("command", ["run", "dialogs"])
@pytest.mark.parametrize("value", ["-0.4", "0", "nan", "inf", "-inf"])
def test_case_budget_must_be_finite_and_positive(
    command: str, value: str, capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parser().parse_args([command, f"--case-budget-usd={value}"])
    assert exc.value.code == 2
    assert "--case-budget-usd must be a finite positive number" in capsys.readouterr().err
    assert cli._parser().parse_args([command, "--case-budget-usd", "0.4"]).case_budget_usd == 0.4


@pytest.mark.parametrize("value", [-0.4, 0, float("nan"), float("inf"), -float("inf")])
def test_invalid_case_budgets_are_rejected_before_scheduling(value: float) -> None:
    def run_one(*_: Any) -> dict[str, Any]:
        pytest.fail("an invalid case budget must not start a case")

    with pytest.raises(runner.EvalError, match="finite positive number"):
        runner.run_suite((_task("a"),), run_one, total_cap_usd=1.0, jobs=2, run_budget_usd=value)


def test_a_task_set_is_resolved_against_the_tasks_there_are(monkeypatch: pytest.MonkeyPatch) -> None:
    planner = [task.id for task in runner.select_tasks(["planner"])]
    assert planner == [task.id for task in TASKS if task.id in runner.TASK_SETS["planner"]]
    assert "dependent_steps" in planner and "single_step" not in planner
    assert {"vault_closeout_keeps", "vault_closeout_nothing"} <= set(planner)
    # Mixed with ids, in the task set's order, each once.
    mixed = [task.id for task in runner.select_tasks(["planner", "single_step", "dependent_steps"])]
    assert mixed == ["single_step", *planner]
    # A task the set names joins it once it exists.
    later = EvalTask(id="roles_multi_platform", requirement="r", check=lambda _work_dir: None)
    monkeypatch.setattr(task_list, "TASKS", (*TASKS, later))
    assert [task.id for task in runner.select_tasks(["planner"])] == [*planner, "roles_multi_platform"]
    with pytest.raises(ValueError, match="unknown task id"):
        runner.select_tasks(["planner", "no_such_task"])
    monkeypatch.setitem(runner.TASK_SETS, "empty", ("no_such_task",))
    with pytest.raises(ValueError, match="none of its tasks"):
        runner.select_tasks(["empty"])


class _Poster:
    """The API as ``answer_gates`` uses it: what it posted."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, Any]] = []

    def post(self, path: str, body: Any) -> dict[str, Any]:
        self.posted.append((path, body))
        return {}


def test_wait_metrics_read_specs_states_and_direct_dependents(tmp_path: Path) -> None:
    detail = {"status": "completed", "waits": [
        {"step": "w1", "step_id": "wait-1", "opened_at": "2026-01-01T00:00:00Z", "state": "done",
         "check": {"type": "github_pr"}},
        {"step": "w2", "step_id": "wait-2", "opened_at": "2026-01-01T00:00:00Z", "state": "timed_out",
         "check": {"type": "command"}},
        {"step": "w3", "step_id": "wait-3", "opened_at": None, "state": "pending", "check": {"type": "github_pr"}},
        {"step": "w4", "step_id": "wait-4", "opened_at": "2026-01-01T00:00:00Z", "state": "done", "check": None},
    ], "steps": [
        {"id": "s1", "depends_on": []}, {"id": "wait-1", "step_key": "w1", "depends_on": ["s1"]},
        {"id": "s2", "depends_on": ["wait-1", "wait-2"]}, {"id": "s3", "depends_on": ["s2"]},
        {"id": "s4", "depends_on": ["wait-3"]},
    ]}
    expected = {"waits_opened": 3, "wait_github_checks": 2, "wait_command_checks": 1,
                "wait_dependents": 2, "waits_timed_out": 1}
    result = metrics.task_metrics(detail=detail, activity=[], records=[], events={}, changes={}, work_dir=tmp_path,
                                 check_problem=None, expect={f"min_{key}": value for key, value in expected.items()},
                                 now=0.0)
    assert {key: result[key] for key in expected} == expected
    assert result["success"]
    assert metrics.wait_metrics({}) == dict.fromkeys(expected, 0)


@pytest.mark.parametrize(("expect", "observed", "count"), [
    ({"min_waits_opened": 1, "min_wait_github_checks": 1, "min_wait_dependents": 1},
     {"waits_opened": 1, "wait_github_checks": 1, "wait_dependents": 1}, 0),
    ({"min_waits_opened": 1, "min_wait_github_checks": 1, "min_wait_dependents": 1}, {},
     3),
    ({"max_waits_opened": 0}, {"waits_opened": 0}, 0),
    ({"max_waits_opened": 0}, {"waits_opened": 1}, 1),
])
def test_wait_expectations(expect: dict[str, int], observed: dict[str, int], count: int) -> None:
    assert len(metrics.flow_problems(expect, observed)) == count


@pytest.mark.parametrize("filename", ["findings.md", "rereview.md"])
def test_rereview_rejects_empty_review_files(tmp_path: Path, filename: str) -> None:
    task = next(t for t in TASKS if t.id == "wait_pr_rereview")
    for name in ("findings.md", "rereview.md"):
        (tmp_path / name).write_text("The loop skips the last value.\n")
    (tmp_path / filename).write_text(" \n\t")
    assert task.check(tmp_path) is not None


def _rereview_flow() -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    detail = {"steps": [
        {"id": "review", "depends_on": []},
        {"id": "wait", "depends_on": ["review"]},
        {"id": "rereview", "depends_on": ["wait"]},
    ], "waits": [{"step_id": "wait", "state": "done", "outcome": "ready", "last_result": "ready", "checks": 2,
                  "opened_at": "2026-10-13T10:00:06+00:00", "closed_at": "2026-10-13T10:00:10+00:00",
                  "check": {"type": "github_pr", "repo": "example-org/demo-service", "number": 12,
                            "ready_when": "head_changed"}}]}
    records = [_record(1, "worker-initial", step_id="review"),
               _record(2, "worker-second", step_id="rereview", start="2026-10-13T10:00:11+00:00",
                       end="2026-10-13T10:00:15+00:00")]
    events = {i: _result(json.dumps({"status": "completed", "changed_files": {filename: "written"}}))
              for i, filename in ((1, "findings.md"), (2, "rereview.md"))}
    return detail, records, events


@pytest.mark.parametrize("fault", [None, "repo", "number", "ready_when", "unrelated_wait", "initial_not_upstream",
                                  "early_rereview", "same_worker", "not_opened", "missing_report",
                                  "proceeded", "cancelled", "resumed", "no_check", "no_baseline"])
def test_rereview_checks_the_requested_pr_and_actual_review_dependency(fault: str | None) -> None:
    task = next(t for t in TASKS if t.id == "wait_pr_rereview")
    detail, records, events = _rereview_flow()
    wait = detail["waits"][0]
    if fault in {"repo", "number", "ready_when"}:
        wait["check"][fault] = {"repo": "other/repo", "number": 99, "ready_when": "merged"}[fault]
    elif fault in {"proceeded", "cancelled", "resumed"}:
        wait["outcome"] = fault
    elif fault == "no_check":
        wait["last_result"] = "not_ready"
    elif fault == "no_baseline":
        wait["checks"] = 1
    elif fault == "unrelated_wait":
        detail["steps"][2]["depends_on"] = ["review"]
        detail["steps"].append({"id": "unrelated", "depends_on": ["wait"]})
    elif fault == "initial_not_upstream":
        detail["steps"][1]["depends_on"] = []
    elif fault == "early_rereview":
        records[1]["created_at"] = "2026-10-13T10:00:07+00:00"
    elif fault == "same_worker":
        events[1] = _result(json.dumps({"status": "completed", "changed_files": {
            "findings.md": "written", "rereview.md": "written"}}))
        del events[2]
    elif fault == "not_opened":
        wait["opened_at"] = None
    elif fault == "missing_report":
        del events[2]
    assert task.flow_check is not None
    assert (task.flow_check(detail, records, events) is None) is (fault is None)


@pytest.mark.parametrize("fixture", [False, True])
def test_gh_stub_answers_views_and_fixture_diffs_and_refuses_other_commands(tmp_path: Path, fixture: bool) -> None:
    task = next(t for t in TASKS if t.id == "wait_pr_rereview")
    stub = runner.install_gh(tmp_path / "bin", diffs=task.gh_pr_diffs if fixture else None)

    def gh(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(stub), *args], capture_output=True, text=True, timeout=5, check=False)

    view = gh("pr", "view", "12", "--repo", "example-org/demo-service", "--json",
              "headRefOid,state,comments,reviews,statusCheckRollup")
    assert view.returncode == 0 and not view.stderr
    assert json.loads(view.stdout) == {"headRefOid": "1" * 40, "state": "OPEN", "comments": [], "reviews": [],
                                       "statusCheckRollup": []}
    diff = gh("pr", "diff", "12", "--repo", "example-org/demo-service")
    assert diff.returncode == 0 and not diff.stderr
    assert diff.stdout == (task.gh_pr_diffs["example-org/demo-service#12"] if fixture else "")
    assert gh("pr", "diff", "99", "--repo", "other/repo").stdout == ""
    for args in [("pr", "merge", "12"), ("api", "user"), (),
                 ("pr", "view", "12", "--repo", "example-org/demo-service"),
                 ("pr", "diff", "12", "--repo", "example-org/demo-service", "--patch")]:
        refused = gh(*args)
        assert refused.returncode == 1 and refused.stdout == ""
        assert refused.stderr.strip() == "gh stub: unsupported"


def test_pr_fix_is_visible_to_both_wait_check_and_worker(tmp_path: Path) -> None:
    from looper.agents.wait_service import run_github_pr
    from looper.agents.waits import CheckResult, GithubPrCheck, ReadyWhen

    task = next(t for t in TASKS if t.id == "wait_pr_rereview")
    stub = runner.install_gh(tmp_path / "bin", diffs=task.gh_pr_diffs)
    work = tmp_path / "work"
    work.mkdir()
    runner.init_pr_fixture(work, task)
    other = tmp_path / "other-case"
    other.mkdir()
    runner.init_pr_fixture(other, task)
    check = GithubPrCheck("example-org/demo-service", 12, ReadyWhen.HEAD_CHANGED)

    def worker(*args: str) -> str:
        return subprocess.run([str(stub), "pr", *args, "--repo", check.repo], cwd=work,
                              capture_output=True, text=True, check=True, timeout=5).stdout

    first = asyncio.run(run_github_pr(check, None, gh=str(stub), work_dir=str(work)))
    assert first[0] is CheckResult.NOT_READY
    old_head = first[3]["baseline"]["head"]
    assert "range(len(values) - 1)" in worker("diff", "12")
    detail = {"waits": [{"step": "w1", "state": "waiting", "checks": 1, "last_result": "not_ready",
                         "opened_at": "1970-01-01T00:01:40Z", "check": check.row()}]}
    assert not runner.push_pr_fixes(task, work, detail, now=104.9)
    assert runner.push_pr_fixes(task, work, detail, now=105.0)
    assert not runner.push_pr_fixes(task, work, detail, now=106.0)
    second = asyncio.run(run_github_pr(check, first[3]["baseline"], gh=str(stub), work_dir=str(work)))
    assert second[0] is CheckResult.READY
    new_head = second[3]["baseline"]["head"]
    assert new_head != old_head
    view = subprocess.run([str(stub), "pr", "view", "12", "--repo", check.repo, "--json", "headRefOid,commits"],
                          cwd=work, capture_output=True, text=True, check=True, timeout=5)
    metadata = json.loads(view.stdout)
    assert metadata["headRefOid"] == new_head
    assert metadata["commits"][-1]["oid"] == new_head
    assert runner._git_in(work, "rev-parse", f"{new_head}^") == old_head
    assert runner._git_in(work, "show", f"{new_head}:totals.py") == task.gh_pr_fix_files["totals.py"].strip()
    diff = worker("diff", "12")
    assert "range(len(values) - 1)" not in diff
    assert "+    for value in values:" in diff
    other_diff = subprocess.run([str(stub), "pr", "diff", "12", "--repo", check.repo], cwd=other,
                                capture_output=True, text=True, check=True, timeout=5).stdout
    assert "range(len(values) - 1)" in other_diff
    # Step worktrees share the PR ref even if their own branch still has the old checkout.
    checkout = tmp_path / "worker-checkout"
    runner._git_in(work, "worktree", "add", "--detach", str(checkout), old_head)
    worktree_diff = subprocess.run([str(stub), "pr", "diff", "12", "--repo", check.repo], cwd=checkout,
                                   capture_output=True, text=True, check=True, timeout=5).stdout
    assert worktree_diff == diff


@pytest.mark.parametrize("fault", ["no_baseline", "error", "wrong_pr", "pending"])
def test_pr_fix_waits_for_a_successful_baseline_of_the_requested_pr(tmp_path: Path, fault: str) -> None:
    task = next(t for t in TASKS if t.id == "wait_pr_rereview")
    runner.init_pr_fixture(tmp_path, task)
    old_head = runner._git_in(tmp_path, "rev-parse", "HEAD")
    wait = {"state": "waiting", "checks": 1, "last_result": "not_ready", "opened_at": "1970-01-01T00:00:00Z",
            "check": {"type": "github_pr", "repo": "example-org/demo-service", "number": 12,
                      "ready_when": "head_changed"}}
    if fault == "no_baseline":
        wait["checks"] = 0
    elif fault == "error":
        wait["last_result"] = "error"
    elif fault == "wrong_pr":
        wait["check"]["number"] = 99
    else:
        wait["state"] = "pending"
    assert not runner.push_pr_fixes(task, tmp_path, {"waits": [wait]}, now=100)
    assert runner._git_in(tmp_path, "rev-parse", "HEAD") == old_head


def test_fake_pr_worker_requires_the_changed_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = next(t for t in TASKS if t.id == "wait_pr_rereview")
    stub = runner.install_gh(tmp_path / "bin")
    work = tmp_path / "work"
    work.mkdir()
    runner.init_pr_fixture(work, task)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(fake_claude, "_answer", lambda *_args, **_kwargs: None)
    stdin = SimpleNamespace(readline=lambda: "start")
    fake_claude._worker(task.fake, "Your step: s1", stdin, [])
    with pytest.raises(RuntimeError, match="does not contain the commit"):
        fake_claude._worker(task.fake, "Your step: s2", stdin, [])
    assert not (work / "rereview.md").exists()
    wait = {"state": "waiting", "checks": 1, "last_result": "not_ready", "opened_at": "1970-01-01T00:00:00Z",
            "check": task.fake["plan"][1]["check"]}
    assert runner.push_pr_fixes(task, work, {"waits": [wait]}, now=5)
    fake_claude._worker(task.fake, "Your step: s2", stdin, [])
    assert task.check(work) is None


def test_wait_resumes_after_delay_once_per_wait_and_only_when_opted_in() -> None:
    task = dataclasses.replace(next(t for t in TASKS if t.id == "wait_pr_rereview"), wait_resume_after_s=5)
    api, resumed = _Poster(), {}
    detail = {"waits": [{"step": "w1", "state": "waiting", "opened_at": "1970-01-01T00:01:40Z"},
                        {"step": "w2", "state": "pending", "opened_at": None}]}
    assert not runner.resume_waits(api, task, "t1", detail, resumed, now=104.9)
    assert runner.resume_waits(api, task, "t1", detail, resumed, now=105.0)
    assert not runner.resume_waits(api, task, "t1", detail, resumed, now=106.0)
    detail["waits"][1].update(state="waiting", opened_at="1970-01-01T00:01:42Z")
    assert runner.resume_waits(api, task, "t1", detail, resumed, now=107.0)
    assert resumed == {"w1": 105.0, "w2": 107.0}
    assert api.posted == [("/api/v1/waits/resume", {"task_id": "t1", "step": "w1"}),
                          ("/api/v1/waits/resume", {"task_id": "t1", "step": "w2"})]
    api = _Poster()
    for state in ("pending", "ready", "done", "timed_out", "cancel_requested", "failed", "skipped"):
        detail["waits"][0]["state"] = state
        assert not runner.resume_waits(api, task, "t1", {"waits": detail["waits"][:1]}, {}, now=110.0)
    detail["waits"][0]["state"] = "waiting"
    no_resume = dataclasses.replace(task, wait_resume_after_s=None)
    assert not runner.resume_waits(api, no_resume, "t1", detail, {}, now=110.0)
    assert api.posted == []


def test_a_failed_resume_is_not_recorded_and_a_later_poll_can_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    task = dataclasses.replace(next(t for t in TASKS if t.id == "wait_pr_rereview"), wait_resume_after_s=0)
    detail = {"waits": [{"step": "w1", "state": "waiting", "opened_at": "1970-01-01T00:00:00Z"}]}
    api, resumed = _Poster(), {}

    def conflict(*_: Any) -> Any:
        raise runner.EvalError("POST answered 409: wait changed")

    with monkeypatch.context() as patch:
        patch.setattr(api, "post", conflict)
        assert not runner.resume_waits(api, task, "t1", detail, resumed, now=0.0)
        assert resumed == {}
    assert runner.resume_waits(api, task, "t1", detail, resumed, now=1.0)
    assert resumed == {"w1": 1.0}
    detail["waits"][0]["opened_at"] = None
    assert not runner.resume_waits(api, task, "t1", detail, {}, now=2.0)


@pytest.mark.parametrize("fault", [None, "wrong_pr", "unrelated_wait"])
def test_run_task_pushes_fixes_without_resuming_the_pr_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None,
) -> None:
    task = dataclasses.replace(next(t for t in TASKS if t.id == "wait_pr_rereview"), check=lambda _: None)
    final, records, events = _rereview_flow()
    final["status"] = "completed"
    final["waits"][0].update(step="w1", opened_at="1970-01-01T00:01:40Z", closed_at="1970-01-01T00:01:45Z")
    if fault == "wrong_pr":
        final["waits"][0]["check"]["number"] = 99
    elif fault == "unrelated_wait":
        final["steps"][2]["depends_on"] = ["review"]
        final["steps"].append({"id": "unrelated", "depends_on": ["wait"]})
    records[0].update(created_at="1970-01-01T00:01:30Z", finished_at="1970-01-01T00:01:35Z")
    records[1].update(created_at="1970-01-01T00:01:46Z", finished_at="1970-01-01T00:01:50Z")
    wait = {**final["waits"][0], "state": "waiting", "closed_at": None, "last_result": "not_ready", "checks": 1}
    details = iter([{"status": "waiting", "waits": [wait]}, {"status": "waiting", "waits": [wait]}, final, final])
    posted: list[tuple[str, Any]] = []

    def post(path: str, body: Any) -> dict[str, Any]:
        posted.append((path, body))
        return {"task_id": "t1", "project_id": "p1"} if path == "/api/tasks/create" else {}

    def get(path: str, **_: Any) -> dict[str, Any]:
        if path == "/api/tasks/t1":
            return next(details)
        if path == "/api/llm-records":
            return {"records": records}
        if path.startswith("/api/llm-records/"):
            return {"events": events[int(path.split("/")[-2])]}
        assert path == "/api/tasks/t1/activity"
        return {"activity": []}

    monkeypatch.setattr(runner.time, "time", lambda: 104.0)
    result = runner.run_task(SimpleNamespace(post=post, get=get), task, tmp_path, timeout=10, poll=0,
                             clock_offset=1.0)
    assert result["outcome"] == ("pass" if fault is None else "fail")
    assert result["wait_dependents"] == 1
    if fault is not None:
        assert result["problems"]  # aggregate counts pass; only the task-specific flow check rejects this
    assert result["wait_resumes"] == {}
    assert [path for path, _ in posted] == ["/api/tasks/create", "/api/tasks/start"]
    assert ("for value in values" in runner._git_in(tmp_path, "show", "HEAD:totals.py")) is (fault != "wrong_pr")


def test_every_server_installs_gh_before_starting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def stop_before_start() -> int:
        assert (tmp_path / "bin" / "gh").is_file()
        assert os.access(tmp_path / "bin" / "gh", os.X_OK)
        raise RuntimeError("server deliberately disabled")

    monkeypatch.setattr(runner, "free_port", stop_before_start)
    with pytest.raises(RuntimeError, match="server deliberately disabled"), runner.running_server(
        database_url="unused", runtime_root=tmp_path / "runtime", bin_dir=tmp_path / "bin",
        log_path=tmp_path / "server.log",
    ):
        pytest.fail("no server may start")


@pytest.mark.parametrize("task_id", ["wait_pr_rereview", "wait_not_needed"])
def test_fake_wait_plans_parse_and_delivered_files_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task_id: str,
) -> None:
    task = next(t for t in TASKS if t.id == task_id)
    assert task_id in {t.id for t in runner.select_tasks(["planner"])}
    messages = iter([json.dumps({"message": {"content": "[PLAN] Plan the task"}}), None])
    replies: list[str] = []
    monkeypatch.setattr(fake_claude, "_answer", lambda _session, text, _cost, **_: replies.append(text))
    fake_claude._planner(task.fake, [], SimpleNamespace(readline=lambda: next(messages)))
    parsed = parse_plan(replies[0], wait_policy=WaitPolicy(limits=WaitLimits(min_check_seconds=10)))
    assert [step.id for step in parsed.steps] == (["s1", "w1", "s2"] if task_id == "wait_pr_rereview"
                                                else ["s1", "s2"])
    if task_id == "wait_pr_rereview":
        assert parsed.steps[1].wait is not None
        assert parsed.steps[1].wait.check.row() == task.fake["plan"][1]["check"]
        assert parsed.steps[1].wait.max_wait_seconds == 45
        assert parsed.steps[1].wait.every_seconds == 10
        assert parsed.steps[2].depends_on == ("w1",)
    else:
        assert all(step.wait is None for step in parsed.steps)
    assert task.check(tmp_path) is not None
    for attempts in task.fake["attempts"].values():
        for name, content in attempts[0]["files"].items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    assert task.check(tmp_path) is None
    if task_id == "wait_pr_rereview":
        (tmp_path / "rereview.md").unlink()
    else:
        (tmp_path / "result.txt").write_text("wrong")
    assert task.check(tmp_path) is not None


def test_the_scripted_user_ends_a_gate_that_asks_again_after_its_script_ran_out() -> None:
    task = next(t for t in TASKS if t.id == "gate_ambiguous")
    api, answered = _Poster(), {}
    waiting = {"gates": [{"step": "g1", "waiting_on": "user", "opened_at": None}]}
    for _ in range(4):
        runner.answer_gates(api, task, "t1", waiting, answered, now=0.0)
    assert api.posted == [("/api/v1/ops/gates/reply", {"task_id": "t1", "gate": "g1", "text": task.gate_script[0]}),
                          ("/api/v1/ops/gates/end", {"task_id": "t1", "gate": "g1"})]
    # A script that ends the discussion itself gets no second end.
    ends = next(t for t in TASKS if t.id == "gate_end_early")
    api, answered = _Poster(), {}
    for _ in range(3):
        runner.answer_gates(api, ends, "t1", waiting, answered, now=0.0)
    assert api.posted == [("/api/v1/ops/gates/end", {"task_id": "t1", "gate": "g1"})]
    # Nothing while the gate waits for the Planner.
    api = _Poster()
    runner.answer_gates(api, task, "t1", {"gates": [{"step": "g1", "waiting_on": "planner"}]}, {}, now=0.0)
    assert api.posted == []


def test_a_gate_that_asks_after_its_script_ran_out_fails_a_task_that_bounds_it(tmp_path: Path) -> None:
    task = next(t for t in TASKS if t.id == "gate_ambiguous")
    assert task.expect["max_gate_asks_beyond_script"] == 0

    def result(asks: int) -> dict[str, Any]:
        return metrics.task_metrics(detail={"status": "completed", "steps": []}, activity=[], records=[], events={},
                                    changes={}, work_dir=tmp_path, check_problem=None,
                                    expect={"max_gate_asks_beyond_script": 0}, gate_asks_beyond_script=asks, now=0.0)

    assert result(0)["success"] and result(0)["gate_asks_beyond_script"] == 0
    assert result(1)["problems"] and not result(1)["success"]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_gate_task_has_gates_on_whatever_user_gates_says(tmp_path: Path) -> None:
    tasks = runner.select_tasks(["gate_ambiguous"])
    assert {"gate_ambiguous", "gate_not_needed", "gate_parallel_branch", "gate_discoverable"} <= \
        set(runner.TASK_SETS["planner"])
    result, _ = runner.run(mode="fake", tasks=tasks, admin_url=str(DSN), out_dir=tmp_path, user_gates="off")
    (row,) = result["tasks"]
    assert row["outcome"] == "pass" and row["gates_opened"] == 1, row["problems"]


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("suite", ["tasks", "dialogs"])
def test_run_metadata_keeps_model_names_with_typed_or_legacy_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: bool, suite: str,
) -> None:
    from evals.prompt_regression import dialogs

    def profile(name: str) -> dict[str, Any]:
        model = {"name": name, "protocol": "anthropic-messages", "model_id": f"model-{name}",
                 "provider_id": "prov-test"} if typed else name
        return {"name": f"claude-code/{name}", "model": model}

    @contextmanager
    def database(*_: Any) -> Any:
        yield "unused", 0.0

    @contextmanager
    def server(**_: Any) -> Any:
        yield SimpleNamespace(post=lambda *_: {"project_id": "project-test"})

    module = runner if suite == "tasks" else dialogs
    monkeypatch.setattr(module, "fresh_database", database)
    monkeypatch.setattr(module, "running_server", server)
    monkeypatch.setattr(module, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(module, "_git", lambda: {})
    monkeypatch.setattr(module, "select_agent", lambda *_, **__: profile("sonnet"))
    if suite == "tasks":
        monkeypatch.setattr(runner, "run_task", lambda _api, task, *_a, **_kw: {
            "id": task.id, "outcome": "pass", "cost_usd": 0.0})
        result, path = runner.run(mode="real", tasks=task_list.select(["single_step"]),
                                  admin_url="unused", out_dir=tmp_path)
        assert result["protocol"] == ("anthropic-messages" if typed else None)
    else:
        monkeypatch.setattr(dialogs, "_manager_profile", lambda *_: profile("opus"))
        monkeypatch.setattr(dialogs, "run_dialog", lambda _api, case, **_: {
            "id": case.id, "outcome": "pass", "turns": [], "cost_usd": 0.0})
        monkeypatch.setattr(dialogs, "_usage", lambda _: (0.0, {}))
        result, path = dialogs.run(mode="real", dialogs=dialogs.select(["status"]),
                                   manager_model="opus", admin_url="unused", out_dir=tmp_path)
        assert result["manager_model"] == "opus"
    assert result["aborted"] is None
    assert result["model"] == "sonnet"
    assert json.loads(path.read_text())["model"] == "sonnet"
    assert runner.compared_model(result) == ("opus" if suite == "dialogs" else "sonnet")


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("dialogs", [False, True])
def test_baseline_comparison_reads_model_names_from_existing_raw_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed: bool, dialogs: bool,
) -> None:
    def model(name: str) -> Any:
        return {"name": name, "protocol": "anthropic-messages", "model_id": f"model-{name}",
                "provider_id": "prov-test"} if typed else name

    run = {"mode": "real", "model": model("sonnet"), "tasks": []}
    if dialogs:
        run.update(manager_model=model("opus"), dialogs=[])
    expected = "opus" if dialogs else "sonnet"
    monkeypatch.setattr(runner, "BASELINES_DIR", tmp_path)
    baseline = tmp_path / ("dialogs-real-opus.json" if dialogs else "real-sonnet.json")
    baseline.write_text(json.dumps(run))
    earlier = tmp_path / "20261016T100000Z-real.json"
    earlier.write_text(json.dumps(run))
    current = tmp_path / "20261016T110000Z-real.json"
    assert runner.compared_model(run) == expected
    for choice in (None, "previous"):
        data, _ = runner.comparison_base(choice, out_dir=tmp_path, current=current, mode="real",
                                         model=runner.compared_model(run), dialogs=dialogs)
        assert data == run


@pytest.mark.parametrize("command", ["run", "dialogs"])
def test_release_tier_refreshes_the_sonnet_baseline_for_every_case(command: str) -> None:
    readme = (Path(__file__).resolve().parents[1] / "evals" / "README.md").read_text(encoding="utf-8")
    release = readme.split("Once before a release,", 1)[1].split("#### ", 1)[0]
    entries = []
    for entry in release.split("\n- "):
        for text in re.findall(r"`((?:run|dialogs) --[^`]+)`", entry):
            args = cli._parser().parse_args(shlex.split(text))
            model = getattr(args, "manager_model", None) or args.model
            if args.command == command and model == "sonnet":
                entries.append((args, entry))
    assert len(entries) == 1, f"release tier needs one full Sonnet {command} run"
    args, entry = entries[0]
    assert args.mode == "real"
    assert args.model == "sonnet"
    assert args.reps == 3
    assert getattr(args, "tasks" if command == "run" else "dialogs") is None
    assert args.case_budget_usd > 0
    assert args.total_cap_usd >= args.jobs * args.case_budget_usd
    baseline = runner.baseline_path(mode="real", model="sonnet", dialogs=command == "dialogs")
    assert f"baselines/{baseline.name}" in entry


def test_the_base_is_the_committed_baseline_unless_another_is_named(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    baselines, runs = tmp_path / "baselines", tmp_path / "runs"
    baselines.mkdir()
    runs.mkdir()
    monkeypatch.setattr(runner, "BASELINES_DIR", baselines)
    # Keep these paths outside the report's repository, including with an in-tree --basetemp.
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path / "repository")

    def write(path: Path, **data: Any) -> Path:
        path.write_text(json.dumps({"mode": "real", **data}), encoding="utf-8")
        return path

    committed = write(baselines / "real-haiku.json", model="haiku", tasks=[], git={"commit": "c0ffee"})
    write(baselines / "dialogs-real-opus.json", model="haiku", manager_model="opus", dialogs=[])
    earlier = write(runs / "20261016T100000Z-real.json", model="haiku", tasks=[])
    write(runs / "20261016T110000Z-dialogs-real.json", model="haiku", dialogs=[])  # newer, but of the dialogs
    write(runs / "20261016T120000Z-real.json", model="opus", tasks=[])  # newer, but of another model
    current = runs / "20261016T130000Z-real.json"

    def base(choice: str | None, **kwargs: Any) -> tuple[Any, str]:
        return runner.comparison_base(choice, out_dir=runs, current=current, mode="real", **kwargs)

    assert base(None, model="haiku") == (json.loads(committed.read_text()), committed.name)
    assert base("previous", model="haiku") == (json.loads(earlier.read_text()), earlier.name)
    assert base(str(earlier), model="opus")[0] == json.loads(earlier.read_text())
    assert base(None, model="opus") == (None, "no committed baseline real-opus.json")
    assert base("previous", model="sonnet")[0] is None
    # Dialogs are compared by the Manager's model.
    assert base(None, model="opus", dialogs=True)[1] == "dialogs-real-opus.json"
    assert base("previous", model="haiku", dialogs=True)[1] == "20261016T110000Z-dialogs-real.json"


# --- The spending cap -----------------------------------------------------------------------------------


def _task(task_id: str) -> EvalTask:
    return EvalTask(id=task_id, requirement="r", check=lambda _work_dir: None)


def test_the_run_stops_at_the_total_cap() -> None:
    seen: list[tuple[str, int, float | None]] = []

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        seen.append((task.id, rep, cap_left))
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.3}

    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcd"), run_one, total_cap_usd=0.5)
    assert [(t, r, round(c, 2)) for t, r, c in seen] == [("a", 1, 0.5), ("b", 1, 0.2)]
    assert stopped is True
    assert [r.get("skipped") for r in results] == [None, None, "the spending cap was reached",
                                                    "the spending cap was reached"]
    _, stopped = runner.run_suite(tuple(_task(t) for t in "ab"), run_one, total_cap_usd=None)
    assert stopped is False
    # The cap covers every rep: rep 1 of each task, then rep 2, until it is reached.
    seen.clear()
    results, stopped = runner.run_suite(tuple(_task(t) for t in "ab"), run_one, total_cap_usd=1.0, reps=3)
    assert [(t, r) for t, r, _ in seen] == [("a", 1), ("b", 1), ("a", 2), ("b", 2)]
    assert [(r["id"], r["rep"], bool(r.get("skipped"))) for r in results] == [
        ("a", 1, False), ("b", 1, False), ("a", 2, False), ("b", 2, False), ("a", 3, True), ("b", 3, True)]


def test_a_case_that_raises_is_a_harness_error_and_the_run_goes_on() -> None:
    def run_one(task: EvalTask, rep: int, _cap_left: float | None) -> dict[str, Any]:
        if (task.id, rep) == ("a", 1):
            raise runner.TaskError("EvalError: GET /api/tasks/t answered 500", cost_usd=0.4)
        if (task.id, rep) == ("b", 1):
            raise ConnectionRefusedError("the server is gone")
        return {"id": task.id, "outcome": "pass", "success": True, "cost_usd": 0.1}

    results, stopped = runner.run_suite((_task("a"), _task("b")), run_one, total_cap_usd=0.5, reps=2)
    assert [(r["id"], r["rep"], r.get("outcome"), r.get("error_class"), r["cost_usd"]) for r in results] == [
        ("a", 1, "error", "harness", 0.4), ("b", 1, "error", "harness", 0.0), ("a", 2, "pass", None, 0.1),
        ("b", 2, None, None, 0.0)]  # the error's cost counts toward the cap
    assert results[1]["error"] == "ConnectionRefusedError: the server is gone" and results[1]["success"] is False
    assert results[3]["skipped"] and stopped is True


def test_cases_run_in_parallel_up_to_the_jobs_and_the_rows_keep_their_order() -> None:
    gate = threading.Barrier(3, timeout=10)  # three cases are in flight together, or the test times out
    delays = {"a": 0.3, "b": 0.0, "c": 0.1, "d": 0.0}

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        if (rep, task.id) in ((1, "a"), (1, "b"), (1, "c")):
            gate.wait()
        time.sleep(delays[task.id])
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.0}

    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcd"), run_one, total_cap_usd=None, reps=2, jobs=3)
    assert stopped is False
    assert [(r["id"], r["rep"]) for r in results] == [(t, rep) for rep in (1, 2) for t in "abcd"]


def test_a_case_starts_only_when_the_spend_and_every_budget_in_flight_fit_the_cap() -> None:
    lock = threading.Lock()
    running = 0
    peak = 0
    seen: list[tuple[str, float | None]] = []

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            seen.append((task.id, None if cap_left is None else round(cap_left, 2)))
        time.sleep(0.2)
        with lock:
            running -= 1
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.1}

    # Cap 1.0, budget 0.4 each: two fit (0.8), a third would pass the cap while they run (1.2).
    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcdef"), run_one, total_cap_usd=1.0, jobs=4,
                                        run_budget_usd=0.4)
    assert peak == 2
    assert dict(seen)["a"] == 0.4 and dict(seen)["b"] == 0.4
    # a and b cost 0.2, c and d 0.2 more: e needs 0.4 + 0.4 + 0.4 = 1.2 after c's turn; it runs once they end.
    assert [r["id"] for r in results] == list("abcdef") and not any(r.get("skipped") for r in results)
    assert stopped is False


@pytest.mark.parametrize("jobs", [1, 2])
def test_case_allowances_fit_the_reservations_and_total_cap(jobs: int) -> None:
    gate = threading.Barrier(jobs, timeout=5)
    seen: list[float] = []

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        assert cap_left is not None
        if task.id in "ab" and jobs == 2:
            gate.wait()
        seen.append(cap_left)
        return {"outcome": "pass", "cost_usd": cap_left}

    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcd"), run_one,
                                        total_cap_usd=1.0, jobs=jobs, run_budget_usd=0.4)
    if jobs == 2:
        assert seen == pytest.approx([0.4, 0.4, 0.2])
        assert all(0 < allowance <= 0.4 for allowance in seen)
    else:
        assert seen == [1.0]  # one job keeps the existing total-cap allowance
    assert sum(row["cost_usd"] for row in results) <= 1.0
    assert stopped and results[-1].get("skipped")


@pytest.mark.parametrize("mode", ["real", "fake"])
@pytest.mark.parametrize("case,expected", [
    ("completed", "pass"), ("recovered", "pass"), ("subagent_failure", "pass"),
    ("completed_no_result", "pass"), ("completed_with_error", "harness"), ("interrupted", "harness"),
    ("failed", "harness"), ("budget_words_only", "harness"), ("incomplete", "harness"),
    ("missing", "harness"), ("failed_after_completed", "harness"),
    ("autonomous_after_failed", "harness"), ("autonomous_after_completed", "pass"),
    ("error_result_completed", "harness"), ("typed_budget", "spending_cap"),
    ("paged_budget", "spending_cap"),
])
def test_dialog_checks_the_final_manager_run_before_scoring_a_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, case: str, expected: str,
) -> None:
    from evals.prompt_regression import dialogs

    checked: list[str] = []
    diagnostic = "Claude Code stopped at its per-run spend cap" if case == "budget_words_only" else "run failed"
    status = "completed" if expected == "pass" or case in {
        "error_result_completed", "completed_with_error"} else "failed"
    if case in {"incomplete", "interrupted"}:
        status = "running" if case == "incomplete" else "interrupted"
    final_id = 3 if case in {"recovered", "failed_after_completed"} else 2
    result = {"type": "result", "subtype": "success", "is_error": False}
    if case in {"typed_budget", "paged_budget"}:
        result.update(subtype="error_max_budget_usd", is_error=True)
    elif case == "error_result_completed":
        result.update(subtype="error_during_execution", is_error=True)

    class Api:
        def __init__(self) -> None:
            self.sent: dict[str, Any] | None = None
            self.event_offsets: list[int] = []

        def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
            assert path == "/api/chat/send"
            self.sent = data
            return {}

        def get(self, path: str, **query: Any) -> dict[str, Any]:
            if path == "/api/tasks":
                return {"tasks": []}
            if path == "/api/chat/messages":
                assert self.sent is not None
                return {"messages": [{"message_id": self.sent["message_id"] + ":reply",
                                      "content": "该配对不兼容，已保持所有配置不变。"}]}
            if path == "/api/llm-records":
                assert query["agent_id"] == "manager"
                rows = [{"id": 1, "status": "failed", "error": "old turn failed"}]
                if self.sent is not None and case != "missing":
                    if final_id == 3:
                        rows.append({"id": 2, "status": "failed" if case == "recovered" else "completed",
                                     "error": "resume failed" if case == "recovered" else None})
                    rows.append({"id": final_id, "status": status,
                                 "error": diagnostic if status == "failed" or case == "completed_with_error" else None})
                    if case.startswith("autonomous_after_"):
                        rows.append({"id": final_id + 1, "status": "completed" if status == "failed" else "running",
                                     "error": None, "metadata": {"autonomous": True}})
                return {"records": list(reversed(rows))}
            event_ids = {final_id, final_id + 1} if case.startswith("autonomous_after_") else {final_id}
            assert path in {f"/api/llm-records/{record_id}/events" for record_id in event_ids}
            after = int(query.get("after", 0))
            limit = int(query.get("limit", 1000))
            self.event_offsets.append(after)
            events = []
            if case == "paged_budget":
                events = [{"seq": i, "event": {"type": "system"}} for i in range(1, 5001)]
            if case != "completed_no_result":
                events.append({"seq": len(events) + 1, "event": result})
            if case == "subagent_failure":
                events.append({"seq": len(events) + 1, "event": {
                    "type": "result", "subtype": "error_during_execution",
                    "is_error": True, "parent_tool_use_id": "subagent-tool"}})
            return {"events": [event for event in events if event["seq"] > after][:limit]}

    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    say = dialogs.Say("确认配对是否有效。", lambda look: checked.append(look.reply))
    scenario = tmp_path / "scenarios.json"
    scenario.write_text("{}")
    api = Api()
    row = dialogs.run_dialog(api, dialogs.Dialog("refused_pair", (say,)), database_url="unused",
                             project_id="project-test", scenarios=scenario, mode=mode, timeout=1)
    if expected == "pass":
        assert row["outcome"] == "pass" and len(checked) == 1, row
    else:
        assert (row["outcome"], row.get("error_class"), row["success"]) == ("error", expected, False), row
        assert not checked
        if status == "failed" and case != "missing":
            assert diagnostic in row["error"]
    if case == "paged_budget":
        assert api.event_offsets == [0, 5000]


@pytest.mark.parametrize("jobs", [1, 2])
def test_dialogs_use_the_reserved_case_budget_only_in_parallel_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: int,
) -> None:
    from evals.prompt_regression import dialogs

    apis: dict[str, Any] = {}
    gate = threading.Barrier(jobs, timeout=5)

    class Api:
        def __init__(self) -> None:
            self.cost = 0.0
            self.messages: list[dict[str, Any]] = []

        def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
            if path == "/api/projects":
                return {"project_id": "project-a"}
            assert path == "/api/chat/send"
            if not self.messages:
                gate.wait()
            self.cost += 0.2
            self.messages.append({"message_id": f"{data['message_id']}:reply", "content": "ok"})
            return {}

        def get(self, path: str, **_: Any) -> dict[str, Any]:
            if path == "/api/tasks":
                return {"tasks": []}
            if path == "/api/llm-records":
                return {"records": [{"id": i + 1, "status": "completed", "error": None}
                                    for i in reversed(range(len(self.messages)))]}
            if path.startswith("/api/llm-records/"):
                return {"events": [{"seq": 1, "event": {"type": "result", "subtype": "success"}}]}
            assert path == "/api/chat/messages"
            return {"messages": self.messages}

    @contextmanager
    def database(_: str) -> Any:
        name = threading.current_thread().name
        apis[name] = Api()
        yield name, None

    @contextmanager
    def server(*, database_url: str, **_: Any) -> Any:
        yield apis[database_url]

    monkeypatch.setattr(dialogs, "fresh_database", database)
    monkeypatch.setattr(dialogs, "running_server", server)
    monkeypatch.setattr(dialogs, "install_claude", lambda *_, **__: None)
    monkeypatch.setattr(dialogs, "select_agent", lambda *_, **__: {"name": "fake"})
    monkeypatch.setattr(dialogs, "_git", lambda: {})
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [])
    monkeypatch.setattr(dialogs, "_usage", lambda db: (apis[db].cost, {}))
    says = tuple(dialogs.Say(text, lambda _: None) for text in ("first", "second", "third"))
    cases = tuple(dialogs.Dialog(t, says) for t in ("abc" if jobs == 2 else "a"))
    result, path = dialogs.run(mode="fake", dialogs=cases,
                               admin_url="disposable", out_dir=tmp_path, jobs=jobs,
                               total_cap_usd=1.0, case_budget_usd=0.4)
    rows = result["dialogs"]
    if jobs == 2:
        assert [row["cost_usd"] for row in rows] == [0.4, 0.4, 0.0]
        assert all(row["error_class"] == "spending_cap" for row in rows[:2])
        assert rows[2]["skipped"] and result["stopped_by_cap"]
    else:
        assert rows[0]["cost_usd"] == 0.6 and rows[0]["outcome"] == "pass"
        assert not result["stopped_by_cap"]
    assert result["total_cost_usd"] <= 1.0
    assert json.loads(path.read_text())["total_cost_usd"] == result["total_cost_usd"]


def test_parallel_cases_are_skipped_once_the_spend_reaches_the_cap() -> None:
    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.5}

    results, stopped = runner.run_suite(tuple(_task(t) for t in "abcd"), run_one, total_cap_usd=1.0, jobs=2,
                                        run_budget_usd=0.5)
    assert [bool(r.get("skipped")) for r in results] == [False, False, True, True] and stopped is True


def test_a_failure_that_is_not_caught_lets_the_cases_in_flight_end_and_is_raised() -> None:
    started: list[str] = []

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        started.append(task.id)
        if task.id == "a":
            raise RuntimeError("setup failed")
        time.sleep(0.2)
        return {"id": task.id, "outcome": "pass", "cost_usd": 0.0}

    results: list[dict[str, Any]] = []
    with pytest.raises(RuntimeError, match="setup failed"):
        runner.run_suite(tuple(_task(t) for t in "abcd"), run_one, total_cap_usd=None, jobs=2, results=results,
                         catch=False)
    assert started == ["a", "b"] and [r["id"] for r in results] == ["b"]


def test_parallel_runs_under_a_cap_need_the_cases_own_budget() -> None:
    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        raise AssertionError("no case should start")

    with pytest.raises(runner.EvalError, match="--case-budget-usd"):
        runner.run_suite((_task("a"),), run_one, total_cap_usd=1.0, jobs=2)
    with pytest.raises(runner.EvalError, match="--case-budget-usd"):
        runner.run(mode="real", tasks=(_task("a"),), admin_url="postgresql://unused/postgres", out_dir=Path("."),
                   total_cap_usd=1.0, max_budget_usd=0.25, jobs=2)


def test_an_interrupt_ends_the_cases_in_flight_before_the_run_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    running = threading.Event()
    ended: list[str] = []

    def run_one(task: EvalTask, rep: int, cap_left: float | None) -> dict[str, Any]:
        running.set()
        while not runner.INTERRUPTED.is_set():
            time.sleep(0.01)
        time.sleep(0.2)  # the case takes a moment to wind down
        ended.append(task.id)
        raise runner.EvalError("the run was interrupted")

    def interrupt(*_args: Any, **_kwargs: Any) -> Any:
        assert running.wait(5)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "wait", interrupt)
    results: list[dict[str, Any]] = []
    with pytest.raises(KeyboardInterrupt):
        runner.run_suite((_task("a"),), run_one, total_cap_usd=None, jobs=2, results=results)
    assert ended == ["a"]  # joined before the exception left run_suite
    assert not any(t.name.startswith("eval-case") and t.is_alive() for t in threading.enumerate())


def test_ports_handed_out_in_one_process_are_distinct() -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        ports = list(pool.map(lambda _: runner.free_port(), range(64)))
    assert len(set(ports)) == 64


class _CostlyApi:
    """A task whose cost grows 0.1 per poll; it is cancelled when asked and then reports so."""

    def __init__(self) -> None:
        self.cost = 0.0
        self.posts: list[tuple[str, Any]] = []
        self.cancelled = False

    def post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        if path == "/api/tasks/cancel":
            self.cancelled = True
        return {"task_id": "task-1"} if path == "/api/tasks/create" else {}

    def get(self, path: str, **_query: Any) -> Any:
        if path == "/api/tasks/task-1":
            if not self.cancelled:
                self.cost += 0.1
            status = "cancelled" if self.cancelled else "running"
            return {"status": status, "phase": "executing", "steps": [], "cost": {"cost_usd": self.cost}}
        if path == "/api/llm-records":
            return {"records": []}
        return {"activity": [], "events": []}


def test_a_task_is_cancelled_when_it_reaches_the_cap_left(tmp_path: Path) -> None:
    api = _CostlyApi()
    result = runner.run_task(api, _task("a"), tmp_path / "work", timeout=60, poll=0, cap_left=0.25)  # type: ignore[arg-type]
    assert [path for path, _ in api.posts] == ["/api/tasks/create", "/api/tasks/start", "/api/tasks/cancel"]
    assert result["stopped_by_cap"] is True and result["success"] is False
    # Cancelled at the cap: an error, not the model's failure.
    assert (result["outcome"], result["error_class"], result["error"]) == (
        "error", "spending_cap", "cancelled at the spending cap")
    assert "cancelled at the spending cap" in result["problems"]
    assert result["cost_usd"] == pytest.approx(0.3)


def test_a_task_the_harness_cannot_follow_is_cancelled_with_its_cost(tmp_path: Path) -> None:
    class _Failing(_CostlyApi):
        def get(self, path: str, **query: Any) -> Any:
            if path == "/api/llm-records":
                raise runner.EvalError("GET /api/llm-records answered 500: boom")
            return super().get(path, **query)

    api = _Failing()
    api.cancelled = True  # the task ended: the harness fails reading it back
    with pytest.raises(runner.TaskError) as raised:
        runner.run_task(api, _task("a"), tmp_path / "work", timeout=60, poll=0)  # type: ignore[arg-type]
    assert str(raised.value) == "EvalError: GET /api/llm-records answered 500: boom"
    assert raised.value.cost_usd == pytest.approx(0.0)
    assert [path for path, _ in api.posts] == ["/api/tasks/create", "/api/tasks/start", "/api/tasks/cancel"]


def test_real_claude_gets_the_per_run_budget(tmp_path: Path) -> None:
    script = runner.install_claude(tmp_path, mode="real", max_budget_usd=0.25, real_claude="/opt/claude dir/claude")
    assert script.read_text().splitlines()[-1] == "exec '/opt/claude dir/claude' \"$@\" --max-budget-usd 0.25"
    with pytest.raises(runner.EvalError):
        runner.install_claude(tmp_path, mode="real", max_budget_usd=0.25, real_claude=None)


def test_with_a_real_reviewer_task_the_stand_in_hands_claude_code_every_run_but_that_tasks_others(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    real = tmp_path / "real-claude"  # records what it was started with
    real.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(tmp_path / 'real.jsonl')!r}, 'a')"
                    ".write(json.dumps(sys.argv[1:]) + '\\n')\n")
    real.chmod(0o755)
    scripted, other = tmp_path / "scripted", tmp_path / "other"
    for directory in (scripted, other):
        directory.mkdir()
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(json.dumps({str(scripted.resolve()): {"real_reviewer": True, "plan": []},
                                     str(other.resolve()): {"plan": []}}))
    script = runner.install_claude(tmp_path / "bin", mode="real", scenarios=scenarios, max_budget_usd=0.25,
                                   real_claude=str(real), real_reviewer=True)
    assert script.read_text().splitlines()[-1].startswith(f"LOOPER_EVAL_REAL_CLAUDE={real} exec ")

    def started(cwd: Path, task_section: str) -> list[list[str]]:
        prompt = tmp_path / "prompt.md"
        prompt.write_text(task_section)
        (tmp_path / "real.jsonl").unlink(missing_ok=True)
        subprocess.run([str(script), "--append-system-prompt-file", str(prompt)], cwd=cwd, input="", text=True,
                       capture_output=True, timeout=30, check=True)
        log = tmp_path / "real.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    review, work = "## Task\nThe step under review: s1\n", "## Task\nYour step: s1\n"
    budget = ["--max-budget-usd", "0.25"]
    # That task's Reviewer is Claude Code, with every argument; its Worker plays the script.
    assert started(scripted, review) == [["--append-system-prompt-file", str(tmp_path / "prompt.md"), *budget]]
    assert started(scripted, work) == []
    # Every run of every other task is Claude Code's.
    assert started(other, work) == [["--append-system-prompt-file", str(tmp_path / "prompt.md"), *budget]]


# --- Fake mode: the task set through a real server -----------------------------------------------------


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_fake_mode_rereviews_the_new_pr_commit(tmp_path: Path) -> None:
    result, _ = runner.run(mode="fake", tasks=task_list.select(["wait_pr_rereview"]),
                           admin_url=str(DSN), out_dir=tmp_path, agent="claude-code/opus",
                           reps=2, jobs=2, task_timeout=60)
    assert result["aborted"] is None
    assert len(result["tasks"]) == 2
    for row in result["tasks"]:
        assert row["outcome"] == "pass", row["problems"]
        assert not row["wait_resumes"]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_fake_mode_reviews_files_in_another_worktree(tmp_path: Path) -> None:
    result, _ = runner.run(mode="fake", tasks=task_list.select(["reviewer_elsewhere", "reviewer_elsewhere_wrong"]),
                           admin_url=str(DSN), out_dir=tmp_path, agent="claude-code/opus")
    assert result["aborted"] is None
    assert [(row["id"], row["success"], row["reviews"], row["retries"]) for row in result["tasks"]] == [
        ("reviewer_elsewhere", True, 1, 0), ("reviewer_elsewhere_wrong", True, 2, 1)]


_MISBEHAVING = EvalTask(
    id="misbehaving",
    requirement="Create done.txt holding done.",
    check=lambda work_dir: None if (work_dir / "done.txt").is_file() else "done.txt is missing",
    fake={
        "plan": [{"id": "s1", "instruction": "Create done.txt.", "acceptance_criteria": ["done.txt exists"],
                  "depends_on": [], "review": False}],
        "break_first_plan": 1,
        "planner_writes": {"planner-notes.txt": "the Planner wrote this\n"},
        "attempts": {"s1": [{"raw": "Finished, no report."},
                            {"status": "completed", "files": {"done.txt": "done\n"}}]},
    },
)


# What each fake-mode task is run through: (steps, retries, reviews, chat turns), and how its steps sit side by side.
_FLOW = {"single_step": (1, 0, 0, 0), "dependent_steps": (2, 0, 0, 0), "many_steps": (9, 0, 0, 0),
         "wait_pr_rereview": (3, 0, 0, 0), "wait_not_needed": (2, 0, 0, 0),
         "parallel_parts": (3, 0, 0, 0),
         "interface_first": (4, 0, 0, 0), "reviewed_step": (1, 0, 1, 0),
         "retry_after_failure": (1, 1, 0, 0), "chat_correction": (1, 0, 0, 1), "misbehaving": (1, 1, 0, 0),
         # The change to the second step replaced it: s1, the dropped s2, and its replacement.
         "supplement_replan": (3, 0, 0, 1), "requirement_edit": (3, 0, 0, 1),
         "supplement_no_change": (2, 0, 0, 1), "profile_choice": (1, 0, 0, 0),
         # The security review role reviews its step after the built-in Reviewer.
         "roles_tester_docs": (3, 0, 0, 0), "roles_security_review": (1, 0, 2, 0),
         "roles_advisory_review": (1, 0, 2, 0), "roles_two_reviews": (1, 0, 3, 0),
         "roles_multi_platform": (6, 0, 0, 0),
         "roles_not_needed": (1, 0, 0, 0), "roles_template_fits": (1, 0, 2, 0),
         "roles_template_unfit": (1, 0, 0, 0),
         # A gate is a step; the steps its closing turn added follow it.
         "gate_ambiguous": (2, 0, 0, 0), "gate_not_needed": (1, 0, 0, 0), "gate_end_early": (2, 0, 0, 0),
         "gate_parallel_branch": (3, 0, 0, 0), "gate_discoverable": (2, 0, 0, 0),
         "operator_notes": (1, 0, 0, 0), "operator_notes_stale": (1, 0, 0, 0),
         "vault_closeout_keeps": (1, 0, 0, 0), "vault_closeout_nothing": (1, 0, 0, 0),
         "room_interface": (3, 0, 0, 0), "room_consult": (2, 0, 0, 0), "room_quiet": (1, 0, 0, 0),
         # The retry notes' tasks: one retry and a passing review; two retries, the first review failing.
         "planner_plan_file": (2, 0, 0, 0), "reviewer_retry_note": (1, 1, 1, 0),
         "reviewer_unrelated_note": (1, 2, 2, 0), "reviewer_elsewhere": (1, 0, 1, 0),
         "reviewer_elsewhere_wrong": (1, 1, 2, 0)}
# (independent pairs, bracketed pairs, parallel workers): only the parts that need nothing of each other run side by
# side; interface_first's sit between its interface step and its integration step.
_SHAPE = {"single_step": (0, 0, 1), "dependent_steps": (0, 0, 1), "many_steps": (0, 0, 1),
          "wait_pr_rereview": (0, 0, 1), "wait_not_needed": (0, 0, 1),
          "parallel_parts": (1, 0, 2),
          "interface_first": (1, 1, 2), "reviewed_step": (0, 0, 1), "retry_after_failure": (0, 0, 1),
          "chat_correction": (0, 0, 1), "misbehaving": (0, 0, 1),
          # The dropped second step and its replacement both depend on s1 alone.
          "supplement_replan": (1, 0, 1), "requirement_edit": (1, 0, 1),
          "supplement_no_change": (0, 0, 1), "profile_choice": (0, 0, 1),
          "roles_tester_docs": (1, 0, 2), "roles_security_review": (0, 0, 1),
          "roles_advisory_review": (0, 0, 1), "roles_two_reviews": (0, 0, 1), "roles_not_needed": (0, 0, 1),
          "roles_template_fits": (0, 0, 1), "roles_template_unfit": (0, 0, 1),
          # Its four platform steps run side by side between the interface step and the check.
          "roles_multi_platform": (6, 6, 4),
          "gate_ambiguous": (0, 0, 1), "gate_not_needed": (0, 0, 1), "gate_end_early": (0, 0, 1),
          # The clear part runs beside the gate and then beside the step the gate's answer planned.
          "gate_parallel_branch": (2, 0, 2), "gate_discoverable": (0, 0, 1),
          "operator_notes": (0, 0, 1), "operator_notes_stale": (0, 0, 1),
          "vault_closeout_keeps": (0, 0, 1), "vault_closeout_nothing": (0, 0, 1),
          "room_interface": (1, 0, 2), "room_consult": (0, 0, 1), "room_quiet": (0, 0, 1),
          "planner_plan_file": (0, 0, 1), "reviewer_retry_note": (0, 0, 1),
          "reviewer_unrelated_note": (0, 0, 1), "reviewer_elsewhere": (0, 0, 1),
          "reviewer_elsewhere_wrong": (0, 0, 1)}
# What only some tasks do; a task not named in a table is checked to do none of it. Only the tasks whose Planner defined roles run
# steps as them: each run on a version it defined, with the boundaries before its contract, and the reviewed step
# accepted only after both reviews passed. An advisory review role's failing review does not keep its step from being
# accepted.
_ROLE_KEYS = ("roles_defined", "role_runs", "role_profile_runs", "role_reviews", "role_prompt_mismatches",
              "role_boundary_misses", "gate_breaches", "advisory_role_fails")
_ROLES = {"roles_tester_docs": (2, 2, 1, 0, 0, 0, 0, 0), "roles_security_review": (1, 1, 0, 1, 0, 0, 0, 0),
          "roles_advisory_review": (1, 1, 0, 1, 0, 0, 0, 1),
          "roles_two_reviews": (2, 2, 0, 2, 0, 0, 0, 0),
          "roles_multi_platform": (4, 4, 0, 0, 0, 0, 0, 0),
          "roles_template_fits": (1, 1, 0, 1, 0, 0, 0, 0)}
# Only the two-reviews task names more than one review role on a step.
_COUNTS = {
    "most_review_roles": {"roles_security_review": 1, "roles_advisory_review": 1, "roles_template_fits": 1,
                          "roles_two_reviews": 2},
    # Only the task whose step fits a project role template copies it.
    "template_roles": {"roles_template_fits": 1},
    # Only the multi-platform task's disciplines each get a role that names their conventions, and its four platform
    # steps are built on its interface step (the tester's and the doc writer's on the module).
    "discipline_roles": {"roles_multi_platform": 4},
    "interface_dependents": {"roles_tester_docs": 2, "roles_multi_platform": 4},
    # Only the task whose project lets its Planner choose a profile runs a Worker on the one it chose.
    "planner_profile_runs": {"profile_choice": 1},
    # Only the two changes that make the second step wrong change the plan.
    "plan_changes": {"supplement_replan": 1, "requirement_edit": 1},
    "waits_opened": {"wait_pr_rereview": 1},
}
# Only the gate tasks open gates. The scripted user answers (or ends the discussion), the gate passes on what was
# settled (or assumed), and the step it planned reads the conclusion; the step that finds the port cancels the gate
# that waits on it before it opens, so no gate asked.
_GATE_KEYS = ("gates_opened", "gate_turns", "gates_passed", "gates_ended_by_user", "gates_cancelled",
              "assumptions_stated", "gate_parallel_runs", "conclusion_reads")
_GATES = {"gate_ambiguous": (1, 1, 1, 0, 0, 0, 0, 1), "gate_end_early": (1, 1, 1, 1, 0, 1, 0, 1),
          "gate_parallel_branch": (1, 1, 1, 0, 0, 0, 1, 1)}
# Only the room tasks' agents use the room: room_interface's store posts its interface, its client asks the Planner,
# which answers in a [ROOM] turn; room_consult's second step asks the first, whose run has ended, and a consult of
# its session answers. Nothing is left unanswered; every other task's room stays quiet.
_ROOM_KEYS = ("room_messages", "room_questions", "room_unanswered", "room_interface", "room_refused",
              "room_tool_calls", "room_consults", "room_consults_answered")
_ROOM = {"room_interface": (3, 1, 0, 1, 0, 3, 0, 0), "room_consult": (2, 1, 0, 0, 0, 2, 1, 1)}
# The Planner turns of each kind that some tasks take.
_PLANNER_TURNS = {"chat_correction": ("chat", 1), "gate_ambiguous": ("gate", 1), "room_interface": ("room", 1)}
_VAULT = {"vault_closeout_keeps": (1, 1), "vault_closeout_nothing": (0, 1)}
_MISBEHAVING_RESULT = {"contract_violations": {"planner": 1, "worker": 1, "reviewer": 0, "total": 2},
                       "planner_edit_tool_paths": ["planner-notes.txt"],
                       "planner_changed_files": ["planner-notes.txt"], "planner_modified_files": 1}


def _run_fake_task(task: EvalTask, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """The task through a real server in fake mode: (its row, the run, the file the run was written to)."""
    result, path = runner.run(mode="fake", tasks=(task,), admin_url=str(DSN), out_dir=tmp_path,
                              agent="claude-code/opus")
    assert result["agent"] == "claude-code/opus"  # every role ran the agent named
    (row,) = result["tasks"]
    assert row["id"] == task.id
    return row, result, path


def _the_run_was_written(result: dict[str, Any], path: Path, tmp_path: Path) -> None:
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == result and written["schema"] == runner.SCHEMA
    assert written["aborted"] is None and {row["rep"] for row in written["tasks"]} == {1}
    assert str(Path.home()) not in path.read_text(encoding="utf-8")
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


def _the_flow_of(task_id: str, row: dict[str, Any]) -> None:
    assert (row["steps"], row["retries"], row["reviews"], row["chat_turns"]) == _FLOW[task_id]
    assert (row["independent_pairs"], row["bracketed_pairs"], row["parallel_workers"]) == _SHAPE[task_id]
    assert row["cost_usd"] > 0
    assert row["vault_outside"] == 0
    if task_id in _VAULT:
        assert (row["vault_writes"], row["vault_closeout"]) == _VAULT[task_id]
    if task_id in _ROLES:
        assert tuple(row[key] for key in _ROLE_KEYS) == _ROLES[task_id]
    else:
        assert not (row["roles_defined"] or row["role_runs"])
    for key, by_task in _COUNTS.items():
        assert (row[key] or 0) == by_task.get(task_id, 0), key
    if task_id in _GATES:
        assert tuple(row[key] for key in _GATE_KEYS) == _GATES[task_id]
    else:
        assert row["gates_opened"] == 0
    assert bool(row["wait_resumes"]) is False
    assert row["gate_asks_beyond_script"] == 0
    assert row["planner_denials"] == 0  # the stand-in is refused nothing: the denials are Claude Code's
    if task_id in _ROOM:
        assert tuple(row[key] for key in _ROOM_KEYS) == _ROOM[task_id]
    else:
        assert not (row["room_messages"] or row["room_tool_calls"])
    if task_id in _PLANNER_TURNS:
        kind, turns = _PLANNER_TURNS[task_id]
        assert row["planner_turns"][kind] == turns


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_fake_mode_runs_a_task_through_its_flow(tmp_path: Path, task: EvalTask) -> None:
    row, result, path = _run_fake_task(task, tmp_path)
    assert row["success"], row["problems"]
    assert row["contract_violations"]["total"] == 0
    assert row["planner_modified_files"] == 0, (row["planner_changed_files"], row["planner_edit_tool_paths"])
    _the_flow_of(task.id, row)
    if task.id == "dependent_steps":
        # The fake Planner's decisions carry progress notes, and its completing accept the final summary.
        assert row["progress_notes"] == 2 and row["final_summary"] == 1
    _the_run_was_written(result, path, tmp_path)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_fake_mode_sees_misbehaviour(tmp_path: Path) -> None:
    row, result, path = _run_fake_task(_MISBEHAVING, tmp_path)
    _the_flow_of("misbehaving", row)
    assert {key: row[key] for key in _MISBEHAVING_RESULT} == _MISBEHAVING_RESULT
    _the_run_was_written(result, path, tmp_path)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_fake_mode_repeats_each_task_rep_major_in_a_directory_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["run", "--mode", "fake", "--pg-url", str(DSN), "--out-dir", str(tmp_path),
                     "--tasks", "single_step,dependent_steps", "--reps", "2"])
    written = json.loads(next(tmp_path.glob("*-fake.json")).read_text(encoding="utf-8"))
    rows = written["tasks"]
    assert code == 0 and written["reps"] == 2
    assert [(r["id"], r["rep"], r["outcome"]) for r in rows] == [
        ("single_step", 1, "pass"), ("dependent_steps", 1, "pass"), ("single_step", 2, "pass"),
        ("dependent_steps", 2, "pass")]
    assert [r["work_dir"] for r in rows] == [f"<eval-root>/work/{r['id']}/{r['rep']}" for r in rows]
    shown = capsys.readouterr().out
    assert "| single_step | 2/2 | 1.0 |" in shown and "| dependent_steps | 2/2 | 2.0 |" in shown
    assert "Base: none (no committed baseline evals/prompt_regression/baselines/fake.json)" in shown


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_fake_mode_runs_cases_in_parallel_with_the_same_rows(tmp_path: Path) -> None:
    code = cli.main(["run", "--mode", "fake", "--pg-url", str(DSN), "--out-dir", str(tmp_path),
                     "--tasks", "single_step,dependent_steps", "--reps", "2", "--jobs", "4"])
    written = json.loads(next(tmp_path.glob("*-fake.json")).read_text(encoding="utf-8"))
    assert code == 0
    assert [(r["id"], r["rep"], r["outcome"]) for r in written["tasks"]] == [
        ("single_step", 1, "pass"), ("dependent_steps", 1, "pass"), ("single_step", 2, "pass"),
        ("dependent_steps", 2, "pass")]
    assert len({r["work_dir"] for r in written["tasks"]}) == 4


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_parallel_dialogs_each_keep_their_own_agent_and_manager(tmp_path: Path) -> None:
    code = cli.main(["dialogs", "--mode", "fake", "--pg-url", str(DSN), "--out-dir", str(tmp_path),
                     "--dialogs", "ask_project,status", "--jobs", "2"])
    written = json.loads(next(tmp_path.glob("*-dialogs-fake.json")).read_text(encoding="utf-8"))
    assert code == 0 and [(r["id"], r["outcome"]) for r in written["dialogs"]] == [
        ("ask_project", "pass"), ("status", "pass")]
    assert written["agent"] and written["manager_agent"] == written["agent"]


_STALLED = EvalTask(
    id="stalled",
    requirement="Create late.txt holding late.",
    check=lambda work_dir: None if (work_dir / "late.txt").is_file() else "late.txt is missing",
    fake={"plan": [{"id": "s1", "instruction": "Create late.txt.", "acceptance_criteria": ["late.txt exists"],
                    "depends_on": [], "review": False}],
          "attempts": {"s1": [{"status": "completed", "seconds": 120, "files": {"late.txt": "late\n"}}]}},
)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_task_past_its_timeout_is_an_error_not_a_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_list, "TASKS", (*TASKS, _STALLED))
    code = cli.main(["run", "--mode", "fake", "--pg-url", str(DSN), "--out-dir", str(tmp_path),
                     "--tasks", "stalled", "--task-timeout", "3"])
    written = json.loads(next(tmp_path.glob("*-fake.json")).read_text(encoding="utf-8"))
    (row,) = written["tasks"]
    assert code == 1
    assert (row["outcome"], row["error_class"], row["error"], row["success"]) == (
        "error", "timeout", "cancelled after 3 s", False)
    assert row["timed_out"] is True and row["status"] == "cancelled"


@pytest.mark.parametrize(("reply", "claims"), [
    # What real Managers answered when asked for a project's limit: explanations and offers.
    ("项目 eval-calc 的并发改不了：Looper 里并发是按任务设置的，项目本身没有并发限制。\n\n1. **只改这个项目里的任务**：把 eval-calc "
     "下各个任务的 Worker 并发分别设成 2。", False),
    ("这个改不了。\n1. **把这个任务的 Worker 上限改成 2**：马上生效，只影响这一个任务。", False),
    ("项目 eval-calc 的 Worker 并发没有改，因为 Looper 里项目本身没有并发限制。", False),
    ("Projects have no concurrency limit, so I changed nothing. I can set this task's Worker limit to 2 instead.", False),
    ("Projects have no concurrency limit, so nothing changed. Want this task's Worker limit at 2?", False),
    # Claims that it was done.
    ("已把项目 eval-calc 的 Worker 并发改为 2。", True),
    ("好的，项目 eval-calc 的 Worker 并发已经设置为 2。", True),
    ("Done: I changed the project's Worker limit to 2.", True),
    ("已把项目 eval-calc 的 Worker 并发调整为 2。", True),
    ("项目 eval-calc 的 Worker 并发已更新为 2。", True),
    ("Updated the project's Worker limit to 2.", True),
    ("Done. Set the project's Worker limit to 2.", True),
    ("Set this task's Worker limit to 2 instead?", False),
    ("Want me to update this task's Worker limit to 2?", False),
])
def test_a_reply_claiming_a_change_is_told_from_an_explanation(reply: str, claims: bool) -> None:
    from evals.prompt_regression import dialogs

    assert dialogs.claims_change(reply) is claims


@pytest.mark.parametrize(("reply", "model", "named"), [
    ("claude-haiku-4-5-20251001: $0.02", "claude-haiku-4-5-20251001", True),
    ("_HAIKU_RUNS: $0.02", "claude-haiku-4-5-20251001", True),
    ("Claude Sonnet: $0.02", "claude-haiku-4-5-20251001", False),
    ("DeepSeek: $0.02", "deepseek-flash", True),
])
def test_a_models_id_or_family_is_named_in_the_reply(reply: str, model: str, named: bool) -> None:
    from evals.prompt_regression.dialogs import _model_named

    assert _model_named(reply, model) is named


@pytest.mark.parametrize(("reply", "cost", "told"), [
    ("$0.04", 0.0409, True),
    ("0.041", 0.0409, True),
    ("$0.0409", 0.0409, True),
    ("0.04 美元", 0.0409, True),
    ("$999999", 0.0409, False),
    ("$0.05", 0.0409, False),
    ("0.1", 0.0409, False),
    ("0.0", 0.0409, False),
    ("$1,234.56", 1234.56, True),
    ("$1,000.0409", 0.0409, False),
    ("$0.04", 0.045, True),
    ("$0.04", 0.045001, False),
])
def test_an_amount_is_told_at_the_written_precision(reply: str, cost: float, told: bool) -> None:
    from evals.prompt_regression.dialogs import _amount_told

    assert _amount_told(reply, cost) is told


_HAIKU_RUNS = {"model": "claude-haiku-4-5", "runs": 2, "running": 1, "cost_usd": 0.0409}


@pytest.mark.parametrize(("task_id", "status", "by_model", "write_status", "fake", "reply", "problem"), [
    ("other", "ok", [_HAIKU_RUNS], None, False, "Haiku", "looper_costs did not read the task's cost"),
    ("task-a", "error", [_HAIKU_RUNS], None, False, "Haiku", "looper_costs did not read the task's cost"),
    ("task-a", "ok", [_HAIKU_RUNS], "ok", False, "Haiku", "something changed: looper_settings"),
    ("task-a", "ok", [_HAIKU_RUNS], "needs_confirmation", False, "Haiku", "something changed: looper_settings"),
    ("task-a", "ok", [{"model": None, "runs": 1, "running": 0, "cost_usd": 0.01}], None, False, "Haiku",
     "the task's cost has no model to name"),
    ("task-a", "ok", [_HAIKU_RUNS], None, False, "Sonnet", "the reply does not name claude-haiku-4-5"),
    ("task-a", "ok", [_HAIKU_RUNS], None, False, "Haiku $999999",
     "the reply does not give the cost of claude-haiku-4-5 (0.0409)"),
    ("task-a", "ok", [{**_HAIKU_RUNS, "cost_usd": None}], None, False, "Haiku", None),
    ("task-a", "ok", [_HAIKU_RUNS], None, True, "ok", None),
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "deepseek-flash", "runs": 1, "running": 0, "cost_usd": 0.02}],
     None, False, "Haiku $0.04 and DeepSeek $0.02", None),
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "deepseek-flash", "runs": 1, "running": 0, "cost_usd": 0.02}],
     None, False, "Haiku $0.02 and DeepSeek $0.04",
     "the reply does not give the cost of claude-haiku-4-5 (0.0409)"),
    ("task-a", "ok", [{**_HAIKU_RUNS, "cost_usd": 0.018943},
                      {"model": "deepseek-flash", "cost_usd": 0.123456}],
     None, False, "| Haiku | $0.019 |\n| DeepSeek | $0.123 |", None),
    ("task-a", "ok", [{**_HAIKU_RUNS, "cost_usd": 0.018943},
                      {"model": "deepseek-flash", "cost_usd": 0.123456}],
     None, False, "| Haiku | $0.123456 |\n| DeepSeek | $0.018943 |",
     "the reply does not give the cost of claude-haiku-4-5 (0.0189)"),
    # The seeded running run is present before the read too, even though it has no amount yet.
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "claude-opus-4-1", "runs": 1, "running": 1, "cost_usd": None}],
     None, False, "Haiku $0.0409", "the reply does not name claude-opus-4-1"),
    ("task-a", "ok", [_HAIKU_RUNS, {"model": "claude-opus-4-1", "runs": 1, "running": 1, "cost_usd": None}],
     None, False, "Haiku $0.0409; Opus is still running, cost unknown", None),
])
def test_a_tasks_cost_is_read_and_told_without_changes(
    monkeypatch: pytest.MonkeyPatch, task_id: str, status: str, by_model: list[dict[str, Any]],
    write_status: str | None, fake: bool, reply: str, problem: str | None,
) -> None:
    from evals.prompt_regression import dialogs

    actions = [{"tool": "looper_costs", "status": status, "risk": "read", "arguments": {"task_id": task_id},
                "result": None}]
    if write_status:
        actions.append({"tool": "looper_settings", "status": write_status, "risk": "write"})
    monkeypatch.setattr(dialogs, "_trail", lambda *_: actions)
    monkeypatch.setattr(dialogs.Look, "detail", lambda self, task_id=None: {"cost": {"by_model": by_model}})
    look = dialogs.Look(None, "task-a", reply, "", 0, frozenset(), fake)
    assert dialogs._cost_by_model_told(look) == problem


@pytest.mark.parametrize("mode", ["real", "fake"])
def test_task_cost_dialog_seeds_without_starting_an_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from evals.prompt_regression import dialogs

    seeded: list[tuple[str, str]] = []

    def seed(database_url: str, project_id: str) -> str:
        seeded.append((database_url, project_id))
        return "task-fixed"

    class Api:
        def get(self, path: str) -> dict[str, Any]:
            assert path == "/api/tasks"
            return {"tasks": [{"task_id": "task-fixed"}]}

    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text("{}", encoding="utf-8")

    def ask(api: Any, session: str, text: str, **_: Any) -> str:
        assert "task-fixed" in text
        if mode == "fake":
            assert json.loads(scenarios.read_text())["manager"][text] == [
                ["looper_costs", {"task_id": "task-fixed"}]]
        return "Haiku $0.018943; DeepSeek $0.12; Opus is running, cost unknown"

    def no_start(*_: Any, **__: Any) -> str:
        pytest.fail("task_cost must not start a Planner or Worker")

    monkeypatch.setattr(dialogs, "_make_cost_task", seed)
    monkeypatch.setattr(dialogs, "_start_slow_task", no_start)
    monkeypatch.setattr(dialogs, "_ask", ask)
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: 0)
    monkeypatch.setattr(dialogs, "_trail", lambda *_: [
        {"tool": "looper_costs", "status": "ok", "risk": "read", "arguments": {"task_id": "task-fixed"},
         "error_code": None}])
    monkeypatch.setattr(dialogs.Look, "detail", lambda *_: {"cost": {"by_model": [
        {"model": "claude-haiku-4-5", "cost_usd": 0.018943},
        {"model": "deepseek-flash", "cost_usd": 0.123456},
        {"model": "claude-opus-4-1", "cost_usd": None},
    ]}})
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    (dialog,) = dialogs.select(["task_cost"])
    row = dialogs.run_dialog(Api(), dialog, database_url="disposable", project_id="project-a",
                             scenarios=scenarios, mode=mode, timeout=1)
    assert row["outcome"] == "pass", row
    assert seeded == [("disposable", "project-a")]


# The calls of each turn (tool:status) that some dialogs' turns are checked for.
_DIALOG_CALLS = {
    "cancel_confirmation": [["looper_task_control:needs_confirmation"], [], ["looper_task_control:needs_confirmation"],
                            ["looper_task_control:ok"]],
    "schedules": [["looper_schedules:ok"], ["looper_schedules:ok"], ["looper_schedules:needs_confirmation"],
                  ["looper_schedules:ok"]],
    "schedule_create": [["looper_schedules:ok"], ["looper_schedules:needs_confirmation"], ["looper_schedules:ok"]],
    "schedule_enable_declined": [["looper_schedules:ok"], ["looper_schedules:needs_confirmation"], []],
    "schedule_modify": [["looper_schedules:ok"], ["looper_schedules:needs_confirmation"], []],
    "schedule_run": [["looper_schedules:ok"], ["looper_schedules:needs_confirmation"], ["looper_schedules:ok"]],
    "schedule_ambiguous": [["looper_schedules:error (invalid)"], ["looper_schedules:needs_confirmation"],
                           ["looper_schedules:ok"]],
    "config_export": [["looper_config_export:ok"], ["looper_config_export:error (forbidden)",
                                                    "looper_config_export:ok"]],
    "scheduler_settings": [["looper_settings:needs_confirmation"], ["looper_settings:ok"]],
    "schedule_timeout": [["looper_schedules:ok"]], "schedule_timeout_unnamed": [["looper_schedules:ok"]],
    "concurrency": [["looper_concurrency:ok"], []],
}
# One turn's calls: (the turn's index, its calls).
_DIALOG_TURN_CALLS = {
    # The key the user pasted: the call passing it is refused, the one without it registers the provider.
    "provider_key": (0, ["looper_providers:error (forbidden)", "looper_providers:ok", "looper_models:ok"]),
    "config_tools": (-2, ["looper_projects:needs_confirmation"]),
    "rename_task": (0, ["looper_task_update:ok"]),
}


def _isolated(dialog: Any) -> Any:
    """The dialog as the run below takes it: ``alone`` checks that its server has no task but its own."""
    from evals.prompt_regression import dialogs

    def alone(look: dialogs.Look) -> str | None:
        found = look.api.get("/api/tasks")["tasks"]
        return f"the dialog's server has {len(found)} tasks, not its own one" if len(found) != 1 else None

    if dialog.id == "provider_key":
        first = dialog.says[0]
        # Keep the forbidden key attempt on the current provider body,
        # followed by the successful account and model registrations.
        forbidden = ("looper_providers", {**first.fake[0][1], "api_key": dialogs.FAKE_KEY})
        first = dataclasses.replace(first, fake=(forbidden, *first.fake))
        return dataclasses.replace(dialog, says=(first, *dialog.says[1:]))
    if dialog.id != "create_start_stop":
        return dialog
    first = dialog.says[0]
    check = dataclasses.replace(first, check=lambda look: alone(look) or first.check(look))
    return dataclasses.replace(dialog, says=(check, *dialog.says[1:]))


def _run_fake_dialogs(tmp_path: Path, *cases: Any) -> tuple[dict[str, dict[str, Any]], dict[str, Any], Path]:
    """The dialogs through a real server in fake mode: (their rows by id, the run, the file it was written to)."""
    from evals.prompt_regression import dialogs

    result, path = dialogs.run(mode="fake", dialogs=cases, admin_url=str(DSN), out_dir=tmp_path,
                               agent="claude-code/opus")
    assert result["aborted"] is None
    assert [row["id"] for row in result["dialogs"]] == [dialog.id for dialog in cases]
    return {dialog["id"]: dialog for dialog in result["dialogs"]}, result, path


def _the_dialogs_were_written(result: dict[str, Any], path: Path, tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    table = dialogs.markdown(result)
    for dialog in result["dialogs"]:  # a failing dialog's problems follow its outcome
        shown = f"{dialog['outcome']} | {len(dialog['turns'])} |" if dialog["outcome"] == "pass" else "fail: "
        assert f"| {dialog['id']} | 1 | {shown}" in table
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


def _the_calls_of(dialog_id: str, row: dict[str, Any]) -> None:
    if dialog_id in _DIALOG_CALLS:
        assert [turn["calls"] for turn in row["turns"]] == _DIALOG_CALLS[dialog_id]
    if dialog_id in _DIALOG_TURN_CALLS:
        index, calls = _DIALOG_TURN_CALLS[dialog_id]
        assert row["turns"][index]["calls"] == calls


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("dialog", MANAGER_DIALOGS, ids=lambda dialog: dialog.id)
def test_the_managers_dialog_passes_in_fake_mode(tmp_path: Path, dialog: Any) -> None:
    cases = (_isolated(dialog),)
    if dialog.id == "create_start_stop":
        # The preceding dialog created a task. The next server must contain only its own task.
        ids = [case.id for case in MANAGER_DIALOGS]
        assert "config_export" in ids
        assert ids.index("create_start_stop") == ids.index("confirm_create_start") + 1
        first = next(case for case in MANAGER_DIALOGS if case.id == "confirm_create_start")
        cases = (first, *cases)
    by_id, result, path = _run_fake_dialogs(tmp_path, *cases)
    for case in cases:
        row = by_id[case.id]
        assert row["outcome"] == "pass", (case.id, row["problems"])
        assert row["rep"] == 1 and row["cost_usd"] > 0
        _the_calls_of(case.id, row)
    _the_dialogs_were_written(result, path, tmp_path)


def _hasty() -> Any:
    from evals.prompt_regression import dialogs

    # The stand-in enables a schedule in the turn that created it: Looper refuses, and it stays disabled.
    created = next(d for d in dialogs.DIALOGS if d.id == "schedule_create").says[0]
    return dialogs.Dialog("hasty", (dataclasses.replace(created, fake=(
        *created.fake, ("looper_schedules", {"action": "enable", "schedule": dialogs.DEPS}))),))


def _eager() -> Any:
    from evals.prompt_regression import dialogs

    # The stand-in creates a task though the user named no project: the check sees it.
    return dialogs.Dialog("eager", (dialogs.Say("帮我写个脚本。", dialogs._asked_for_the_project, (dialogs._CREATE,)),))


def _stretch() -> Any:
    from evals.prompt_regression import dialogs

    # Asked for a project's limit, which no longer exists, the stand-in changes the default for every task instead.
    return dialogs.Dialog("stretch", (dialogs.Say(
        f"把项目 {dialogs.PROJECT} 的 Worker 并发改成 2。", dialogs._project_limit_explained,
        (("looper_settings", {"action": "set", "role": "worker", "values": {"max_concurrency": 2}}),)),))


def _wrong_one() -> Any:
    from evals.prompt_regression import dialogs

    # Told which project's schedule to run, the stand-in runs the other one: the check sees it.
    return dialogs.Dialog("wrong_one", (
        dialogs.Say(f"{dialogs.OPS} 项目里的那个。", dialogs._named_one_run_asked, (
            ("looper_schedules", {"action": "run", "schedule": dialogs.SCHEDULE, "project": dialogs.PROJECT}),)),
        dialogs.Say("确认。", dialogs._the_named_one_ran, (
            ("looper_confirm", {"confirmation_id": "<confirmation>"}),))),
        seed=dialogs._make_two_projects_schedules)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize(("make", "outcome"), [
    (_hasty, "pass"),  # the refused enabling leaves the dialog as it was
    (_eager, "fail"), (_stretch, "fail"), (_wrong_one, "fail"),
], ids=["hasty", "eager", "stretch", "wrong_one"])
def test_a_wrong_call_fails_its_dialog(tmp_path: Path, make: Any, outcome: str) -> None:
    dialog = make()
    by_id, result, path = _run_fake_dialogs(tmp_path, dialog)
    row = by_id[dialog.id]
    assert row["outcome"] == outcome
    assert bool(row["problems"]) is (outcome == "fail")
    if dialog.id == "hasty":
        assert [turn["calls"] for turn in row["turns"]] == [["looper_schedules:ok", "looper_schedules:error (conflict)"]]
    _the_dialogs_were_written(result, path, tmp_path)


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_a_dialog_without_a_reply_is_a_timeout_error_and_the_next_still_runs(tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    # The stand-in Manager answers this message two minutes late.
    stalled = dialogs.Dialog("stalled", (dialogs.Say("现在忙吗？", dialogs._answered_by_reading,
                                                     (("sleep", {"seconds": 120}),)),))
    result, path = dialogs.run(mode="fake", dialogs=(stalled, *dialogs.select(["status"])), admin_url=str(DSN),
                               out_dir=tmp_path, timeout=15)
    late, status = result["dialogs"]
    assert (late["outcome"], late["error_class"], late["error"], late["turns"]) == (
        "error", "timeout", "no reply to '现在忙吗？' within 15 s", [])
    assert status["outcome"] == "pass", status["problems"]
    assert json.loads(path.read_text(encoding="utf-8")) == result and result["aborted"] is None


@pytest.mark.parametrize(("reply", "raw"), [
    ("任务已创建，还没有开始（Ready — not started）：\n- 任务：hello", False),
    ('好的。{"action": "task_start", "task_id": "task-1"}', True),
    ('```json\n{"status": "ok"}\n```', True),
    ('[{"status": "ok", "result": {}}]', True),
])
def test_a_dialog_reply_must_not_show_raw_json(reply: str, raw: bool) -> None:
    from evals.prompt_regression.dialogs import _raw_json

    assert (_raw_json(reply) is not None) is raw


class _StopApi:
    """The Manager's run records, as ``_check_manager_run`` reads them."""

    def __init__(self, status: str, via: str | None) -> None:
        metadata = {"user_stop": {"via": via}} if via else {}
        self.record = {"id": 5, "status": status, "error": None, "metadata": metadata}

    def get(self, path: str, **_: Any) -> dict[str, Any]:
        if path.endswith("/events"):
            return {"events": [{"seq": 1, "event": {"type": "result", "subtype": "success", "is_error": False}}]}
        return {"records": [self.record]}


def test_a_stop_say_accepts_only_a_run_the_web_stop_interrupted() -> None:
    from evals.prompt_regression import dialogs

    dialogs._check_manager_run(_StopApi("interrupted", "web"), 0, stop=True)
    for status, via in (("completed", None), ("interrupted", "telegram"), ("completed", "web")):
        with pytest.raises(runner.EvalError, match="the turn was not stopped"):
            dialogs._check_manager_run(_StopApi(status, via), 0, stop=True)
    with pytest.raises(runner.EvalError, match="interrupted"):
        dialogs._check_manager_run(_StopApi("interrupted", "web"), 0)


def test_the_stop_dialog_checks_its_replies() -> None:
    from evals.prompt_regression import dialogs

    def look(reply: str) -> Any:
        return dialogs.Look(None, None, reply, "disposable", 0, frozenset(), True)  # type: ignore[arg-type]

    assert dialogs._was_stopped(look("一些内容\n\n（已停止）")) is None
    assert dialogs._was_stopped(look("已停止，这次没有回复。")) is None
    assert dialogs._was_stopped(look("已处理（looper_projects：ok）。")) is not None


@pytest.mark.parametrize("reply,reads,problem", [
    ("汇总已经完成，还没有别的问题", (), "the reply claims the summary was finished"),
    ("还没有真正查询任何项目，之后再汇总。", (), None),
    ("刚才只做到加载工具，还没有真正查询任何项目或任务。要我现在继续查询，然后汇总吗？", (), None),
    ("汇总：项目 eval-calc 最近没有任务。",
     (("looper_projects", {"action": "list"}),
      ("looper_tasks", {"action": "list", "project": "eval-calc", "limit": 3})), None),
    ("汇总：项目 eval-calc 最近没有任务。", (), "the reply claims the summary was finished"),
    ("我已经逐个查看并汇总好了。", (), "the reply claims the summary was finished"),
    ("我已经查看了每个项目最近的三个任务，汇总完成。",
     (("looper_projects", {"action": "list"}),), "the reply claims the summary was finished"),
    ("我已经查看了每个项目最近的三个任务，汇总完成。",
     (("looper_projects", {"action": "list"}), ("looper_tasks", {"action": "list"})),
     "the reply claims the summary was finished"),
    ("我已经查看了每个项目最近的三个任务，汇总完成。",
     (("looper_projects", {"action": "list"}),
      ("looper_tasks", {"action": "list", "project": "eval-calc", "limit": 3})), None),
])
def test_the_stop_dialog_flags_only_a_summary_no_read_backs(
        monkeypatch: pytest.MonkeyPatch, reply: str, reads: tuple[tuple[str, dict[str, Any]], ...],
        problem: str | None) -> None:
    from evals.prompt_regression import dialogs

    class Api:
        def get(self, path: str, **params: Any) -> dict[str, Any]:
            return {"records": [{"id": 5, "input_text": f"{dialogs.STOPPED_NOTE} ...\n\n刚才停在哪了？",
                                 "metadata": {}}]}

    # The stopped turn's read cannot back this turn's summary.
    trail = [{"id": 1, "tool": "looper_projects", "risk": "read", "status": "ok",
              "arguments": {"action": "list"}}] + [
        {"id": 2 + i, "tool": tool, "risk": "read", "status": "ok", "arguments": arguments}
        for i, (tool, arguments) in enumerate(reads)]
    monkeypatch.setattr(dialogs, "_trail", lambda _url, since, *_: [a for a in trail if a["id"] > since])
    look = dialogs.Look(Api(), None, reply, "disposable", 0, frozenset(), False,  # type: ignore[arg-type]
                        turn_since=1)
    assert dialogs._knows_it_was_stopped(look) == problem


def test_a_dialog_check_sees_the_calls_of_its_own_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    trail: list[dict[str, Any]] = []
    asked: list[str] = []

    def ask(api: Any, session: str, text: str, **_: Any) -> str:
        asked.append(text)
        trail.append({"id": len(trail) + 1, "tool": f"looper_{len(asked)}", "risk": "read", "status": "ok",
                      "arguments": {}, "error_code": None, "result": None})
        return "ok"

    seen: list[tuple[list[str], list[str]]] = []

    def check(look: dialogs.Look) -> None:
        seen.append(([a["tool"] for a in look.actions()], [a["tool"] for a in look.turn_actions()]))

    monkeypatch.setattr(dialogs, "_ask", ask)
    monkeypatch.setattr(dialogs, "_last_action", lambda *_: len(trail))
    monkeypatch.setattr(dialogs, "_trail", lambda _url, since, *_: [a for a in trail if a["id"] > since])
    monkeypatch.setattr(dialogs, "_stop_leftovers", lambda *_: None)
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text("{}", encoding="utf-8")
    dialog = dialogs.Dialog("two_turns", (dialogs.Say("一", check), dialogs.Say("二", check)))

    class Api:
        def get(self, path: str, **params: Any) -> dict[str, Any]:
            return {"tasks": []}

    row = dialogs.run_dialog(Api(), dialog, database_url="disposable", project_id="project-a",  # type: ignore[arg-type]
                             scenarios=scenarios, mode="fake", timeout=1)
    assert row["outcome"] == "pass", row
    assert seen == [(["looper_1"], ["looper_1"]), (["looper_1", "looper_2"], ["looper_2"])]


def test_a_withdraw_say_accepts_no_run_or_the_run_the_web_stop_interrupted() -> None:
    from evals.prompt_regression import dialogs

    class NoRuns:
        def get(self, path: str, **params: Any) -> dict[str, Any]:
            return {"records": []}

    dialogs._check_manager_run(NoRuns(), 0, withdraw=True)  # type: ignore[arg-type]
    dialogs._check_manager_run(_StopApi("interrupted", "web"), 0, withdraw=True)
    for status, via in (("completed", None), ("interrupted", "telegram"), ("completed", "web")):
        with pytest.raises(runner.EvalError, match="the turn was not stopped"):
            dialogs._check_manager_run(_StopApi(status, via), 0, withdraw=True)
    with pytest.raises(runner.EvalError, match="without a new run record"):
        dialogs._check_manager_run(NoRuns(), 0)  # type: ignore[arg-type]


def test_the_withdraw_dialog_checks_that_the_second_input_starts_with_the_note_and_the_reply_claims_no_work() -> None:
    from evals.prompt_regression import dialogs

    def look(input_text: str, reply: str, fake: bool = False) -> Any:
        class Api:
            def get(self, path: str, **params: Any) -> dict[str, Any]:
                return {"records": [{"id": 5, "input_text": input_text, "metadata": {}}]}

        return dialogs.Look(Api(), None, reply, "disposable", 0, frozenset(), fake)  # type: ignore[arg-type]

    note = f'{dialogs.WITHDREW_NOTE} a message before you saw it (it began: "..."). Do not act on it.\n\n我刚才让你做什么了？'
    assert dialogs._knows_it_was_withdrawn(look(note, "你撤回了一条消息，我没有看到，所以没有做。")) is None
    assert dialogs._knows_it_was_withdrawn(look("[RESUME] ...\n\n" + note, "没做。")) is not None
    assert dialogs._knows_it_was_withdrawn(look(dialogs.STOPPED_NOTE + " ...", "没做。")) is not None
    assert dialogs._knows_it_was_withdrawn(look(note, "我已经逐个查看并汇总好了。")) is not None
    assert dialogs._knows_it_was_withdrawn(look(note, "已处理。", fake=True)) is None


def test_the_withdraw_dialog_first_turn_may_count_the_projects_from_its_message() -> None:
    """Every Manager message starts with the projects, so a real Manager can answer the count without a tool."""
    from evals.prompt_regression import dialogs

    class Api:
        def get(self, path: str, **params: Any) -> dict[str, Any]:
            assert path == "/api/projects"
            return {"projects": [{"name": dialogs.PROJECT}]}

    def look(reply: str) -> Any:
        return dialogs.Look(Api(), None, reply, "disposable", 0, frozenset(), False)  # type: ignore[arg-type]

    (dialog,) = dialogs.select(["withdraw_then_continue"])
    check = dialog.says[0].check
    assert check(look(f"现在有 1 个项目：**{dialogs.PROJECT}**（默认目录：`<eval-root>/work/calc`）。")) is None
    assert check(look("现在有 1 个项目。")) is not None
    assert check(look("")) is not None


def test_the_withdraw_dialog_withdraws_its_second_say_only() -> None:
    from evals.prompt_regression import dialogs

    (dialog,) = dialogs.select(["withdraw_then_continue"])
    assert [say.withdraw for say in dialog.says] == [False, True, False]
    assert dialog.says[1].text == dialogs.WITHDRAWN_ASK and not dialog.says[1].stop


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="disposable PostgreSQL unavailable")
def test_the_fake_withdraw_dialog_passes(tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    result, _ = dialogs.run(mode="fake", dialogs=dialogs.select(["withdraw_then_continue"]),
                            admin_url=os.environ["LOOPER_TEST_DATABASE_URL"], out_dir=tmp_path, timeout=30)
    assert result["aborted"] is None, result
    assert [row["outcome"] for row in result["dialogs"]] == ["pass"], result["dialogs"]


@pytest.mark.skipif(not os.environ.get("LOOPER_TEST_DATABASE_URL"), reason="disposable PostgreSQL unavailable")
def test_the_fake_stop_dialog_passes(tmp_path: Path) -> None:
    from evals.prompt_regression import dialogs

    result, _ = dialogs.run(mode="fake", dialogs=dialogs.select(["stop_then_continue"]),
                            admin_url=os.environ["LOOPER_TEST_DATABASE_URL"], out_dir=tmp_path, timeout=30)
    assert result["aborted"] is None, result
    assert [row["outcome"] for row in result["dialogs"]] == ["pass"], result["dialogs"]


def _tool_use(name: str, **arguments: Any) -> dict[str, Any]:
    return {"seq": 1, "event": {"type": "assistant", "parent_tool_use_id": None,
                                "message": {"content": [{"type": "tool_use", "name": name, "input": arguments}]}}}


@pytest.mark.parametrize("events,start_statuses,problem", [
    ([_tool_use("mcp__looper__looper_task_control", action="start", task_id="t")], ("ok",), None),
    ([_tool_use("mcp__looper__looper_task_control", action="start", task_id="t"),
      _tool_use("Bash", command="sleep 60")], ("ok",), "the Manager ran ['Bash'] after starting the task"),
    *[([_tool_use("use_tool", tool_name="looper__looper_task_control", tool_input=arguments),
        _tool_use("run_terminal_command", command="sleep 60")], ("ok",),
       "the Manager ran ['run_terminal_command'] after starting the task")
      for arguments in ({"action": "start", "task_id": "t"}, '{"action":"start","task_id":"t"}')],
    ([_tool_use("mcp__looper__looper_task_control", action="start", task_id="t"),
      _tool_use("Bash", command="sleep 60"),
      _tool_use("mcp__looper__looper_task_control", action="start", task_id="t")], ("ok", "error"),
     "the Manager ran ['Bash'] after starting the task"),
    ([_tool_use("mcp__looper__looper_task_control", action="start", task_id="t"),
      _tool_use("Bash", command="sleep 60"),
      _tool_use("mcp__looper__looper_task_control", action="start", task_id="t")], ("error", "ok"), None),
    ([_tool_use("mcp__looper__looper_task_control", action="start", task_id="t"),
      {"event": {"type": "assistant", "message": {"content": [
          {"type": "tool_use", "name": "looper__looper_task_control", "input": "not a dict"}]}}}], ("ok",), None),
])
def test_start_no_wait_rejects_a_native_shell_after_the_start(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]], start_statuses: tuple[str, ...], problem: str | None,
) -> None:
    from evals.prompt_regression import dialogs

    class Api:
        def get(self, path: str, **_: Any) -> dict[str, Any]:
            if path == "/api/llm-records":
                return {"records": [{"id": 7, "metadata": {}}]}
            return {"events": events if _ == {} or _.get("after") == 0 else []}

    trail = [{"tool": "looper_task_control", "status": status, "risk": "write",
              "arguments": {"action": "start", "task_id": "t"}} for status in start_statuses]
    monkeypatch.setattr(dialogs, "_trail", lambda *_: trail)
    look = dialogs.Look(Api(), "t", "", "", 0, frozenset(), True)  # type: ignore[arg-type]
    assert dialogs._started_without_waiting(look) == problem
