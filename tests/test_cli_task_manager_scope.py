"""The task Manager's local command boundary is explicit and closed by default."""

from __future__ import annotations

import argparse
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from looper import child_env, cli


# Keep the intended classification independent of the implementation's table.
ALLOW = [
    ("version", None, []), ("profile", "show", []),
    ("claude-code", "profile", []), ("claude-code", "mcp", []), ("claude-code", "status", []),
    ("ledger", "list", []), ("ledger", "show", ["L-1"]), ("leads", "search", []),
    ("memory", "list", []), ("memory", "show", ["M-1"]), ("memory", "lessons", []),
    ("mcp-server", None, []),
]
REFUSE = [
    ("serve", None, []), ("doctor", None, []), ("config", "init-ops", []),
    ("auth", "set-password", []), ("auth", "clear-password", []),
    *(("service", sub, []) for sub in ("install", "probe", "restart", "upgrade", "status", "uninstall")),
    ("claude-code", "install", []), ("claude-code", "uninstall", []),
    ("ledger", "add", ["--kind", "pending", "--title", "A task"]),
    ("ledger", "close", ["L-1"]), ("ledger", "confirm", ["L-1"]),
    ("leads", "mark", ["lead-1", "--inaccurate"]), ("leads", "delete", ["lead-1"]),
    ("leads", "eval-export", ["--out", "synthetic.jsonl"]), ("memory", "check", []),
    *(("memory", sub, ["M-1"]) for sub in ("reject", "retry", "revoke", "renew", "delete")),
    ("knowledge", "migrate", ["--project", "synthetic"]),
    ("msgflow", "import", ["--days", "1"]), ("msgflow", "parity", []),
    ("msgflow", "delete", ["--message", "synthetic-message"]), ("msgflow", "leads-backfill", []),
    ("msgflow", "why", ["synthetic-message"]), ("decisions", "replay", ["task-x"]),
]
CASES = [(command, sub, extra, allowed) for allowed, entries in ((True, ALLOW), (False, REFUSE))
         for command, sub, extra in entries]


def _subparsers(parser):
    return next((action for action in parser._actions if isinstance(action, argparse._SubParsersAction)), None)


def test_scope_table_complete():
    commands = _subparsers(cli.build_parser())
    pairs = set()
    for command, parser in commands.choices.items():
        children = _subparsers(parser)
        if children:
            pairs.update((command, sub) for sub in children.choices)
        else:
            pairs.add((command, None))
    assert pairs == set(cli.TASK_MANAGER_SCOPE)
    assert {(command, sub) for command, sub, _, _ in CASES} == pairs


@pytest.fixture
def dispatches(monkeypatch, tmp_path):
    """Replace every dispatch target before invoking main; no service or database is touched."""
    targets = {
        "serve": "looper.agents.serve.serve_agent", "doctor": "looper.agents.self_check.doctor",
        "auth-set": "looper.agents.auth_commands.set_password",
        "auth-clear": "looper.agents.auth_commands.clear_password",
        "config": "looper.cli._init_ops", "service": "looper.cli._service",
        "profile": "looper.agents.profile_commands.show", "claude-code": "looper.agents.claude_code_entry.run",
        "mcp-server": "looper.agents.mcp_server.main",
        **{name: f"looper.agents.{module}_commands.run" for name, module in (
            ("msgflow", "msgflow"), ("ledger", "ledger"), ("leads", "leads"),
            ("knowledge", "knowledge"), ("memory", "memory"), ("decisions", "decision"))},
    }
    mocks = {}
    for command, target in targets.items():
        module, _, attribute = target.rpartition(".")
        mocks[command] = Mock(return_value=0)
        monkeypatch.setattr(importlib.import_module(module), attribute, mocks[command])
    monkeypatch.setattr(cli, "_load_ops", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr("looper.ops_config.runtime_root", lambda _: (tmp_path / "runtime", None))
    monkeypatch.delenv(child_env.AGENT_RUN_ENV, raising=False)
    return mocks


@pytest.mark.parametrize("command,sub,extra,allowed", CASES,
                         ids=["-".join(filter(None, (c, s))) for c, s, _, _ in CASES])
@pytest.mark.parametrize("in_manager", [True, False], ids=["task-manager", "operator"])
def test_scope_refusals(monkeypatch, capsys, dispatches, command, sub, extra, allowed, in_manager):
    if in_manager:
        monkeypatch.setenv(child_env.TASK_MANAGER_ENV, "task-x")
    else:
        monkeypatch.delenv(child_env.TASK_MANAGER_ENV, raising=False)
    scope = cli.CommandScope.ALLOW if allowed else cli.CommandScope.REFUSE
    assert cli.TASK_MANAGER_SCOPE.get((command, sub)) is scope
    argv = [command, *([sub] if sub else []), *extra]
    refused = in_manager and not allowed
    assert cli.main(argv) == (4 if refused else 0)
    output = capsys.readouterr()
    if refused:
        name = " ".join(filter(None, (command, sub)))
        assert output.err == (f"error: looper {name} is the operator's own; inside a task Manager it is refused. "
                              "Run it in your own terminal.\n")
        assert all(mock.call_count == 0 for mock in dispatches.values())
    elif command == "version":
        assert output.out.startswith("looper ")
    else:
        target = ("auth-set" if sub == "set-password" else "auth-clear") if command == "auth" else command
        dispatches[target].assert_called_once()
        assert sum(mock.call_count for mock in dispatches.values()) == 1


@pytest.mark.parametrize("group", [False, True], ids=["command", "subcommand"])
def test_scope_refusals_unlisted(monkeypatch, capsys, dispatches, group):
    parser = cli.build_parser()
    commands = _subparsers(parser)
    if group:
        _subparsers(commands.choices["service"]).add_parser("unclassified")
        argv = ["service", "unclassified"]
    else:
        unknown = commands.add_parser("unclassified")
        unknown.set_defaults(host=None, port=None, runtime_root=None, config=None, verbose=False, skip_backup=False)
        argv = ["unclassified"]
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setenv(child_env.TASK_MANAGER_ENV, "task-x")
    assert cli.main(argv) == 4
    assert "is the operator's own; inside a task Manager it is refused" in capsys.readouterr().err
    assert all(mock.call_count == 0 for mock in dispatches.values())


def test_scope_allows_session_start_hook(monkeypatch, dispatches):
    monkeypatch.setenv(child_env.TASK_MANAGER_ENV, "task-x")
    assert cli.main(["claude-code", "profile", "--hook"]) == 0
    assert dispatches["claude-code"].call_args.args[0].hook is True


@pytest.mark.parametrize("task", ["task-x", "another-task"])
def test_scope_own_task(monkeypatch, capsys, dispatches, task):
    monkeypatch.setenv(child_env.TASK_MANAGER_ENV, "task-x")
    monkeypatch.setitem(cli.TASK_MANAGER_SCOPE, ("decisions", "replay"), cli.CommandScope.OWN_TASK)
    assert cli.main(["decisions", "replay", task]) == (0 if task == "task-x" else 4)
    if task == "task-x":
        dispatches["decisions"].assert_called_once()
    else:
        assert "only this task's" in capsys.readouterr().err
        assert all(mock.call_count == 0 for mock in dispatches.values())


@pytest.mark.parametrize("argv,message", [
    (["ledger", "add", "--kind", "pending", "--title", "A task"],
     "error: a ledger write is the user's own; run it in your own terminal, not from an agent\n"),
    (["leads", "search"], "消息线索只供操作者查询和修改，请在自己的终端运行\n"),
    (["memory", "list"], "记忆候选只供操作者查看，请在自己的终端运行\n"),
])
def test_agent_run_unchanged(monkeypatch, capsys, argv, message):
    monkeypatch.setenv(child_env.AGENT_RUN_ENV, "synthetic-run")
    monkeypatch.delenv(child_env.TASK_MANAGER_ENV, raising=False)
    load = Mock(side_effect=AssertionError("agent refusal must precede configuration access"))
    monkeypatch.setattr(cli, "_load_ops", load)
    assert cli.main(argv) == 4
    assert capsys.readouterr().err == message
    load.assert_not_called()
