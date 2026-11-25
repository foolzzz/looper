"""Opt-in, offline real-CLI launch checks; no provider credentials are used."""

# Matrix: Claude Code 2.1.332; 2026-11-24. See the PR for the recorded observations.
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fake_anthropic_messages import FakeAnthropicMessages, text, tool_use
from looper.agents import claude_cli
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.library import EMPTY_LIBRARY
from looper.agents.outbound_hook import DENY_OUTPUT, ToolHook
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.runtimes.claude_code import ClaudeCodeRuntime


def test_fake_server_script():
    server = FakeAnthropicMessages()
    url, log = server.start([tool_use("Bash", {"command": "echo ok"}), text("done")])
    try:
        def post(path, body):
            with urlopen(Request(url + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": "test"})) as response:
                return response.read().decode()

        body = {"model": "probe", "stream": True, "tools": [{"name": "Bash"}],
                "messages": [{"role": "user", "content": "probe"}]}
        assert '"text": "ok"' in post("/v1/messages", {"messages": []})
        turns = []
        for _ in range(2):
            events = [json.loads(line[6:]) for line in post("/v1/messages", body).splitlines()
                      if line.startswith("data: ")]
            assert events[0]["type"] == "message_start"
            assert events[-1]["type"] == "message_stop"
            assert [event["type"] for event in events] == [
                "message_start", "content_block_start", "content_block_delta", "content_block_stop",
                "message_delta", "message_stop"]
            turns.append(events)
            body["messages"].extend([{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": events[1]["content_block"].get("id"), "content": "ok"}]},
                {"role": "system", "content": "Trailing runtime context"}])
        assert turns[0][1]["content_block"]["name"] == "Bash"
        assert json.loads(turns[0][2]["delta"]["partial_json"]) == {"command": "echo ok"}
        assert turns[1][2]["delta"]["text"] == "done"
        assert json.loads(post("/v1/messages/count_tokens", {})) == {"input_tokens": 1}
        with pytest.raises(HTTPError, match="404"):
            post("/unknown", {})
        assert len(log) == 5
        assert log[-1]["path"] == "/unknown"
    finally:
        server.stop()


class Probe:
    def __init__(self, root, binary, monkeypatch):
        self.root, self.binary, self.monkeypatch = root, binary, monkeypatch
        self.runs, self.observations = [], {}
        # Allowlist the child environment: never inherit auth, provider routing, proxy or CLI configuration.
        self.env = {"PATH": os.defpath, "HOME": str(root / "home"),
                    "CLAUDE_CONFIG_DIR": str(root / "config"), "TMPDIR": str(root / "tmp"),
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
                    "ANTHROPIC_API_KEY": "sk-probe-not-real",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                    "DISABLE_TELEMETRY": "1", "NO_PROXY": "127.0.0.1,localhost"}
        for name in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
            Path(self.env[name]).mkdir()
            monkeypatch.setenv(name, self.env[name])
        self.version = subprocess.check_output([binary, "--version"], env=self.env, text=True, timeout=20).strip()
        self.markers = root / "markers"
        self.markers.mkdir()
        self.hook = root / "hook.py"
        self.hook.write_text(
            "import json, pathlib, sys\n"
            "event = json.load(sys.stdin)\n"
            "with pathlib.Path(sys.argv[1]).open('a') as out:\n"
            "    out.write(f'{sys.argv[2]} {event[\"tool_name\"]} {event[\"tool_use_id\"]}\\n')\n"
            f"if sys.argv[2] == 'settings-hook' and 'probe-deny' in event.get('tool_input', {{}}).get('command', ''):\n"
            f"    print({DENY_OUTPUT!r})\n")
        self.settings = root / "launch.json"
        self.settings.write_text(json.dumps({"crossSessionInbound": "refuse", "hooks": self.hooks("settings")}))
        self.user_settings = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        self.user = {"hooks": {**self.hooks("user"), "SessionStart": [{"hooks": [{"type": "command",
                     "command": "printf '%s' " + shlex.quote(json.dumps({"hookSpecificOutput": {
                         "hookEventName": "SessionStart", "additionalContext": "USER-SESSIONSTART-9c1e"}}))}]}]},
                     "permissions": {"allow": ["Bash(*)", "mcp__probe__*"]}}
        self.user_settings.write_text(json.dumps(self.user))
        self.cwd = self.repo("project")
        (root / "CLAUDE.md").write_text("OUTSIDE-PROJECT-MEMORY-b62e\n")

    def hooks(self, source):
        return ToolHook(" ".join(shlex.quote(part) for part in (
            sys.executable, str(self.hook), str(self.markers / f"{source}.log"), f"{source}-hook"))).settings()

    def repo(self, name):
        directory = self.root / name
        directory.mkdir()
        (directory / "CLAUDE.md").write_text("PROJECT-MEMORY-7f3a\n")
        for args in (["init", "-q"], ["add", "CLAUDE.md"],
                     ["-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
                      "commit", "-qm", "Initial probe repository"]):
            subprocess.run(["git", *args], cwd=directory, env=self.env, check=True, capture_output=True)
        return directory

    def command(self, cwd, *, read_only=False, resume=None, user_only=False):
        runtime = ClaudeCodeRuntime()
        launch = Launch(AgentSettings(profile=builtin_resolved_profile()))
        prompt = self.root / "prompt.md"
        prompt.write_text("")
        # Reuse resident framing, replacing only its isolation and permissions for these two launch shapes.
        with self.monkeypatch.context() as patch:
            patch.setattr(runtime, "isolation_args", lambda *args, **kwargs: [])
            patch.setattr(runtime, "launch_args", lambda *args, **kwargs: [])
            command = runtime.resident_command(self.binary, cwd=cwd, prompt_file=prompt, launch=launch)
        # This probe adds no instructions to Claude Code's own system prompt.
        prompt_args = claude_cli.system_prompt_args(prompt, launch.system_prompt_mode)
        position = command.index(prompt_args[0])
        del command[position:position + len(prompt_args)]
        if read_only:
            command += claude_cli.isolation_args(cwd, settings_dir=self.root / "isolated-settings")
            command += claude_cli.read_only_args(EMPTY_LIBRARY, cwd=cwd)
        else:
            # --basetemp may live below a real home. HOME alone does not stop ancestor memory loading.
            settings = json.loads(self.settings.read_text())
            settings["claudeMdExcludes"] = claude_cli._outside_memory(Path(cwd))
            self.settings.write_text(json.dumps(settings))
            command += claude_cli.permission_args() + ["--settings", str(self.settings)]
            if user_only:
                command += ["--setting-sources", "user"]
        command += claude_cli.peer_args(False)
        if resume:
            command += runtime.resume_args(resume)
        return command

    def run(self, script, *, cwd=None, **kwargs):
        cwd = cwd or self.cwd
        server = FakeAnthropicMessages()
        url, log = server.start(script)
        env = {**self.env, "ANTHROPIC_BASE_URL": url}
        command = self.command(cwd, **kwargs)
        record = {"command": command, "cwd": str(cwd), "environment": env, "requests": log, "script": script}
        self.runs.append(record)
        try:
            result = subprocess.run(command, cwd=cwd, env=env, input=json.dumps({
                "type": "user", "message": {"role": "user", "content": "Run the probe."}}) + "\n",
                text=True, capture_output=True, timeout=90)
            record.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired as error:
            record.update(timeout=True, stdout=str(error.stdout), stderr=str(error.stderr))
            raise
        finally:
            server.stop()
        record["events"] = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        assert all(request["api_key"] == "sk-probe-not-real" for request in log)
        assert all("OUTSIDE-PROJECT-MEMORY-b62e" not in json.dumps(request) for request in log)
        return record

    def success(self, run):
        assert run["exit_code"] == 0, run["stderr"]
        assert any(request["has_tools"] for request in run["requests"])
        assert any(event.get("type") == "result" and not event.get("is_error") for event in run["events"])

    def lines(self, source, tool="Bash", tool_id=None):
        if tool_id is None:
            tool_id = next(turn["id"] for turn in self.runs[-1]["script"] if turn.get("name") == tool)
        path = self.markers / f"{source}.log"
        return [line for line in path.read_text().splitlines() if line == f"{source}-hook {tool} {tool_id}"] \
            if path.exists() else []

    def session(self, run):
        return next(event["session_id"] for event in run["events"]
                    if event.get("type") == "system" and event.get("subtype") == "init")

    def results(self, run):
        return [block for request in run["requests"] for message in request["messages"]
                if isinstance(message.get("content"), list) for block in message["content"]
                if block.get("type") == "tool_result"]


@pytest.fixture
def probe(tmp_path, monkeypatch, request):
    binary = os.environ.get("LOOPER_CLAUDE_PROBE_BIN")
    if not binary:
        pytest.skip("Set LOOPER_CLAUDE_PROBE_BIN to opt into the offline real-CLI probe; a skip is not a pass")
    instance = Probe(tmp_path, str(Path(binary).resolve()), monkeypatch)
    try:
        yield instance
    finally:
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps({"check": request.node.name, "version": instance.version,
            "environment": instance.env, "observations": instance.observations, "runs": instance.runs,
            "markers": {p.name: p.read_text() for p in instance.markers.iterdir()}}, indent=2))
        print(f"Evidence: {evidence}")


@pytest.mark.installed
def test_a_both_hooks(probe):
    run = probe.run([tool_use("Bash", {"command": "echo probe-a > a.txt"}), text("done")])
    probe.success(run)
    assert (probe.cwd / "a.txt").read_text().strip() == "probe-a"
    assert len(probe.lines("settings")) == len(probe.lines("user")) == 1


@pytest.mark.installed
def test_b_denial_reaches_model(probe):
    run = probe.run([tool_use("Bash", {"command": "echo probe-deny > b.txt"}), text("done")])
    probe.success(run)
    assert not (probe.cwd / "b.txt").exists()
    assert len(probe.lines("settings")) == len(probe.lines("user")) == 1
    reason = json.loads(DENY_OUTPUT)["hookSpecificOutput"]["permissionDecisionReason"]
    assert any(reason in json.dumps(block) for block in probe.results(run))
    assert reason in run["stdout"]


@pytest.mark.installed
def test_c_user_and_project_context(probe):
    run = probe.run([text("done")])
    probe.success(run)
    first = json.dumps(run["requests"][0])
    assert "USER-SESSIONSTART-9c1e" in first
    assert "PROJECT-MEMORY-7f3a" in first


def disable_project_hooks(probe, *, committed=False):
    directory = probe.cwd / ".claude"
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
    if committed:
        subprocess.run(["git", "add", ".claude/settings.json"], cwd=probe.cwd, env=probe.env, check=True)
        subprocess.run(["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid",
                        "commit", "-qm", "Disable project hooks"], cwd=probe.cwd, env=probe.env,
                       check=True, capture_output=True)


@pytest.mark.installed
@pytest.mark.parametrize("case", ["D1", "D2", "D3", "D4-committed", "D4-written"])
def test_d_hook_settings(probe, case):
    dynamic = case in ("D3", "D4-written")
    if case == "D1":
        probe.user["disableAllHooks"] = True
        probe.user_settings.write_text(json.dumps(probe.user))
    elif not dynamic:
        disable_project_hooks(probe, committed=True)
    script = ([tool_use("Write", {"file_path": str(probe.cwd / ".claude/settings.json"),
                                 "content": '{"disableAllHooks": true}'})] if dynamic else [])
    run = probe.run([*script, tool_use("Bash", {"command": "echo observed > d.txt"}), text("done")],
                    user_only=case.startswith("D4"))
    probe.success(run)
    assert (probe.cwd / "d.txt").read_text().strip() == "observed"
    if dynamic:
        assert json.loads((probe.cwd / ".claude/settings.json").read_text()) == {"disableAllHooks": True}
        assert len(probe.lines("settings", "Write")) == 1
    first = next(request for request in run["requests"] if request["has_tools"])
    probe.observations.update(settings_hook_survived=bool(probe.lines("settings")),
                              project_memory="PROJECT-MEMORY-7f3a" in json.dumps(first))


@pytest.mark.installed
def test_e_project_hook_merge(probe):
    directory = probe.cwd / ".claude"
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({"hooks": probe.hooks("project")}))
    run = probe.run([tool_use("Bash", {"command": "echo observed > e.txt"}), text("done")])
    probe.success(run)
    assert (probe.cwd / "e.txt").exists()
    probe.observations.update(project_hook_ran=bool(probe.lines("project")),
                              settings_hook_ran=bool(probe.lines("settings")))
    assert len(probe.lines("project")) == len(probe.lines("settings")) == 1


@pytest.mark.installed
def test_f_resume_other_directory(probe):
    first = probe.run([text("first")])
    probe.success(first)
    session = probe.session(first)
    resumed = probe.run([text("resumed")], cwd=probe.repo("other-project"), resume=session)
    probe.observations.update(original_session=session, resume_exit=resumed["exit_code"],
        same_session=any(event.get("session_id") == session and event.get("subtype") == "init"
                         for event in resumed["events"]), message=resumed["stdout"] + resumed["stderr"])


@pytest.mark.installed
def test_g_read_only_resume(probe):
    target = probe.cwd / "owned.txt"
    first = probe.run([tool_use("Write", {"file_path": str(target), "content": "original\n"}), text("done")])
    probe.success(first)
    assert target.read_text() == "original\n"
    before = {str(p.relative_to(probe.cwd)): p.read_bytes() for p in probe.cwd.rglob("*")
              if p.is_file() and ".git" not in p.relative_to(probe.cwd).parts}
    resumed = probe.run([tool_use("Write", {"file_path": str(target), "content": "changed\n"}),
        tool_use("Bash", {"command": "echo changed > shell.txt"}),
        tool_use("Edit", {"file_path": str(target), "old_string": "original", "new_string": "changed"}),
        text("done")], read_only=True, resume=probe.session(first))
    probe.success(resumed)
    assert probe.session(resumed) == probe.session(first)
    assert {str(p.relative_to(probe.cwd)): p.read_bytes() for p in probe.cwd.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(probe.cwd).parts} == before
    errors = {block["tool_use_id"] for block in probe.results(resumed) if block.get("is_error")}
    assert {turn["id"] for turn in resumed["script"] if turn["type"] == "tool_use"} <= errors
    assert len(probe.lines("user", "Write", first["script"][0]["id"])) == 1
    assert not probe.lines("user", "Write")


@pytest.mark.installed
def test_h_peer_tools_refused(probe):
    run = probe.run([tool_use("ListAgents", {}), tool_use("SendMessage", {
        "type": "message", "recipient": "probe-nonexistent", "content": "probe"}), text("done")])
    probe.success(run)
    errors = {block["tool_use_id"] for block in probe.results(run) if block.get("is_error")}
    assert {turn["id"] for turn in run["script"] if turn["type"] == "tool_use"} <= errors
