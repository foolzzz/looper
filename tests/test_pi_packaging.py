"""Install a fresh wheel and cold-start real pi outside the checkout, using only local HTTP services."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = "looper/agents/runtimes/pi_extension/looper.ts"


def _run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    assert done.returncode == 0, done.stdout + done.stderr
    return done


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, str]]:
    work = tmp_path_factory.mktemp("pi-wheel")
    env = {"PATH": os.environ["PATH"], "HOME": str(work / "home"), "TMPDIR": str(work),
           "LANG": "C.UTF-8", "UV_CACHE_DIR": str(work / "cache")}
    # Callers can supply a disposable, pre-populated cache for offline validation.
    if "UV_CACHE_DIR" in os.environ:
        env["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    if "UV_OFFLINE" in os.environ:
        env["UV_OFFLINE"] = os.environ["UV_OFFLINE"]
    (work / "home").mkdir()
    src = work / "source"
    names = _run(["git", "ls-files", "-z"], cwd=ROOT, env=env).stdout.split("\0")
    for name in names:
        if name and (name.startswith("looper/") or name in
                     {"pyproject.toml", "README.md", "LICENSE", "NOTICE", "LICENSE-MIT"}):
            source = ROOT / name
            if source.is_file():
                target = src / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    _run(["uv", "build", "--wheel", "--out-dir", str(work / "dist")], cwd=src, env=env, timeout=90)
    wheel, = (work / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        assert RESOURCE in archive.namelist(), "the wheel must carry the tool bridge explicitly"
    python = work / "venv/bin/python"
    _run(["uv", "venv", "--python", sys.executable, str(python.parent.parent)], cwd=work, env=env)
    _run(["uv", "pip", "install", "--python", str(python), str(wheel)], cwd=work, env=env)
    # Delete the build source so a repository-relative lookup cannot accidentally pass.
    shutil.rmtree(src)
    return work, python, env


def test_installed_resource_loads_with_the_real_typescript_loader(installed: tuple[Path, Path, dict[str, str]]) -> None:
    from test_pi_tool_bridge import _pi_package

    work, python, env = installed
    done = _run([str(python), "-I", "-c", '''
import json
from importlib.resources import files
import looper
from looper.agents.runtimes.pi import bridge_extension
resource = files("looper.agents.runtimes").joinpath("pi_extension/looper.ts")
assert resource.is_file()
assert str(resource) == str(bridge_extension())
print(json.dumps({"module": looper.__file__, "extension": str(resource)}))
'''], cwd=work, env=env)
    located = json.loads(done.stdout)
    assert Path(located["module"]).is_relative_to(python.parent.parent)
    extension = Path(located["extension"])
    assert extension.is_relative_to(python.parent.parent) and extension.is_file()
    package = _pi_package()
    if not package or not shutil.which("node"):
        pytest.skip("installed pi and node are required; the real TS loader did not run")
    script = work / "load.mjs"
    script.write_text('''
import {pathToFileURL} from 'node:url';
const {loadExtensions} = await import(pathToFileURL(process.argv[2]));
const result = await loadExtensions([process.argv[3]], process.cwd());
if (result.errors.length) throw new Error(JSON.stringify(result.errors));
if (result.extensions.length !== 1) throw new Error('bridge was not loaded');
console.log(JSON.stringify([...result.extensions[0].tools.keys()]));
''')
    loader_env = {**env, "PI_CODING_AGENT_DIR": str(work / "empty-agent"),
                  "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1"}
    loaded = _run(["node", str(script), str(package / "dist/core/extensions/loader.js"), str(extension)],
                  cwd=work, env=loader_env)
    assert json.loads(loaded.stdout) == []  # An unbound bridge still loads successfully.


class _ColdStartServer(ThreadingHTTPServer):
    state: dict[str, Any]


class _ColdStartAPI(BaseHTTPRequestHandler):
    server: _ColdStartServer

    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        state = self.server.state
        state["gets"].append((self.path, self.headers.get("Authorization")))
        status, body = state["catalog"]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.state["requests"].append({"path": self.path, "body": body})
        response = {"id": "resp-test", "status": "completed", "output": [],
                    "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1}}
        data = ("event: response.completed\ndata: " + json.dumps(
            {"type": "response.completed", "response": response}) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def api() -> Iterator[_ColdStartServer]:
    try:
        server = _ColdStartServer(("127.0.0.1", 0), _ColdStartAPI)
    except PermissionError:
        pytest.skip("local HTTP listeners unavailable; cold-start not verified")
    server.state = {"requests": [], "gets": [], "catalog": (200, {"tools": []})}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Prove the provider counter works before asserting zero on failed startup.
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("POST", "/v1/responses", "{}", {"Content-Type": "application/json"})
            assert client.getresponse().read()
            assert len(server.state["requests"]) == 1
            server.state["requests"].clear()
        finally:
            client.close()
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _launch(installed: tuple[Path, Path, dict[str, str]], root: Path, api: _ColdStartServer,
            mode: str, bound: bool = True) -> tuple[list[str], dict[str, str]]:
    work, python, env = installed
    script = '''
import json, sys
from pathlib import Path
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind
from looper.agents.runtimes.pi import PiRuntime
root, url, executable, mode, bound = sys.argv[1:]
root = Path(root)
prompt = root / '.local/prompts/system.md'
prompt.parent.mkdir(parents=True)
prompt.write_text('Packaging probe')
provider = ProviderSpec(id='local', name='local', auth=AuthKind.API_KEY,
    protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}), base_url=url + '/v1', api_key='local-key')
profile = AgentProfile('pi-test', 'pi-test', RuntimeSpec('rt-pi', 'pi', 'pi', executable), provider, 'selected', '')
config = root / 'mcp.json'
config.write_text(json.dumps({'mcpServers': {'looper': {'env': {
    'LOOPER_AGENT_URL': url, 'LOOPER_AGENT_TOKEN': 'scoped-test-token'}}}}))
launch = Launch(AgentSettings(profile=profile), AgentLibrary(mcp_config=str(config) if bound == 'yes' else None))
runtime = PiRuntime()
method = runtime.steer_command if mode == 'rpc' else runtime.run_command
command = method(executable, cwd=root, prompt_file=prompt, launch=launch)
print(json.dumps({'command': command, 'env': launch.environment()}))
'''
    executable = shutil.which("pi")
    if executable is None:
        pytest.skip("pi is unavailable; real cold-start did not run")
    done = _run([str(python), "-I", "-c", script, str(root), f"http://127.0.0.1:{api.server_port}",
                 executable, mode, "yes" if bound else "no"], cwd=work, env=env)
    payload = json.loads(done.stdout)
    return payload["command"], {**payload["env"], "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}


@pytest.mark.parametrize("mode", ["json", "rpc"])
@pytest.mark.parametrize("failure", ["unauthorized", "invalid-schema"])
def test_initialization_failure_never_calls_provider(installed: tuple[Path, Path, dict[str, str]], tmp_path: Path,
                                                    api: _ColdStartServer, mode: str, failure: str) -> None:
    api.state["catalog"] = ((401, {"error": "unauthorized"}) if failure == "unauthorized" else
                            (200, {"tools": [{"name": "broken", "description": "bad", "inputSchema": []}]}))
    command, env = _launch(installed, tmp_path, api, mode)
    prompt = "test" if mode == "json" else json.dumps({"type": "prompt", "id": "p", "message": "test"}) + "\n"
    done = subprocess.run(command, input=prompt, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
    assert done.returncode != 0, done.stdout + done.stderr
    assert len(api.state["gets"]) == 1
    assert ("HTTP 401" if failure == "unauthorized" else "illegal inputSchema") in done.stderr
    assert "scoped-test-token" not in done.stdout + done.stderr
    assert api.state["requests"] == []


@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("mode", ["json", "rpc"])
def test_successful_cold_start_registers_the_complete_table(installed: tuple[Path, Path, dict[str, str]],
                                                          tmp_path: Path, api: _ColdStartServer,
                                                          bound: bool, mode: str) -> None:
    from looper.agents.ops.planner import ROLE_TOOLS, TOOLS
    from looper.agents.ops.service import MANAGER_TOOLS
    from looper.agents.ops.room import TOOLS as ROOM_TOOLS
    from looper.agents.ops.vault import TOOLS as VAULT_TOOLS

    tools = {t.name: t for t in (*MANAGER_TOOLS, *TOOLS, *ROLE_TOOLS, *ROOM_TOOLS, *VAULT_TOOLS)}
    catalog = [tool.listing() for tool in tools.values()]
    api.state["catalog"] = (200, {"tools": catalog})
    command, env = _launch(installed, tmp_path, api, mode, bound)
    if mode == "json":
        done = subprocess.run(command, input="test", cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
        assert done.returncode == 0, done.stdout + done.stderr
    else:
        # Keep stdin open until Pi settles; EOF during startup could prevent the
        # prompt from running and make a zero-request assertion meaningless.
        process = subprocess.Popen(command, cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        output: list[str] = []
        errors: list[str] = []
        settled = threading.Event()

        def read_stdout() -> None:
            for line in process.stdout:
                output.append(line)
                if json.loads(line).get("type") == "agent_settled":
                    settled.set()

        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write(json.dumps({"type": "prompt", "id": "p", "message": "test"}) + "\n")
            process.stdin.flush()
            assert settled.wait(25), "".join(output + errors)
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=2)
            process.stdout.close()
            process.stderr.close()
        assert not "".join(errors), "".join(errors)
    assert len(api.state["requests"]) == 1
    received = api.state["requests"][0]
    assert received["path"] == "/v1/responses"
    names = {t["name"] for t in received["body"]["tools"] if t["name"].startswith("mcp__looper__")}
    assert names == ({"mcp__looper__" + name for name in tools} if bound else set())
    assert api.state["gets"] == ([("/api/agent/v1/tools", "Bearer scoped-test-token")] if bound else [])
