"""The ``denyRead`` probe of ``--credential operator-login``: a scripted local model drives the CLI through one
read-only command per denied path. Unit tests only, except the opt-in run against the real CLI at the end; every
"operator home" here is a temporary directory and no model is called."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from evals.prompt_regression import task_manager_deny_probe as probe
from looper.agents import task_manager_sandbox

KEY = probe.PROBE_KEY
SENTINEL = "SENTINEL-BODY-9f31"


def post(url, headers, body=None):
    body = body if body is not None else {"model": "m", "stream": True, "tools": [{"name": "Bash"}],
                                          "messages": [{"role": "user", "content": SENTINEL}]}
    try:
        with urlopen(Request(url + "/v1/messages", data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json", **headers}), timeout=10) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, ""


def script(*commands):
    return [(f"toolu_{index}", command) for index, command in enumerate(commands)]


def results(blocks):
    return {"model": "m", "stream": True, "tools": [{"name": "Bash"}], "messages": [{"role": "user", "content": blocks}]}


def result(ident, content, is_error=False):
    return {"type": "tool_result", "tool_use_id": ident, "content": content, "is_error": is_error}


def tool_id(body):
    for line in body.splitlines():
        if line.startswith("data: "):
            event = json.loads(line[6:])
            if event["type"] == "content_block_start":
                return event["content_block"].get("id")
    return None


# --- The fake server --------------------------------------------------------------------------------------------


def test_the_server_hands_out_one_command_at_a_time_and_keeps_markers_only():
    server = probe.MarkerServer(script("true", "false"))
    url = server.start()
    try:
        status, first = post(url, {"x-api-key": KEY})
        assert status == 200 and tool_id(first) == "toolu_0"
        _, second = post(url, {"x-api-key": KEY}, results([result("toolu_0", "DENIED")]))
        assert tool_id(second) == "toolu_1"
        _, third = post(url, {"x-api-key": KEY}, results([
            result("toolu_0", "DENIED"), result("toolu_1", [{"type": "text", "text": "ALLOWED\n"}])]))
        assert tool_id(third) is None
    finally:
        server.stop()
    assert server.marks == {"toolu_0": probe.Mark.DENIED, "toolu_1": probe.Mark.ALLOWED}
    assert server.requests == 3 and server.refusal is None
    assert SENTINEL not in repr(vars(server))


@pytest.mark.parametrize("content, is_error, mark", [
    ("DENIED", False, "DENIED"),
    ("OTHER", False, "OTHER"),
    ("DENIED extra file-name", False, "OTHER"),
    ("", False, "OTHER"),
    ("DENIED", True, "OTHER"),
    ("PreToolUse:Bash hook error: no", True, "BLOCKED_BY_HOOK"),
    ("PreToolUse:Bash hook error: [exit 2]: no\n", True, "BLOCKED_BY_HOOK"),
])
def test_a_result_is_read_as_a_marker_only(content, is_error, mark):
    server = probe.MarkerServer(script("true"))
    url = server.start()
    try:
        post(url, {"x-api-key": KEY}, results([result("toolu_0", content, is_error)]))
    finally:
        server.stop()
    assert server.marks == {"toolu_0": probe.Mark(mark)}


@pytest.mark.parametrize("headers, refusal", [
    ({"x-api-key": KEY, "Authorization": "Bearer sk-ant-oat-SECRET"}, probe.Refusal.AUTHORIZATION),
    ({"Authorization": "Bearer sk-ant-oat-SECRET"}, probe.Refusal.AUTHORIZATION),
    ({"x-api-key": "sk-ant-api-SECRET"}, probe.Refusal.WRONG_KEY),
    ({}, probe.Refusal.WRONG_KEY),
])
def test_a_foreign_credential_stops_the_server_and_nothing_of_the_request_is_kept(headers, refusal):
    server = probe.MarkerServer(script("true"))
    url = server.start()
    try:
        assert post(url, headers)[0] == 401
        assert server.refused.is_set()
    finally:
        server.stop()
    assert server.refusal is refusal and server.marks == {} and server.requests == 0
    kept = repr(vars(server))
    assert "SECRET" not in kept and SENTINEL not in kept


METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "BREW"]


def send(url, method, headers, path="/"):
    try:
        with urlopen(Request(url + path, headers=headers, method=method), timeout=10) as response:
            return response.status
    except HTTPError as error:
        return error.code


@pytest.mark.parametrize("path", ["/", "/v1/messages"])
@pytest.mark.parametrize("headers, refusal", [
    ({"Authorization": "Bearer x"}, probe.Refusal.AUTHORIZATION),
    ({"x-api-key": "sk-ant-api-SECRET"}, probe.Refusal.WRONG_KEY),
])
@pytest.mark.parametrize("method", METHODS)
def test_a_foreign_credential_on_any_method_is_refused(method, headers, refusal, path):
    server = probe.MarkerServer(script("true"))
    url = server.start()
    try:
        assert send(url, method, headers, path) == 401
        assert server.refused.is_set()
    finally:
        server.stop()
    assert server.refusal is refusal and server.requests == 0


@pytest.mark.parametrize("headers", [{}, {"x-api-key": KEY}])
def test_a_request_without_a_foreign_credential_is_not_refused(headers):
    server = probe.MarkerServer(script("true"))
    url = server.start()
    try:
        assert send(url, "HEAD", headers) == 404
        assert send(url, "GET", headers) == 404
    finally:
        server.stop()
    assert server.refusal is None


# --- The commands -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_commands_print_one_marker_and_no_name_or_content(tmp_path, kind):
    base = tmp_path / "a dir with spaces $(x) 'q'"
    base.mkdir()
    (base / "secret-name").write_text("secret-content")
    target = base if kind == "directory" else base / "secret-name"

    def shell(command):
        return subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=30)

    done = shell(probe.command(target, probe.Kind(kind)))
    assert done.stdout == "ALLOWED\n" and done.stderr == ""
    missing = shell(probe.command(tmp_path / "gone", probe.Kind(kind)))
    assert missing.stdout == "OTHER\n" and missing.stderr == ""
    assert (base / "secret-name").read_text() == "secret-content"


def test_the_file_command_copies_no_byte_of_the_file(tmp_path):
    """The real ``dd``, with its output redirected into a file this test can read: it must stay empty."""
    stub, copied, target = tmp_path / "bin", tmp_path / "copied", tmp_path / "target"
    stub.mkdir()
    target.write_text("secret-content")
    (stub / "dd").write_text(
        '#!/bin/bash\nargs=()\nfor a in "$@"; do [[ $a == of=* ]] && a="of=$COPIED"; args+=("$a"); done\n'
        'exec /bin/dd "${args[@]}"\n')
    (stub / "dd").chmod(0o755)
    done = subprocess.run(["/bin/bash", "-c", probe.command(target, probe.Kind.FILE)], capture_output=True, text=True,
                          timeout=30, env={"PATH": f"{stub}:/usr/bin:/bin", "COPIED": str(copied)})
    assert done.stdout == "ALLOWED\n" and copied.read_bytes() == b""


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_commands_read_a_sandbox_denial_as_denied(tmp_path, kind):
    """The marker follows the words the sandbox's refusal carries, with a stand-in for ls and dd that says them."""
    stub = tmp_path / "bin"
    stub.mkdir()
    for name in ("ls", "dd"):
        (stub / name).write_text(f"#!/bin/sh\necho '{name}: x: Operation not permitted' >&2\nexit 1\n")
        (stub / name).chmod(0o755)
    command = probe.command(tmp_path, probe.Kind(kind))
    done = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{stub}:/usr/bin:/bin"})
    assert done.stdout == "DENIED\n"


# --- The verdict ------------------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = (tmp_path / "operator-home").resolve()
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id").write_text("k")
    (home / ".netrc").write_text("n")
    monkeypatch.setenv("HOME", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "CODEX_HOME", "GROK_HOME", "TRAE_HOME", "COPILOT_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(task_manager_sandbox, "SUPPORTED_PLATFORM", sys.platform)
    return home


class Cli:
    """Stands in for the CLI: answers each command the server hands out with ``answer(entry command)``."""

    def __init__(self, answer, *, headers=None, touch=None, denials=(), extra_requests=0):
        self.answer, self.headers, self.touch, self.denials = answer, headers, touch, list(denials)
        self.extra_requests = extra_requests
        self.seen = {}

    def __call__(self, command, *, cwd, env, server):
        self.seen = {"command": command, "cwd": cwd, "env": env}
        url = env["ANTHROPIC_BASE_URL"]
        headers = self.headers or {"x-api-key": env["ANTHROPIC_API_KEY"]}
        blocks = []
        while not server.refused.is_set():
            status, body = post(url, headers, results(blocks) if blocks else None)
            if status != 200 or (ident := tool_id(body)) is None:
                break
            used = next(json.loads(e["delta"]["partial_json"])["command"] for e in
                        (json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: "))
                        if e["type"] == "content_block_delta")
            content, is_error = self.answer(used)
            blocks.append(result(ident, content, is_error))
        for _ in range(self.extra_requests):
            post(url, headers, results(blocks))
        if self.touch:
            self.touch()
        return probe.CliRun(0, frozenset(self.denials))


def sandbox(used):
    """A boundary that holds: the positive controls are readable, everything else is denied."""
    return ("ALLOWED" if "probe-allowed" in used else "DENIED"), False


def run(tmp_path, cli):
    root = tmp_path / "probe-root"
    root.mkdir(exist_ok=True)
    return probe.run_probe("/nonexistent/claude", root=root, drive=cli)


def test_a_boundary_that_holds_passes_and_lists_what_this_machine_does_not_have(home, tmp_path):
    cli = Cli(sandbox)
    report = run(tmp_path, cli)
    assert report["status"] == "PASS", report
    assert "~/.aws" in report["absent"] and "~/.ssh" not in report["absent"]
    # ~/.ssh, ~/.netrc and the runtime root the probe made are present.
    assert report["verified_denied"] == 3
    assert report["summary"] == (f"3 verified denied, {len(report['absent'])} absent on this machine (not verified)")
    assert report["failed"] == [] and report["alarms"] == [] and report["changed_during_probe"] == []
    assert report["reason"] is None and report["unverifiable"] == []
    assert report["controls"] == {"allowed_directory": "ALLOWED", "allowed_file": "ALLOWED",
                                  "denied_directory": "DENIED", "denied_file": "DENIED"}
    assert str(home) not in json.dumps(report) and str(tmp_path) not in json.dumps(report)
    env = cli.seen["env"]
    assert env["ANTHROPIC_API_KEY"] == KEY and env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert "ANTHROPIC_AUTH_TOKEN" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env and env["HOME"] == str(home)


def test_the_command_is_the_one_the_live_session_is_launched_with(home, tmp_path):
    cli = Cli(sandbox)
    run(tmp_path, cli)
    command = cli.seen["command"]
    assert "--dangerously-skip-permissions" in command and "--strict-mcp-config" in command
    settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
    live = task_manager_sandbox.read_deny_paths(cli.seen["env"], tmp_path / "probe-root" / "runtime")
    denied = settings["sandbox"]["filesystem"]["denyRead"]
    assert denied[:len(live)] == list(live) and len(denied) == len(live) + 2  # the two negative controls
    assert settings == task_manager_sandbox.settings_content(Path(cli.seen["cwd"]), read_deny=denied)


def test_an_absent_entry_is_neither_run_nor_counted(home, tmp_path):
    used = []

    def answer(command):
        used.append(command)
        return sandbox(command)

    report = run(tmp_path, Cli(answer))
    assert not any(".aws" in command for command in used)
    assert "~/.aws" in report["absent"] and report["verified_denied"] == 3 and report["status"] == "PASS"


@pytest.mark.parametrize("mark", ["ALLOWED", "OTHER"])
def test_an_entry_that_is_not_denied_fails_and_is_named_by_index_and_home_path(home, tmp_path, mark):
    report = run(tmp_path, Cli(lambda c: (mark, False) if "/.netrc" in c else sandbox(c)))
    assert report["status"] == "FAIL"
    [failed] = report["failed"]
    assert failed["path"] == "~/.netrc" and failed["mark"] == mark and isinstance(failed["index"], int)


@pytest.mark.parametrize("control, mark", [("probe-allowed", "DENIED"), ("probe-denied", "ALLOWED")])
def test_a_control_that_answers_wrongly_fails(home, tmp_path, control, mark):
    report = run(tmp_path, Cli(lambda c: (mark, False) if control in c else sandbox(c)))
    assert report["status"] == "FAIL" and report["failed"] == []


@pytest.mark.parametrize("how", ["text", "denial"])
def test_a_command_a_hook_blocked_invalidates_the_whole_probe(home, tmp_path, how):
    def answer(command):
        if "/.netrc" not in command:
            return sandbox(command)
        return ("PreToolUse:Bash hook error: no", True) if how == "text" else ("a hook's own words", True)

    cli = Cli(answer)
    if how == "denial":
        def drive(command, *, cwd, env, server):
            done = cli(command, cwd=cwd, env=env, server=server)
            return probe.CliRun(done.exit_code, frozenset(server.marks))
        report = run(tmp_path, drive)
    else:
        report = run(tmp_path, cli)
    assert report["status"] == "INVALID" and report["markers"]["BLOCKED_BY_HOOK"] >= 1


def test_a_bearer_aborts_and_only_the_fact_is_kept(home, tmp_path):
    cli = Cli(sandbox, headers={"x-api-key": KEY, "Authorization": "Bearer sk-ant-oat-SECRET"})
    report = run(tmp_path, cli)
    assert report == {"status": "ABORTED", "reason": "an Authorization header reached the probe server"}


def test_no_request_at_all_is_not_a_pass(home, tmp_path):
    report = run(tmp_path, lambda command, **kwargs: probe.CliRun(1, frozenset()))
    assert report["status"] == "ABORTED" and "no request" in report["reason"]


def test_an_unexpected_request_count_invalidates(home, tmp_path):
    report = run(tmp_path, Cli(sandbox, extra_requests=2))
    assert report["status"] == "INVALID" and "requests" in report["reason"]


@pytest.mark.parametrize("change", ["count", "mtime"])
def test_a_changed_denied_directory_raises_the_alarm(home, tmp_path, change):
    def touch():
        if change == "count":
            (home / ".ssh" / "planted").write_text("x")
        if change == "mtime":
            os.utime(home / ".ssh", ns=(1, 1))

    report = run(tmp_path, Cli(sandbox, touch=touch))
    assert report["status"] == "ALARM"
    [alarm] = report["alarms"]
    assert alarm["path"] == "~/.ssh" and change in alarm["changed"]
    assert report["changed_during_probe"] == []


def change_directory(path, change):
    if change == "count":
        (path / "written-by-a-session").write_text("x")
    elif change == "mtime":
        os.utime(path, ns=(1, 1))
    else:
        path.rmdir()


@pytest.mark.parametrize("change", ["count", "mtime"])
@pytest.mark.parametrize("name", [".claude", ".codex", ".grok", ".trae", ".copilot"])
def test_a_changed_agent_state_directory_is_listed_and_still_passes(home, tmp_path, name, change):
    (home / name).mkdir()
    report = run(tmp_path, Cli(sandbox, touch=lambda: change_directory(home / name, change)))
    assert report["status"] == "PASS" and report["alarms"] == [], report
    [seen] = report["changed_during_probe"]
    assert seen["path"] == f"~/{name}" and change in seen["changed"]


def test_an_agent_state_change_does_not_hide_a_credential_change(home, tmp_path):
    (home / ".claude").mkdir()

    def touch():
        change_directory(home / ".claude", "count")
        change_directory(home / ".ssh", "count")

    report = run(tmp_path, Cli(sandbox, touch=touch))
    assert report["status"] == "ALARM"
    assert [item["path"] for item in report["alarms"]] == ["~/.ssh"]
    assert [item["path"] for item in report["changed_during_probe"]] == ["~/.claude"]


def test_an_agent_state_directory_that_disappears_still_raises_the_alarm(home, tmp_path):
    (home / ".grok").mkdir()
    report = run(tmp_path, Cli(sandbox, touch=lambda: change_directory(home / ".grok", "gone")))
    assert report["status"] == "ALARM" and report["changed_during_probe"] == []
    assert report["alarms"][0]["changed"] == ["gone or unreadable"]


@pytest.mark.parametrize("name", [".claude", ".grok"])
def test_an_agent_state_entry_that_is_not_denied_still_fails(home, tmp_path, name):
    (home / name).mkdir()
    report = run(tmp_path, Cli(lambda c: ("ALLOWED", False) if c.count(f"/{name} ") else sandbox(c),
                               touch=lambda: change_directory(home / name, "count")))
    assert report["status"] == "FAIL" and [item["path"] for item in report["failed"]] == [f"~/{name}"]


# --- The classes ------------------------------------------------------------------------------------------------


def every_variable(tmp_path):
    names = ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "CODEX_HOME", "GROK_HOME", "TRAE_HOME", "COPILOT_HOME")
    return {"HOME": str(tmp_path / "h"), **{name: str(tmp_path / name.lower()) for name in names}}


def test_every_deny_entry_declares_its_class(tmp_path):
    entries = task_manager_sandbox.read_deny_entries(every_variable(tmp_path), tmp_path / "runtime", tmp_path / "ops")
    assert [str(e.path) for e in entries if not isinstance(e.deny_class, task_manager_sandbox.DenyClass)] == []


def test_the_agent_state_entries_are_exactly_the_agent_clis_own(tmp_path):
    env = every_variable(tmp_path)
    classes = task_manager_sandbox.read_deny_classes(env, tmp_path / "runtime", tmp_path / "ops")
    assert tuple(classes) == task_manager_sandbox.read_deny_paths(env, tmp_path / "runtime", tmp_path / "ops")
    home = tmp_path / "h"
    expected = [home / name for name in (".claude", ".claude.json", ".codex", ".grok", ".trae", ".copilot")]
    expected += [Path(env[name]) for name in
                 ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", "TRAE_HOME", "COPILOT_HOME")]
    state = {path for path, found in classes.items() if found is task_manager_sandbox.DenyClass.AGENT_STATE}
    assert state == {os.path.realpath(path) for path in expected}


def test_a_path_declared_under_both_classes_is_a_credential(tmp_path):
    home = tmp_path / "h"
    env = {"HOME": str(home), "CODEX_HOME": str(home / ".ssh"), "CLAUDE_CONFIG_DIR": str(tmp_path / "runtime")}
    classes = task_manager_sandbox.read_deny_classes(env, tmp_path / "runtime")
    for path in (home / ".ssh", tmp_path / "runtime"):  # declared credential first, and declared credential last
        assert classes[os.path.realpath(path)] is task_manager_sandbox.DenyClass.CREDENTIAL


def test_an_unclassified_entry_is_watched_as_a_credential(home, tmp_path, monkeypatch):
    declared = task_manager_sandbox.read_deny_entries
    (home / "new-store").mkdir()
    monkeypatch.setattr(task_manager_sandbox, "read_deny_entries", lambda *args: (
        *declared(*args), task_manager_sandbox.DenyEntry(home / "new-store")))
    report = run(tmp_path, Cli(sandbox, touch=lambda: change_directory(home / "new-store", "count")))
    assert report["status"] == "ALARM" and [item["path"] for item in report["alarms"]] == ["~/new-store"]


def test_a_directory_unreadable_outside_the_sandbox_is_not_counted_as_verified(home, tmp_path):
    (home / ".aws").mkdir(mode=0o000)
    try:
        report = run(tmp_path, Cli(sandbox))
    finally:
        (home / ".aws").chmod(0o700)
    assert report["status"] == "PASS" and report["verified_denied"] == 3
    [item] = report["unverifiable"]
    assert item["path"] == "~/.aws" and item["reason"] == "it cannot be listed outside the sandbox"
    assert "~/.aws (it cannot be listed outside the sandbox)" in report["reason"]


def test_a_file_unreadable_outside_the_sandbox_is_named_with_its_reason(home, tmp_path):
    (home / ".netrc").chmod(0o000)
    used = []

    def answer(command):
        used.append(command)
        return sandbox(command)

    report = run(tmp_path, Cli(answer))
    assert not any("/.netrc" in command for command in used)
    assert [(item["path"], item["reason"]) for item in report["unverifiable"]] == [
        ("~/.netrc", "it has no read permission outside the sandbox")]
    assert report["status"] == "PASS" and "~/.netrc" in report["reason"] and report["verified_denied"] == 2


def test_an_entry_whose_metadata_cannot_be_read_is_named_with_its_reason(home, tmp_path):
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config").chmod(0o000)
    try:
        report = run(tmp_path, Cli(sandbox))
    finally:
        (home / ".config").chmod(0o700)
    reasons = {item["path"]: item["reason"] for item in report["unverifiable"]}
    assert reasons["~/.config/gh"] == "its metadata cannot be read outside the sandbox"


def test_the_standalone_command_runs_the_probe_and_exits_by_its_status(home, tmp_path, monkeypatch, capsys):
    from evals.prompt_regression import task_manager_eval as evaluation

    monkeypatch.setattr(evaluation, "MANAGED_SETTINGS", ())
    monkeypatch.setattr(evaluation, "check_real_claude", lambda path: path)
    seen = []

    def run_probe(binary, **kwargs):
        seen.append((binary, os.environ["PATH"], os.environ["HOME"]))
        return {"status": seen_status[0]}

    monkeypatch.setattr(probe, "run_probe", run_probe)
    for status, code in (("PASS", 0), ("FAIL", 1), ("ALARM", 1), ("INVALID", 1), ("ABORTED", 1)):
        seen_status = [status]
        with pytest.raises(SystemExit) as exit_code:
            evaluation.main(["--probe-deny-read", "--claude-bin", "/x/claude"])
        assert exit_code.value.code == code
        assert json.loads(capsys.readouterr().out) == {"status": status}
    assert seen[0] == ("/x/claude", evaluation.MINIMAL_PATH, str(home))


def test_the_standalone_command_refuses_before_the_cli_when_the_settings_bill_a_key(home, monkeypatch, capsys):
    from evals.prompt_regression import task_manager_eval as evaluation

    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": "echo SECRET"}))
    monkeypatch.setattr(evaluation, "MANAGED_SETTINGS", ())
    monkeypatch.setattr(evaluation, "check_real_claude", lambda path: pytest.fail("the CLI was started"))
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: pytest.fail("the probe ran"))
    with pytest.raises(SystemExit) as exit_code:
        evaluation.main(["--probe-deny-read", "--claude-bin", "/x/claude"])
    assert exit_code.value.code == 2
    shown = capsys.readouterr().err
    assert "apiKeyHelper" in shown and "settings.json" in shown and "SECRET" not in shown


# --- The real CLI against the fake server (opt-in) --------------------------------------------------------------


@pytest.mark.installed
@pytest.mark.parametrize("hook", [None, "exit 2"])
def test_real_cli_denies_every_entry_in_a_synthetic_home(tmp_path, monkeypatch, hook):
    binary = os.environ.get("LOOPER_CLAUDE_PROBE_BIN")
    if not binary:
        pytest.skip("Set LOOPER_CLAUDE_PROBE_BIN to opt into the offline real-CLI probe; a skip is not a pass")
    from evals.prompt_regression import task_manager_eval as evaluation

    binary = evaluation.check_real_claude(str(Path(binary).resolve()))
    home = (tmp_path / "home").resolve()
    for directory in (".ssh", ".claude", ".config/gh", ".docker"):
        (home / directory).mkdir(parents=True)
        (home / directory / "synthetic").write_text("synthetic")
    (home / ".netrc").write_text("synthetic")
    if hook:
        (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": f"echo synthetic >&2; {hook}"}]}]}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root = tmp_path / "probe"
    with evaluation.isolated_environment(root, None, operator_login=True):
        report = probe.run_probe(binary, root=root)
    print(json.dumps(report, indent=2))
    if hook:
        assert report["status"] == "INVALID" and report["markers"]["BLOCKED_BY_HOOK"] > 0
    else:
        assert report["failed"] == [] and report["verified_denied"] >= 5, report
        assert report["controls"] == {"allowed_directory": "ALLOWED", "allowed_file": "ALLOWED",
                                      "denied_directory": "DENIED", "denied_file": "DENIED"}
        # The CLI writes its session into its own configuration directory, an AGENT_STATE entry: listed, no alarm.
        assert report["status"] == "PASS" and report["alarms"] == [], report
        assert {item["path"] for item in report["changed_during_probe"]} <= {"~/.claude"}, report
