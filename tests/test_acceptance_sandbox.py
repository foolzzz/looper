"""The local launcher must prove enforcement before it can run a check."""
from __future__ import annotations

import asyncio
import errno
import json
import shlex
import shutil
import sys
from unittest.mock import AsyncMock

import pytest

from looper import child_env
from looper.agents import acceptance_sandbox as sandbox, proc_tree


def test_missing_or_unsupported_facility_never_starts_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda *args, **kwargs: None)
    spawn = AsyncMock(side_effect=AssertionError("must not execute without a sandbox"))
    monkeypatch.setattr(proc_tree, "spawn", spawn)
    assert asyncio.run(sandbox.local_prefix(tmp_path, child_env.inherited())) is None
    spawn.assert_not_awaited()


@pytest.mark.parametrize("reply", [
    (71, b""),
    (0, b"not-json"),
    (0, b'{"bind_errno": 0, "connect_errno": 1}'),
    (0, b'{"bind_errno": 1, "connect_errno": 0}'),
    (0, b'{"bind_errno": 1, "connect_errno": 60}'),
    None,
])
def test_preflight_rejects_errors_or_a_non_enforcing_launcher(tmp_path, monkeypatch, reply):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda *args, **kwargs: "/usr/bin/sandbox-exec")
    run = AsyncMock(side_effect=[(0, b""), reply])
    monkeypatch.setattr(sandbox, "_run_probe", run)
    assert asyncio.run(sandbox.local_prefix(tmp_path, {})) is None
    assert run.await_args_list[0].args[0] == (
        "/usr/bin/sandbox-exec", "-p",
        "(version 1)(allow default)(deny network*)(deny job-creation)",
        "/bin/sh", "-c", "true",
    )


def test_profile_rejected_before_network_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda *args, **kwargs: "/usr/bin/sandbox-exec")
    run = AsyncMock(return_value=(65, b""))
    monkeypatch.setattr(sandbox, "_run_probe", run)
    assert asyncio.run(sandbox.local_prefix(tmp_path, {})) is None
    assert run.await_count == 1


@pytest.mark.parametrize("namespace", ["net:[old]", "net:[new]"])
def test_linux_requires_a_new_network_namespace(tmp_path, monkeypatch, namespace):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda *args, **kwargs: "/usr/bin/unshare")
    monkeypatch.setattr(sandbox.os, "readlink", lambda path: "net:[old]")
    run = AsyncMock(side_effect=[(0, b""), (0, json.dumps({
        "namespace": namespace, "interfaces": ["lo"], "connect_errno": errno.ENETUNREACH,
    }).encode())])
    monkeypatch.setattr(sandbox, "_run_probe", run)
    result = asyncio.run(sandbox.local_prefix(tmp_path, {}))
    assert result == (("/usr/bin/unshare", "-rn", "--") if namespace != "net:[old]" else None)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec enforcement")
def test_passthrough_launcher_is_rejected_without_an_external_connect(tmp_path, monkeypatch):
    executable = tmp_path / "sandbox-exec"
    executable.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
    executable.chmod(0o755)
    monkeypatch.setattr(sandbox.shutil, "which", lambda *args, **kwargs: str(executable))
    # A passthrough fails the loopback-bind gate before attempting the external probe.
    assert asyncio.run(sandbox.local_prefix(tmp_path, child_env.inherited())) is None


@pytest.mark.installed
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec enforcement")
def test_real_macos_profile_blocks_network_and_descendants_but_allows_file_io(tmp_path):
    assert shutil.which("sandbox-exec"), "sandbox-exec is required on the macOS validation host"
    code = '''
import errno, pathlib, socket, subprocess, sys
def rejected(call):
    try:
        call()
    except OSError as exc:
        assert exc.errno == errno.EPERM, repr(exc)
    else:
        raise AssertionError("network access was not rejected")
rejected(lambda: socket.create_connection(("1.1.1.1", 53), 1))
rejected(lambda: socket.create_connection(("127.0.0.1", 9), 1))
rejected(lambda: socket.socket().bind(("127.0.0.1", 0)))
try:
    socket.getaddrinfo("example.com", 443)
except socket.gaierror:
    pass
else:
    raise AssertionError("DNS was not rejected")
pathlib.Path("file-io.txt").write_text("ok")
assert subprocess.check_output(["/bin/sh", "-c", "printf pipe-ok | /bin/cat"]) == b"pipe-ok"
'''
    child = '''
import errno, socket
try:
    socket.create_connection(("1.1.1.1", 53), 1)
except OSError as exc:
    assert exc.errno == errno.EPERM, repr(exc)
else:
    raise AssertionError("grandchild network access was not rejected")
'''
    code += "\nsubprocess.run([sys.executable, '-I', '-S', '-c', " + repr(child) + "], check=True)\n"
    command = shlex.join([sys.executable, "-I", "-S", "-c", code])

    async def run():
        env = child_env.inherited()
        prefix = await sandbox.local_prefix(tmp_path, env)
        assert prefix is not None
        process = await proc_tree.spawn(*prefix, "/bin/sh", "-c", command, cwd=tmp_path, env=env,
                                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.STDOUT)
        tree = proc_tree.track(process, label="sandbox integration probe")
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 2)
            assert process.returncode == 0, output.decode()
        finally:
            await tree.end()
    asyncio.run(run())
    assert (tmp_path / "file-io.txt").read_text() == "ok"
