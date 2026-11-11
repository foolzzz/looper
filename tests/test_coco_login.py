"""Coco's linked login and read-only probes, using only stand-in executables."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_claude import fake_claude
from looper.agents.runtimes import coco_login


@pytest.mark.parametrize(("returncode", "output", "state", "detail"), [
    (0, "Logged in using Trae\n", "logged_in", "logged in"),
    (1, "Not logged in\n", "not_logged_in", "not logged in"),
    (2, "\n  unexpected response\nsecond line\n", "unknown", "unexpected response"),
])
def test_status_uses_exit_code_and_output(returncode, output, state, detail):
    status = coco_login.parse_status(returncode, output)
    assert status.state.value == state and detail in status.detail
    assert "second line" not in status.detail


@pytest.mark.parametrize("setting", ["absolute", "tilde", "unset"])
def test_login_file_respects_the_server_home(tmp_path, monkeypatch, setting):
    monkeypatch.setenv("HOME", str(tmp_path))
    environ = {"TRAE_HOME": f"  {tmp_path / 'configured'}  "} if setting == "absolute" else (
        {"TRAE_HOME": " ~/configured "} if setting == "tilde" else {})
    directory = tmp_path / (".trae" if setting == "unset" else "configured")
    assert coco_login.login_file(environ) == directory / "cli" / "auth.json"


def test_link_login_creates_private_cli_directory(tmp_path):
    home, target = tmp_path / "run", tmp_path / "stand-in-auth.json"
    coco_login.link_login(home, target)
    assert stat.S_IMODE((home / "cli").stat().st_mode) == 0o700
    assert (home / "cli" / "auth.json").is_symlink()
    assert os.readlink(home / "cli" / "auth.json") == str(target)


@pytest.mark.parametrize("entry", ["copy", "missing-cli"], ids=["forked-file", "removed-state-directory"])
def test_inspection_replaces_a_forked_login_once(tmp_path, entry):
    directory, target = tmp_path / "coco", tmp_path / "stand-in-auth.json"
    target.write_text("stand-in login")
    home = directory / "runs" / "example"
    (home / "cli").mkdir(parents=True)
    (home / "traecli.toml").write_text(coco_login.CONFIG_MARKER + "\n")
    if entry == "copy":
        (home / "cli" / "auth.json").write_text("forked login")
    else:
        (home / "cli").rmdir()
    problems = coco_login.inspect_homes(directory, target)
    assert len(problems) == 1
    assert (home / "cli" / "auth.json").is_symlink()
    assert os.readlink(home / "cli" / "auth.json") == str(target)
    assert target.read_text() == "stand-in login"
    assert coco_login.inspect_homes(directory, target) == []


def test_probe_uses_only_a_link_and_removes_the_probe_home(tmp_path, monkeypatch):
    target = tmp_path / "stand-in-auth.json"
    target.write_text("stand-in login")
    for name in coco_login.ENV_UNSET:
        monkeypatch.setenv(name, "would-override-the-login")
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "unrelated-home"))
    recorded = tmp_path / "probe-home"
    executable = fake_claude(tmp_path / "coco", (
        "import os, sys\n"
        "from pathlib import Path\n"
        "home = Path(os.environ['TRAE_HOME'])\n"
        "assert sys.argv[1:] == ['login', 'status'], sys.argv\n"
        "assert sorted(p.name for p in home.iterdir()) == ['cli']\n"
        "assert sorted(p.name for p in (home / 'cli').iterdir()) == ['auth.json']\n"
        f"assert os.readlink(home / 'cli' / 'auth.json') == {str(target)!r}\n"
        f"assert all(name not in os.environ for name in {coco_login.ENV_UNSET!r})\n"
        f"Path({str(recorded)!r}).write_text(str(home))\n"
        "print('Logged in using Trae')\n"
    ))
    status = coco_login.probe_login(executable, target)
    assert status.state is coco_login.LoginState.LOGGED_IN
    assert not Path(recorded.read_text()).exists()
    assert target.read_text() == "stand-in login"


@pytest.mark.parametrize("failure", [False, True], ids=["catalog", "failed-command"])
def test_model_catalog_maps_display_and_config_names(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "operator-home"))
    recorded = tmp_path / "probe-home"
    executable = fake_claude(tmp_path / "coco", (
        "import os, sys\n"
        "from pathlib import Path\n"
        "home = Path(os.environ['TRAE_HOME'])\n"
        "assert sys.argv[1:] == ['models', '--json'], sys.argv\n"
        "assert (home / 'cli' / 'auth.json').is_symlink()\n"
        f"Path({str(recorded)!r}).write_text(str(home))\n"
        'print(\'[{"name": "GPT-5.5", "config_name": "gpt-5.5-config"}, {"name": "GPT-6.1-Sol"}]\')\n'
        f"sys.exit({int(failure)})\n"
    ))
    coco_login.forget_models()
    try:
        models = coco_login.available_models(executable)
        assert models == (None if failure else {
            "gpt-5.5": "GPT-5.5", "gpt-5.5-config": "GPT-5.5", "gpt-6.1-sol": "GPT-6.1-Sol",
        })
        assert not Path(recorded.read_text()).exists()
    finally:
        coco_login.forget_models()


def test_model_catalog_cache_is_per_executable(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "operator-home"))
    first = fake_claude(tmp_path / "first-coco", "print('[{\"name\": \"GPT-5.5\"}]')\n")
    second = fake_claude(tmp_path / "second-coco", "print('[{\"name\": \"GPT-6.1-Sol\"}]')\n")
    coco_login.forget_models()
    try:
        assert coco_login.available_models(first) == {"gpt-5.5": "GPT-5.5"}
        fake_claude(tmp_path / "first-coco", "raise SystemExit(1)\n")
        assert coco_login.available_models(first) == {"gpt-5.5": "GPT-5.5"}
        assert coco_login.available_models(second) == {"gpt-6.1-sol": "GPT-6.1-Sol"}
        coco_login.forget_models()
        assert coco_login.available_models(first) is None
    finally:
        coco_login.forget_models()


def test_cached_models_expires_without_running_a_process(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "operator-home"))
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(coco_login, "time", SimpleNamespace(monotonic=lambda: clock.now))
    executable = fake_claude(tmp_path / "coco", "print('[{\"name\": \"GPT-5.5\"}]')\n")
    coco_login.forget_models()
    try:
        assert coco_login.cached_models(executable) is None
        models = coco_login.available_models(executable)
        assert models == {"gpt-5.5": "GPT-5.5"}
        ran = tmp_path / "models-ran"
        fake_claude(tmp_path / "coco", f"from pathlib import Path\nPath({str(ran)!r}).touch()\n")
        clock.now = coco_login._MODELS_TTL_SEC
        assert coco_login.cached_models(executable) == models
        assert coco_login.cached_models("another-coco") is None
        clock.now += 0.01
        assert coco_login.cached_models(executable) is None
        assert not ran.exists()
    finally:
        coco_login.forget_models()


def test_login_probe_cache_is_per_executable_and_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAE_HOME", str(tmp_path / "operator-home"))
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(coco_login, "time", SimpleNamespace(monotonic=lambda: clock.now))
    first = fake_claude(tmp_path / "first-coco", "print('Logged in using Trae')\n")
    second = fake_claude(tmp_path / "second-coco", "print('Not logged in')\nraise SystemExit(1)\n")
    coco_login.forget_probe()
    try:
        assert coco_login.cached_probe(first).state is coco_login.LoginState.LOGGED_IN
        assert coco_login.cached_probe(second).state is coco_login.LoginState.NOT_LOGGED_IN
        fake_claude(tmp_path / "first-coco", "print('Not logged in')\nraise SystemExit(1)\n")
        clock.now = coco_login._PROBE_TTL_SEC
        assert coco_login.cached_probe(first).state is coco_login.LoginState.LOGGED_IN
        clock.now += 0.01
        assert coco_login.cached_probe(first).state is coco_login.LoginState.NOT_LOGGED_IN
        fake_claude(tmp_path / "first-coco", "print('Logged in using Trae')\n")
        fake_claude(tmp_path / "second-coco", "print('Logged in using Trae')\n")
        coco_login.forget_probe()
        assert coco_login.cached_probe(first).state is coco_login.LoginState.LOGGED_IN
        assert coco_login.cached_probe(second).state is coco_login.LoginState.LOGGED_IN
    finally:
        coco_login.forget_probe()
