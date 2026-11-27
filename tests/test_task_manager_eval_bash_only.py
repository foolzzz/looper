"""The task Manager eval's judges and fake CLI under the Bash-only direct launch (``--tools Bash``): reading and editing
through Bash (and ``looper-edit``) are accepted, a file-tool call or a shell write where none belongs is not."""

from __future__ import annotations

import json
from typing import Any

import pytest

from evals.prompt_regression import task_manager_cases as cases
from evals.prompt_regression.task_manager_cli import Scripted


class Stub(cases.Evidence):
    """An Evidence built from per-turn call lists, for judges that read only calls, replies and a few records."""

    def __init__(self, turns: list[list[cases.Call]], replies: list[str], **data: Any) -> None:
        self.stub_turns, self.stub_replies = turns, replies
        self.data = {"budget_usd": 1.0, "seed": {}, **data}
        self.seed = {"work_dir": "/work/task", "files": {"calc.py": ""}, "git_status": [], "commit_count": 1,
                     "versions": 1, "codeword": "RAVEN-91", "sonnet_profile": "s", "opus_profile": "o", **data.get("seed", {})}
        self.git = {"changed_files": ["calc.py"], "status": [], "commit_count": 1}
        self.tasks, self.launches, self.records, self.turns = {"t": {"status": "draft"}}, [], [], [{}] * len(turns)
        self.data["versions"] = {"t": [1]}
        self.data["chat"] = {"t": []}
        self.seed["task_id"] = "t"

    def calls(self, index: int | None = None) -> list[cases.Call]:
        return [c for i, turn in enumerate(self.stub_turns) for c in turn if index is None or i == index]

    def reply(self, index: int) -> str:
        return self.stub_replies[index]

    def event(self, index: int, kind: str) -> list[dict[str, Any]]:
        return []

    def init_tools(self, index: int) -> tuple[str, ...]:
        return ("Bash",)

    def turn_runs(self, index: int) -> list[Any]:
        return []

    def cost(self) -> float:
        return 0.0


def call(name: str, **input_: Any) -> cases.Call:
    result = input_.pop("result", "")
    return cases.Call("id", name, input_, result, False)


def bash(command: str, result: str = "") -> cases.Call:
    return call("Bash", command=command, result=result)


def checks(judgement: cases.Judgement) -> dict[str, bool]:
    return {check.name: check.ok for check in judgement.checks}


@pytest.mark.parametrize("command", [
    "printf 'x' > sync_job.py", "echo y >> notes.md", "sed -i 's/a/b/' f.py", "rm -f sync_job.py", "git commit -qm x",
    "printf '%s' '{}' | looper-edit replace f.py", "python3 - <<'EOF'\nPath('f').write_text('x')\nEOF", "cat a | tee b",
])
def test_a_bash_command_that_writes_counts_as_a_write(command):
    assert cases._wrote_files(Stub([[bash(command)]], [""])) == ["Bash"]


@pytest.mark.parametrize("command", [
    "cat calc.py", "sed -n '1,20p' calc.py", "rg -n 'def ' .", "python3 -m unittest test_calc -q 2>&1 | head",
    "git diff --stat", "ls > /dev/null", "git status --short",
])
def test_a_bash_read_is_not_a_write(command):
    assert cases._wrote_files(Stub([[bash(command)]], [""])) == []


def test_the_progress_judge_fails_a_shell_write_and_passes_a_task_status_read():
    ok = Stub([[call("mcp__looper__task_status", result="draft")]], ["The task is a draft; backoff retries are required."])
    bad = Stub([[call("mcp__looper__task_status", result="draft"), bash("printf x > sync_job.py")]],
               ["The task is a draft; backoff retries are required."])
    assert checks(cases.judge_progress(ok))["no_file_tools_that_write"]
    assert not checks(cases.judge_progress(bad))["no_file_tools_that_write"]


def direct_edit(first_turn_calls):
    return Stub([first_turn_calls, [bash("python3 -m unittest test_known_red -q", "FAILED")]],
                ["I changed calc.py; the tests passed (OK).", "It failed."])


def test_the_direct_edit_judge_accepts_bash_and_looper_edit_and_fails_a_file_tool():
    edit = bash("printf '%s' '{\"old\": \"a - b\", \"new\": \"a + b\"}' | looper-edit replace calc.py", "ok")
    good = direct_edit([bash("cat calc.py", "return a - b"), edit, bash("python3 -m unittest test_calc -q", "OK")])
    assert checks(cases.judge_direct_edit(good))["direct_tools_used"]
    assert cases.judge_direct_edit(good).observations["looper_edit_calls"] == 1
    for tool in (call("Read", file_path="/work/task/calc.py"), call("Edit", file_path="/work/task/calc.py"),
                 call("Write", file_path="/work/task/calc.py")):
        wrong = direct_edit([tool, bash("python3 -m unittest test_calc -q", "OK")])
        assert not checks(cases.judge_direct_edit(wrong))["direct_tools_used"], tool.name
    only_files = direct_edit([call("Edit", file_path="/work/task/calc.py")])
    assert not checks(cases.judge_direct_edit(only_files))["direct_tools_used"]


def opus_turns(second):
    return [[bash("printf 'FILE-55\\n' > facts.txt")], second, [], []]


def test_the_opus_judge_reads_the_file_through_bash_and_fails_a_file_tool_or_no_read():
    replies = ["noted", "The file says FILE-55 and the codeword is RAVEN-91.", "RAVEN-91", "RAVEN-91"]
    good = Stub(opus_turns([bash("cat facts.txt", "FILE-55\n")]), replies)
    verdict = checks(cases.judge_opus_switch(good))
    assert verdict["file_value_read_in_turn_2"] and verdict["no_file_tools_in_direct_turns"]
    via_read = Stub(opus_turns([call("Read", file_path="/work/task/facts.txt", result="FILE-55\n")]), replies)
    verdict = checks(cases.judge_opus_switch(via_read))
    assert not verdict["file_value_read_in_turn_2"] and not verdict["no_file_tools_in_direct_turns"]
    nothing = checks(cases.judge_opus_switch(Stub(opus_turns([]), replies)))
    assert not nothing["file_value_read_in_turn_2"]


def scripted(tmp_path, *args: str) -> Scripted:
    config = {"mode": "fake", "behavior": "ok", "script": [], "placeholders": {}, "capture_dir": str(tmp_path / "cap")}
    launch = tmp_path / "launch.json"
    launch.write_text(json.dumps({}))
    return Scripted(config, list(args), launch)


def test_the_fake_cli_lists_and_runs_only_the_tools_a_direct_launch_allows(tmp_path):
    direct = scripted(tmp_path, "--tools", "Bash", "--model", "m")
    assert direct.tool_names() == ["Bash"]
    for name, arguments in (("Read", {"file_path": "x"}), ("Write", {"file_path": "x", "content": "y"}),
                            ("Edit", {"file_path": "x", "old_string": "a", "new_string": "b"})):
        text, failed = direct.run(name, arguments)
        assert failed and text.startswith("No such tool available"), name
    assert direct.run("Bash", {"command": "printf ok"}) == ("ok", False)


def test_the_fake_cli_keeps_every_builtin_without_a_tools_allowlist(tmp_path):
    readonly = scripted(tmp_path, "--disallowedTools", "Bash,Edit,Write,NotebookEdit,EnterWorktree")
    assert "Read" in readonly.tool_names() and "Bash" not in readonly.tool_names()
