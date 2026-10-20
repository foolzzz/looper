#!/usr/bin/env python3
"""Smoke the installed branch wheel without a real model or operator state.

Run with ``uv run --extra test python examples/pi/isolated-smoke.py``.
Optionally pass --database-url (or LOOPER_TEST_DATABASE_URL), --disposable-db,
--runtime-root under the OS temporary directory, and --port (0 selects a free
port). Only a fresh local database named looper_smoke_* is accepted. The caller
owns database creation/deletion. No tasks, chats or model turns are scheduled.
The development harness reuses the packaging tests' build/HTTP helpers; the
server and negative startup checks run in the installed wheel's interpreter.
Missing prerequisites are recorded as unverified and return 2; failures return
1. Evidence stays in the checkout's ignored .local directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
ROLES = ("manager", "planner", "worker", "reviewer")


@contextmanager
def managed_process(command, *, cwd, env, grace=3, **kwargs):
    """Always reap the child and terminate its entire isolated process group."""
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True, **kwargs)
    try:
        yield process
    finally:
        # Descendants can outlive the leader, so signal the group even if it exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    pass
        process.wait(timeout=5)


def validate(parser, args):
    if args.port == 8767 or not 0 <= args.port < 65536:
        parser.error("port must be 0 or an unused non-8767 TCP port")
    if args.runtime_root:
        root = Path(args.runtime_root).resolve()
        temps = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
        if not any(root != temp and root.is_relative_to(temp) for temp in temps):
            parser.error("runtime root must be inside an OS temporary directory")
        if root.exists() and (not root.is_dir() or any(root.iterdir())):
            parser.error("runtime root must be new or empty")
    if args.database_url:
        try:
            dsn = urlsplit(args.database_url)
            valid = (dsn.scheme in ("postgres", "postgresql") and dsn.hostname in ("localhost", "127.0.0.1")
                     and re.fullmatch(r"looper_smoke_[a-zA-Z0-9_]+", unquote(dsn.path.lstrip("/")))
                     and not dsn.query and not dsn.fragment and dsn.port != 8767)
        except ValueError:
            valid = False
        if not args.disposable_db or not valid:
            parser.error("DB must be explicitly marked --disposable-db and use a local looper_smoke_* database")


def support():
    spec = importlib.util.spec_from_file_location("pi_packaging_support", ROOT / "tests/test_pi_packaging.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(base, path, token, method="GET", data=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    req = urllib.request.Request(base + path, headers=headers, method=method,
                                 data=json.dumps(data).encode() if data is not None else None)
    # Never route local smoke traffic through an ambient proxy or follow redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=3) as response:
        return json.load(response)


def registry_smoke(helper, installed, root, port, dsn, output, provider_url):
    work, python, env = installed
    # Refuse an initialized DB before serve can resume anything or enable a channel.
    helper._run([str(python), "-I", "-c", '''
import asyncio, os, asyncpg
async def check():
    conn = await asyncpg.connect(os.environ['SMOKE_DSN'], timeout=5)
    try:
        count = await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")
        assert count == 0, 'smoke requires a fresh empty database'
    finally:
        await conn.close()
asyncio.run(check())
'''], cwd=work, env={**env, "SMOKE_DSN": dsn})
    root.mkdir(parents=True, exist_ok=True)
    ops = root / "ops"
    ops.mkdir(mode=0o700)
    config = ops / "server.toml"
    config.write_text('[database]\nurl = ' + json.dumps(dsn) + '\n')
    config.chmod(0o600)
    # A minimal PATH prevents startup from probing the operator's other CLIs.
    bin_dir = work / "safe-bin"
    bin_dir.mkdir()
    for name in ("pi", "node", "git"):
        binary = shutil.which(name)
        if binary:
            (bin_dir / name).symlink_to(Path(binary).resolve())
    env = {**env, "PATH": str(bin_dir) + ":/usr/bin:/bin"}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        port = sock.getsockname()[1]
    if port == 8767:
        raise RuntimeError("free port selection returned forbidden 8767")
    base = f"http://127.0.0.1:{port}"
    with (output / "serve.log").open("w") as log, managed_process(
        [str(python.parent / "looper"), "serve", "--host", "127.0.0.1", "--port", str(port),
         "--runtime-root", str(root)], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    ) as process:
        deadline = time.monotonic() + 90
        while True:
            if process.poll() is not None:
                raise RuntimeError("installed serve exited before readiness; see private serve.log")
            try:
                token = (root / "operator-token").read_text().strip()
                request(base, "/api/status", token)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("installed serve readiness timed out")
                time.sleep(0.2)
        channels = request(base, "/api/channel-configs", token)["configs"]
        assert {c["channel_type"] for c in channels} >= {"telegram", "lark", "lark_bot"}
        assert all(not c["enabled"] for c in channels if c["channel_type"] in ("telegram", "lark", "lark_bot"))
        probe = request(base, "/api/runtimes/rt-pi/probe", token, "POST", {})
        assert not probe["runtime"]["probe_error"], "pi probe failed"
        provider = request(base, "/api/providers", token, "POST", {
            "name": "isolated-smoke", "auth": "api_key", "protocols": ["openai-responses"],
            "base_url": provider_url + "/v1", "api_key": "local-key", "models": [{"name": "selected"}],
        })["provider"]
        profile = request(base, "/api/agent-profiles", token, "POST", {
            "name": "isolated-smoke", "runtime_id": "rt-pi", "provider_id": provider["provider_id"],
            "model": "selected", "agent_teams": False,
        })["profile"]
        profile_id = profile["profile_id"]
        for role in ROLES:
            request(base, f"/api/agent-settings/roles/{role}", token, "PUT", {"profile_id": profile_id})
        settings = request(base, "/api/agent-settings", token)
        for role in ROLES:
            assert settings["roles"][role]["values"]["profile_id"] == profile_id
            assert settings["roles"][role]["effective"]["profile"]["value"]["profile_id"] == profile_id
        registry = request(base, "/api/registry", token)
        saved = next(p for p in registry["profiles"] if p["profile_id"] == profile_id)
        assert saved["runtime_type"] == "pi"
        assert set(saved["roles"]) == set(ROLES)
        assert {layer["role"] for layer in saved["used_by"]["layers"]} == set(ROLES)
        (output / "registry.json").write_text(json.dumps({"profile": saved, "probe": probe}, indent=2, default=str))


def configuration_smoke(helper, installed, root, url):
    work, python, env = installed
    return helper._run([str(python), "-I", "-c", '''
import sys
from pathlib import Path
from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.library import AgentLibrary
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ApiProtocol, AuthKind, RuntimeUnsupported
from looper.agents.runtimes.pi import PiRuntime
root, url, executable = sys.argv[1:]
prompt = Path(root) / '.local/prompts/system.md'
prompt.parent.mkdir(parents=True)
prompt.write_text('Startup validation only')
for protocol, key, expected in [(ApiProtocol.OPENAI_RESPONSES, '', 'no API key'),
                                (ApiProtocol.ANTHROPIC_MESSAGES, 'local-key', 'protocol')]:
    provider = ProviderSpec(id='local', name='local', auth=AuthKind.API_KEY,
        protocols=frozenset({protocol}), base_url=url + '/v1', api_key=key)
    profile = AgentProfile('pi-test', 'pi-test', RuntimeSpec('rt-pi', 'pi', 'pi', executable), provider, 'selected', '')
    launch = Launch(AgentSettings(profile=profile), AgentLibrary())
    try:
        PiRuntime().run_command(executable, cwd=Path(root), prompt_file=prompt, launch=launch)
    except RuntimeUnsupported as error:
        assert expected in str(error), str(error)
    else:
        raise AssertionError('invalid provider reached process launch')
print('missing key and unsupported protocol rejected')
''', str(root), url, shutil.which("pi")], cwd=work, env=env)


def smoke(args, root, output, summary):
    if not shutil.which("uv"):
        summary["unverified"].append("uv not installed: wheel build and installation")
        return
    helper = support()
    work = root / "wheel"
    work.mkdir()
    installed = helper.install_wheel(work)
    _, python, env = installed
    located = helper._run([str(python), "-I", "-c", '''
import json, sys, looper
from looper.agents.runtimes.pi import bridge_extension
from looper.db.connection import SCHEMA_PATH
assert bridge_extension().is_file() and SCHEMA_PATH.is_file()
print(json.dumps({'sys.executable': sys.executable, 'looper.__file__': looper.__file__,
                  'extension': str(bridge_extension()), 'schema': str(SCHEMA_PATH)}))
'''], cwd=work, env=env)
    origin = json.loads(located.stdout)
    assert Path(origin["looper.__file__"]).is_relative_to(python.parent.parent)
    assert Path(origin["sys.executable"]) == python
    (output / "origin.json").write_text(json.dumps(origin, indent=2))
    summary["passed"].append("wheel installation and installed resource provenance")
    if not args.database_url:
        summary["unverified"].append("no disposable DB: installed serve and four-role registry readback")
    if not shutil.which("pi"):
        summary["unverified"].append("pi not installed: safe probe and startup checks")
        return
    probed = helper._run([str(python), "-I", "-c", '''
import sys
from looper.agents.runtimes.pi import PiRuntime
probe = PiRuntime().probe(sys.argv[1])
assert not probe.error, probe.error
print(probe.version)
''', shutil.which("pi")], cwd=work, env=env)
    (output / "probe.txt").write_text(probed.stdout)
    summary["passed"].append("safe isolated pi version/help probe")
    configuration_smoke(helper, installed, root / "negative", "http://127.0.0.1:1")
    summary["passed"].append("missing key and unsupported protocol rejected before launch")
    # The helper's positive control proves a failed launch cannot hide requests.
    with helper.cold_start_api() as api:
        url = f"http://127.0.0.1:{api.server_port}"
        configuration_smoke(helper, installed, root / "counted-negative", url)
        assert not api.state["requests"]
        summary["passed"].append("invalid providers rejected; model requests = 0")
        # Deliberately bind the wrong token to a local endpoint that checks it.
        api.state["token"] = "correct-local-token"
        for mode in ("json", "rpc"):
            launch_root = root / ("wrong-token-" + mode)
            command, launch_env = helper._launch(installed, launch_root, api, mode)
            with managed_process(command, cwd=launch_root, env=launch_env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                prompt = "test" if mode == "json" else json.dumps({"type": "prompt", "id": "p", "message": "test"}) + "\n"
                stdout, stderr = process.communicate(prompt, timeout=25)
                assert process.returncode != 0 and "HTTP 401" in stderr
                assert "scoped-test-token" not in stdout + stderr
            assert not api.state["requests"]
        assert len(api.state["gets"]) == 2
        summary["passed"].append("wrong bridge token rejected in JSON/RPC; model requests = 0")
        if args.database_url:
            registry_smoke(helper, installed, Path(args.runtime_root) if args.runtime_root else root / "runtime",
                           args.port, args.database_url, output, url)
            assert not api.state["requests"]
            summary["passed"].append("installed serve, disabled channels and four-role registry readback; model requests = 0")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("LOOPER_TEST_DATABASE_URL"))
    parser.add_argument("--disposable-db", action="store_true")
    parser.add_argument("--runtime-root")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    validate(parser, args)
    output = ROOT / ".local" / ("pi-smoke-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()))
    output.mkdir(parents=True, mode=0o700)
    summary = {"passed": [], "failed": [], "unverified": []}

    def expired(*_args):
        raise TimeoutError("25 minute smoke deadline exceeded")

    def interrupted(*_args):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, expired)
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    signal.alarm(25 * 60)
    try:
        with tempfile.TemporaryDirectory(prefix="looper-pi-smoke-") as temporary:
            smoke(args, Path(temporary), output, summary)
    except PermissionError as error:
        (output / "environment.log").write_text(str(error))
        summary["unverified"].append("sandbox denied local listener/process access: HTTP model counter, wrong-token JSON/RPC startup and registry smoke")
    except KeyboardInterrupt:
        summary["failed"].append("interrupted; subprocess cleanup completed")
    except Exception as error:
        # Raw exceptions can contain DSNs or subprocess output; retain them privately.
        (output / "failure.log").write_text(str(error))
        summary["failed"].append(type(error).__name__ + ": see private failure.log")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        signal.signal(signal.SIGTERM, previous_term)
        (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print("Evidence: " + str(output.relative_to(ROOT)))
    return 1 if summary["failed"] else 2 if summary["unverified"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
