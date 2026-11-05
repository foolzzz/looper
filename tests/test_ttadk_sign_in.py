"""TTADK's Cursor sign-in probe uses the same executable boundary as a run."""

import asyncio
import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from fake_claude import fake_claude
from looper.agents.runtimes import cursor_login, ttadk


@pytest.fixture(autouse=True)
def isolated_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    ttadk.forget_probe()
    yield
    ttadk.forget_probe()


@pytest.mark.parametrize("times_out", [False, True], ids=["empty-models", "timeout"])
def test_cursor_sign_in_resolves_agent_through_shim_and_cleans_directories(tmp_path, monkeypatch, times_out):
    capture = tmp_path / "cursor.json"
    wrong = tmp_path / "wrong-agent"
    wrong.mkdir()
    marker = tmp_path / "wrong-ran"
    fake_claude(wrong / "agent", f"import pathlib, time\npathlib.Path({str(marker)!r}).touch()\n"
                                "print('ready', flush=True)\ntime.sleep(15)\n")
    cursor = tmp_path / "cursor-agent"
    cursor.write_text(f"#!{sys.executable}\n" + f"""
import json, os, pathlib
env = dict(os.environ)
assert not set({cursor_login.ENV_UNSET!r}) & env.keys()
assert 'TTADK_JWT_TOKEN' not in env
for name in {cursor_login.DIRECTORY_VARS!r}:
    assert pathlib.Path(env[name]).is_dir()
pathlib.Path({str(capture)!r}).write_text(json.dumps(env))
print('{{"models": []}}')
""", encoding="utf-8")
    cursor.chmod(0o700)
    executable = fake_claude(tmp_path / "ttadk", """
import os, sys
assert sys.argv[1:] == ['models', '-t', 'cursor-cli', '-f', 'json']
os.environ['TTADK_JWT_TOKEN'] = 'gateway-token'
os.execvp('agent', ['agent', '--list-models'])
""")
    base = {**os.environ, "PATH": os.pathsep.join([str(wrong), str(tmp_path), os.environ["PATH"]]),
            **dict.fromkeys(cursor_login.ENV_UNSET, "inherited-secret"),
            **dict.fromkeys(cursor_login.DIRECTORY_VARS, str(tmp_path / "inherited-dir"))}
    monkeypatch.setattr(ttadk.child_env, "inherited", lambda: dict(base))
    # Positive control: the inherited PATH really resolves to the slow, unrelated agent.
    with subprocess.Popen([executable, "models", "-t", "cursor-cli", "-f", "json"],
                          env=base, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            # The startup guard is separate from the timeout being tested.
            assert select.select([process.stdout], [], [], 10)[0], "wrong agent did not start"
            assert process.stdout.readline() == b"ready\n"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.1)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
    assert marker.is_file()
    marker.unlink()

    if times_out:
        run = subprocess.run

        def timeout_after_capture(argv, **kwargs):
            # Keep the real TTADK -> shim -> Cursor path and its environment
            # assertions; inject the timeout only after Cursor has recorded them.
            done = run(argv, **kwargs)
            assert done.returncode == 0, done.stderr
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(ttadk.subprocess, "run", timeout_after_capture)

    problem = asyncio.run(ttadk.sign_in_problem(executable, "cursor-cli"))
    assert problem is (ttadk.TtadkProblem.CHECK_FAILED if times_out else None)
    assert not marker.exists()
    env = json.loads(capture.read_text())
    shim = Path(env["PATH"].split(os.pathsep)[0])
    assert shim.parts[-2:] == ("cursor-agent", "ttadk-bin")
    assert env["PATH"].split(os.pathsep)[1:] == base["PATH"].split(os.pathsep)
    assert env["HOME"] == base["HOME"]
    assert env["CURSOR_CONFIG_DIR"] != env["CURSOR_DATA_DIR"]
    assert not shim.exists()
    assert all(not Path(env[name]).exists() for name in cursor_login.DIRECTORY_VARS)


@pytest.mark.parametrize(("tool", "timeout"), [("codex", 20.0), ("cursor-cli", 45.0)])
def test_sign_in_timeout_budget_and_codex_environment_are_preserved(tool, timeout, tmp_path, monkeypatch):
    executable = fake_claude(tmp_path / "ttadk", "")
    cursor = tmp_path / "cursor-agent"
    cursor.write_text("#!/bin/sh\nexit 0\n")
    cursor.chmod(0o700)
    base = {"PATH": str(tmp_path), "HOME": str(tmp_path / "home"), "CODEX_HOME": "unchanged"}
    monkeypatch.setattr(ttadk.child_env, "inherited", lambda: dict(base))
    environments = []

    def run(argv, **kwargs):
        assert argv == [executable, "models", "-t", tool, "-f", "json"]
        assert kwargs["timeout"] == timeout
        assert kwargs["stdin"] == subprocess.DEVNULL
        environments.append(kwargs["env"])
        if tool == "codex":
            assert kwargs["env"] == base
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(ttadk.subprocess, "run", run)
    assert ttadk._sign_in_problem(executable, tool) is ttadk.TtadkProblem.CHECK_FAILED
    if tool == "cursor-cli":
        assert all(not Path(environments[0][name]).exists() for name in cursor_login.DIRECTORY_VARS)
