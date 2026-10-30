"""Runner startup refusals stay local to the adapter and retain actionable causes."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from looper import ops_config as ops
from looper.agents import serve
from looper.agents.runtimes import ADAPTERS, RuntimeUnsupported
from looper.agents.runtimes import dsh_runner, pi_runner
from looper.agents.runtimes.runner import dsh_command, pi_command
from looper.agents.runtimes.runner.supervisor import RunnerUnavailable


@pytest.fixture(params=["pi", "dsh"])
def installed(tmp_path, monkeypatch, request):
    kind = request.param
    module = pi_runner if kind == "pi" else dsh_runner
    package, version = (("@earendil-works/pi-coding-agent", "1.3.0") if kind == "pi"
                        else ("@deepseek-ai/dsh", "0.2.1-rc.2"))
    root = tmp_path / "package"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"name": package, "version": version}))
    executable = root / kind
    executable.touch()
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nprintf 'v22.19.0\\n'\n")
    node.chmod(0o755)
    monkeypatch.setitem(ADAPTERS, kind, ADAPTERS[kind])
    which = shutil.which
    monkeypatch.setattr(module.shutil, "which", lambda name:
        str(node) if name == "node" else str(executable) if name == kind else which(name))
    return kind, module, node


def _diagnostic(kind, cause):
    adapter = ADAPTERS[kind]
    assert adapter.supervisor is None
    assert cause in adapter.unavailable
    assert f'[runtimes] {kind} = "cli"' in adapter.unavailable
    with pytest.raises(RuntimeUnsupported, match="runner") as caught:
        adapter.resident_command(kind, cwd=".", prompt_file=Path("absent"), launch=None)
    assert str(caught.value) == adapter.unavailable


@pytest.mark.parametrize("failure", ["missing-entry", "relative-node", "resolve", "supervisor"])
def test_enable_refusal_does_not_abort_serve(tmp_path, monkeypatch, installed, failure):
    kind, module, node = installed
    command = pi_command if kind == "pi" else dsh_command
    if failure == "missing-entry":
        # Exercise the real resource lookup and its missing-file check.
        monkeypatch.setattr(command, "files", lambda _: tmp_path / "missing-package")
        cause = "runner is missing from this installation"
    elif failure == "relative-node":
        monkeypatch.chdir(tmp_path)
        which = module.shutil.which
        monkeypatch.setattr(module.shutil, "which", lambda name: "./node" if name == "node" else which(name))
        cause = "requires an absolute Node executable"
    elif failure == "resolve":
        monkeypatch.setattr(module, "_package_version", lambda *args: "1.3.0" if kind == "pi" else "0.2.1-rc.2")
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path.name == kind:
                raise OSError("executable resolution denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        cause = "executable resolution denied"
    else:
        def invalid(*args, **kwargs):
            raise ValueError("invalid runner configuration")

        monkeypatch.setattr(module, "RunnerSupervisor", invalid)
        cause = "invalid runner configuration"
    assert module.enable(tmp_path / "runtime") is None
    _diagnostic(kind, cause)
    reached = []

    async def owned(**kwargs):
        _diagnostic(kind, cause)
        assert not kwargs.get("runner_supervisors")
        # The normal serve path still has the Claude Code profile's adapter.
        from looper.agents.runtimes import adapter

        assert adapter("claude-code").label == "Claude Code"
        reached.append(True)

    monkeypatch.setattr(serve, "_run_agent_system_owned", owned)
    config = ops.OpsConfig(runtime_root=tmp_path, path=tmp_path / ops.FILE,
        pi_runtime="runner" if kind == "pi" else "cli", dsh_runtime="runner" if kind == "dsh" else "cli")
    asyncio.run(serve._run_agent_system(ops=config, runtime_root=tmp_path, work_dir=tmp_path))
    assert reached == [True]


@pytest.mark.parametrize("failure", ["exit", "os", "timeout", "node-23"])
def test_node_probe_names_its_actual_failure(tmp_path, monkeypatch, installed, failure):
    kind, module, node = installed
    if failure == "exit":
        node.write_text("#!/bin/sh\nexit 17\n")
        cause = "returned non-zero exit status 17"
    elif failure == "node-23":
        node.write_text("#!/bin/sh\nprintf 'v23.3.0\\n'\n")
        cause = "Node 23.0–23.5 cannot run the runner (use 22.19+ or 23.6+)"
    else:
        def failed(*args, **kwargs):
            if failure == "os":
                raise OSError("node execution denied")
            raise subprocess.TimeoutExpired(args[0], 2)

        monkeypatch.setattr(module.subprocess, "run", failed)
        cause = "node execution denied" if failure == "os" else "did not finish within 2 seconds"
    assert module.enable(tmp_path / "runtime") is None
    _diagnostic(kind, cause)
    assert ("did not finish within 2 seconds" in ADAPTERS[kind].unavailable) == (failure == "timeout")


@pytest.mark.installed
@pytest.mark.parametrize("kind", ["pi", "dsh"])
def test_nested_sdk_refusal_reaches_resident_session(tmp_path, monkeypatch, kind):
    from dsh_runner_support import require_node
    from test_dsh_runner import fake_package as dsh_package
    from test_pi_runner import fake_package as pi_package

    node = require_node()
    executable = (pi_package(tmp_path, ai_version="1.4.0") if kind == "pi" else
                  dsh_package(tmp_path, dependency="0.2.1-rc.3"))
    module = pi_runner if kind == "pi" else dsh_runner
    monkeypatch.setitem(ADAPTERS, kind, ADAPTERS[kind])
    which = shutil.which
    monkeypatch.setattr(module.shutil, "which", lambda name:
        node if name == "node" else str(executable) if name == kind else which(name))
    supervisor = module.enable(tmp_path / "runtime")
    assert supervisor is not None  # The top-level package passes the pre-start gate.
    package, version = (("@earendil-works/pi-ai", "1.4.0") if kind == "pi" else
                        ("@deepseek-ai/dsh-llm", "0.2.1-rc.3"))
    from looper.agents.runtimes import ApiProtocol
    from looper.agents.runtimes.api.adapter import KEY_ENV
    from looper.agents.runtimes.runner.session import Descriptor

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Disposable instructions.")
    descriptor = Descriptor(kind=kind, role="manager", provider_id="test",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES if kind == "dsh" else ApiProtocol.OPENAI_RESPONSES,
        base_url="http://127.0.0.1:1", model="test-model", effort=None,
        context_window=None, system_prompt_file=str(prompt))

    async def exercise():
        try:
            session = ADAPTERS[kind].resident_session("manager")
            for _ in range(2):
                with pytest.raises(RunnerUnavailable) as caught:
                    await session.start(descriptor.to_argv(), cwd=str(tmp_path), signature=None,
                                        env={KEY_ENV: "synthetic-key"})
                assert f"{package} version {version} is unsupported" in str(caught.value)
                assert f'[runtimes] {kind} = "cli"' in str(caught.value)
        finally:
            await supervisor.stop()

    asyncio.run(asyncio.wait_for(exercise(), 20))
