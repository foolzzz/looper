"""Fixtures for the offline real-CLI probes of the task Manager's direct-session sandbox boundary.

``SandboxProbe`` runs the real ``CLAUDE_CODE.operator_resident_command`` (never a hand-built copy) against a scripted
local Messages API, with a scratch ``HOME``/``CLAUDE_CONFIG_DIR``/``TMPDIR`` and an allowlisted child environment: no
real model, no operator configuration. Cards that probe the same boundary import it.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fake_anthropic_messages import FakeAnthropicMessages, text, tool_use  # noqa: F401  (re-exported for the probes)
from looper.agents import operator_mcp, task_manager_sandbox
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.library import EMPTY_LIBRARY, AgentLibrary
from looper.agents.profile_registry import builtin_resolved_profile
from looper.agents.runtimes import CLAUDE_CODE

# A minimal stdio MCP server: it records the directory it started in and offers one tool.
MCP_SERVER = r'''
import json, os, sys
marker = sys.argv[1]
with open(marker, "a") as out:
    out.write("cwd=" + os.path.realpath(os.getcwd()) + "\n")
for line in sys.stdin:
    message = json.loads(line)
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "probe", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "echoed"}]}
    elif ident is None:
        continue
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
'''


class Listener:
    """A loopback TCP listener that counts connections: what a sandboxed command must never reach."""

    def __init__(self) -> None:
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.connections: list[bytes] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            try:
                connection.settimeout(1)
                try:
                    first = connection.recv(512)
                except OSError:
                    first = b""
                with self._lock:
                    self.connections.append(first)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\nlistener-ok")
            except OSError:
                pass
            finally:
                connection.close()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.connections)

    def close(self) -> None:
        self.server.close()


@dataclass
class Run:
    command: list[str]
    cwd: Path
    exit_code: int | None
    events: list[dict[str, Any]]
    requests: list[dict[str, Any]]
    script: list[dict[str, Any]]
    stderr: str = ""
    timed_out: bool = False
    control: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def init(self) -> dict[str, Any]:
        return next(event for event in self.events if event.get("type") == "system" and event.get("subtype") == "init")

    @property
    def session_id(self) -> str:
        return self.init["session_id"]

    def results(self) -> list[str]:
        """The text of every tool result, in order."""
        found = []
        for event in self.events:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = block.get("content")
                    found.append(body if isinstance(body, str) else json.dumps(body))
        return found

    def result_of(self, tool_use_id: str) -> str:
        for event in self.events:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and \
                        block.get("tool_use_id") == tool_use_id:
                    body = block.get("content")
                    return body if isinstance(body, str) else json.dumps(body)
        return ""

    def finished(self) -> bool:
        return any(event.get("type") == "result" for event in self.events)


class Session:
    """A resident stream-json process kept across turns, its output in files (a sandboxed child that inherits a pipe
    would keep a reader waiting forever)."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str], root: Path, name: str) -> None:
        self.out, self.err = root / f"{name}.out", root / f"{name}.err"
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=self.out.open("w"), stderr=self.err.open("w"),
            text=True, start_new_session=True)

    def send(self, value: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def user(self, content: str) -> None:
        self.send({"type": "user", "message": {"role": "user", "content": content}})

    def events(self) -> list[dict[str, Any]]:
        found = []
        for line in self.out.read_text().splitlines():
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
        return found

    def wait(self, predicate, timeout: float = 90.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            if (found := predicate(events)) :
                return found
            if self.process.poll() is not None:
                events = self.events()
                if (found := predicate(events)):
                    return found
                raise AssertionError(f"the CLI exited ({self.process.returncode}): {self.err.read_text()[-1500:]}")
            time.sleep(0.2)
        raise AssertionError("timed out waiting for the CLI; stderr: " + self.err.read_text()[-1500:])

    def wait_results(self, count: int, timeout: float = 90.0) -> list[dict[str, Any]]:
        return self.wait(lambda events: (rows if len(rows := [e for e in events if e.get("type") == "result"]) >= count
                                         else None), timeout)

    def control(self, subtype: str, request_id: str, **fields: Any) -> dict[str, Any]:
        self.send({"type": "control_request", "request_id": request_id, "request": {"subtype": subtype, **fields}})
        return self.wait(lambda events: next((e for e in events if e.get("type") == "control_response"
                                              and (e.get("response") or {}).get("request_id") == request_id), None),
                         timeout=30)["response"]

    def close(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.process.wait(timeout=10)


class SandboxProbe:
    def __init__(self, root: Path, binary: str) -> None:
        self.root, self.binary = root, binary
        self.runs: list[Run] = []
        self.observations: dict[str, Any] = {}
        self.home, self.config, self.tmp = root / "home", root / "config", root / "tmp"
        self.runtime_root = root / "runtime"
        self.outside = root / "outside"
        self.mcp_cwd = self.runtime_root / "task-managers" / "probe" / "mcp-cwd"
        self.markers = root / "markers"
        for path in (self.home, self.config, self.tmp, self.runtime_root, self.outside, self.markers):
            path.mkdir()
        # Allowlist the child environment: never inherit auth, provider routing, proxy or CLI configuration.
        self.env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin", "HOME": str(self.home),
                    "CLAUDE_CONFIG_DIR": str(self.config), "TMPDIR": str(self.tmp),
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_API_KEY": "sk-probe-not-real",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
                    "DISABLE_TELEMETRY": "1"}
        self.version = subprocess.check_output([binary, "--version"], env=self.env, text=True, timeout=30).strip()
        self.listener = Listener()
        self.cwd = self.repo("project")
        self.user_settings = self.config / "settings.json"
        self.sessions: list[Session] = []

    # --- fixtures ---

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=Probe", "-c", "user.email=probe@example.invalid", *args],
                              cwd=cwd, env=self.env, check=True, capture_output=True, text=True).stdout

    def repo(self, name: str) -> Path:
        directory = self.root / name
        directory.mkdir()
        self.git(directory, "init", "-q")
        (directory / "a.txt").write_text("a\n")
        self.git(directory, "add", "a.txt")
        self.git(directory, "commit", "-qm", "Initial probe repository")
        return directory

    def write_user_settings(self, content: dict[str, Any]) -> None:
        self.user_settings.write_text(json.dumps(content))

    def mcp_library(self, marker: Path | None = None) -> AgentLibrary:
        """The real ``library_with_allowed`` over one stdio user-scope server, ``probe``, that records its cwd."""
        marker = marker or self.markers / "mcp-cwd.txt"
        script = self.root / "mcp_server.py"
        script.write_text(MCP_SERVER)
        (self.config / ".claude.json").write_text(json.dumps({"mcpServers": {"probe": {
            "type": "stdio", "command": sys.executable, "args": [str(script), str(marker)]}}}))
        return asyncio.run(operator_mcp.library_with_allowed(EMPTY_LIBRARY, self.runtime_root, ["probe"],
                                                             environment=self.env, cwd=self.mcp_cwd))

    # --- the command under test ---

    @property
    def read_deny(self) -> tuple[str, ...]:
        return task_manager_sandbox.read_deny_paths(self.env, self.runtime_root)

    def launch(self, library: AgentLibrary = EMPTY_LIBRARY) -> Launch:
        return Launch(AgentSettings(profile=builtin_resolved_profile()), library=library)

    def command(self, cwd: Path | None = None, *, library: AgentLibrary = EMPTY_LIBRARY, resume: str | None = None,
                allowed_domains: tuple[str, ...] = (), control: str | None = None) -> list[str]:
        """The direct session's command. ``control="loose"`` is the positive control: the same command with the
        pins removed (only ``sandbox.enabled``) and the project and local layers loading, the shape the boundary
        replaces."""
        cwd = cwd or self.cwd
        prompt = self.root / "prompt.md"
        prompt.write_text("")
        command = CLAUDE_CODE.operator_resident_command(
            self.binary, cwd=cwd, prompt_file=prompt, launch=self.launch(library), settings_dir=self.root / "settings",
            allowed_domains=allowed_domains, read_deny=self.read_deny)
        if control == "loose":
            loose = self.root / "loose-settings.json"
            loose.write_text(json.dumps({"sandbox": {"enabled": True}}))
            at = command.index("--setting-sources")
            del command[at:at + 4]  # --setting-sources user --tools Bash
            command[command.index("--settings") + 1] = str(loose)
        if resume:
            command += CLAUDE_CODE.resume_args(resume)
        return command

    def settings_file(self, command: list[str]) -> dict[str, Any]:
        return json.loads(Path(command[command.index("--settings") + 1]).read_text())

    def child_env(self, url: str) -> dict[str, str]:
        return {**self.env, "ANTHROPIC_BASE_URL": url}

    # --- running it ---

    def run(self, script: list[dict[str, Any]], *, cwd: Path | None = None, timeout: float = 120.0,
            message: str = "Run the probe.", command: list[str] | None = None, **kwargs: Any) -> Run:
        """One user message, then end of input: the CLI answers and exits. Output goes to files."""
        cwd = cwd or self.cwd
        server = FakeAnthropicMessages()
        url, log = server.start(script)
        command = command if command is not None else self.command(cwd, **kwargs)
        out, err = self.root / f"run{len(self.runs)}.out", self.root / f"run{len(self.runs)}.err"
        timed_out = False
        process = subprocess.Popen(command, cwd=cwd, env=self.child_env(url), stdin=subprocess.PIPE,
                                   stdout=out.open("w"), stderr=err.open("w"), text=True, start_new_session=True)
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": message}}) + "\n")
            process.stdin.close()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=10)
            server.stop()
        events = []
        for line in out.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        run = Run(command, cwd, process.returncode, events, log, script, err.read_text(), timed_out)
        self.runs.append(run)
        assert not timed_out, f"timed out; stderr: {run.stderr[-1500:]}"
        assert all(request["api_key"] == "sk-probe-not-real" for request in log)
        return run

    def session(self, script: list[dict[str, Any]], *, cwd: Path | None = None,
                command: list[str] | None = None, **kwargs: Any) -> tuple[Session, list[dict[str, Any]]]:
        """A resident process for several turns; the scripted model serves ``script`` across them."""
        cwd = cwd or self.cwd
        server = FakeAnthropicMessages()
        url, log = server.start(script)
        self.servers = [*getattr(self, "servers", []), server]
        command = command if command is not None else self.command(cwd, **kwargs)
        session = Session(command, cwd, self.child_env(url), self.root, f"session{len(self.sessions)}")
        self.sessions.append(session)
        return session, log

    def close(self) -> None:
        for session in self.sessions:
            session.close()
        for server in getattr(self, "servers", []):
            server.stop()
        self.listener.close()

    def evidence(self) -> dict[str, Any]:
        return {"version": self.version, "observations": self.observations,
                "runs": [{"command": run.command[1:], "cwd": str(run.cwd), "exit_code": run.exit_code,
                          "tools": run.init.get("tools") if any(e.get("subtype") == "init" for e in run.events) else None,
                          "results": run.results()} for run in self.runs]}
