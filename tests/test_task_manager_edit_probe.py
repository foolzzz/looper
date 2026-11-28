"""Opt-in, offline real-CLI checks of ``looper-edit`` and the fenced project instructions in a direct session.

Run with ``LOOPER_CLAUDE_PROBE_BIN`` set to a real Claude Code binary (never a terminal shim) and a ``--basetemp``
outside ``/tmp``; no real model is called: the CLI talks to a scripted local Messages API with a scratch ``HOME``.
The command is the real ``CLAUDE_CODE.operator_resident_command`` with the sandbox, and the environment the real
``task_manager_safety.environment`` builds with ``task_manager_edit.edit_bin``. A skip is not a pass.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from looper import child_env
from looper.agents import task_manager_edit, task_manager_project_instructions as instructions
from looper.agents import task_manager_safety, task_manager_sandbox
from looper.agents.task_manager_brief import instructions as fixed_instructions
from looper.db.tasks import InteractionMode
from sandbox_probe_support import SandboxProbe, text, tool_use

pytestmark = pytest.mark.installed

MARKER = "PROBE-MARKER-7f3a1c"


@pytest.fixture
def probe(tmp_path, request, monkeypatch):
    binary = os.environ.get("LOOPER_CLAUDE_PROBE_BIN")
    if not binary:
        pytest.skip("Set LOOPER_CLAUDE_PROBE_BIN to opt into the offline real-CLI probe; a skip is not a pass")
    assert not str(tmp_path.resolve()).startswith(("/private/tmp/", "/tmp/")), "use a --basetemp outside /tmp"
    instance = SandboxProbe(tmp_path, str(Path(binary).resolve()))
    # The real environment() builds the child's environment from the scratch one, with looper-edit first on PATH.
    monkeypatch.setattr(child_env, "inherited", lambda environ=None: dict(instance.env))
    launch = instance.launch()
    edit_dir = task_manager_edit.edit_bin(instance.env, forbidden=(str(instance.cwd), str(instance.tmp), *instance.read_deny))
    environment = task_manager_safety.environment(launch, "probe", drop=task_manager_sandbox.DIRECT_DROPPED_ENV,
                                                  edit_dir=edit_dir)
    assert environment["PATH"].split(os.pathsep)[0] == str(edit_dir)
    instance.env.update(environment)
    instance.edit_dir = edit_dir
    try:
        yield instance
    finally:
        instance.close()
        evidence = tmp_path / "evidence.json"
        evidence.write_text(json.dumps({"check": request.node.name, **instance.evidence()}, indent=2))
        print(f"Evidence: {evidence}")


def bash(command: str) -> dict:
    return tool_use("Bash", {"command": command})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_e1_looper_edit_is_found_on_the_sandboxed_bash_path(probe):
    script = [bash("command -v looper-edit"), text("done")]
    run = probe.run(script)
    found = run.result_of(script[0]["id"])
    assert str(probe.edit_dir / "looper-edit") in found, found
    probe.observations["e1"] = {"command_v": found.strip()}


def test_e2_replace_and_patch_edit_the_task_directory_and_a_stale_edit_changes_nothing(probe):
    (probe.cwd / "f.txt").write_text("alpha\nbeta\n")
    (probe.cwd / "one.txt").write_text("1\n2\n")
    (probe.cwd / "two.txt").write_text("x\ny\n")
    diff = ("--- a/one.txt\n+++ b/one.txt\n@@ -1,2 +1,2 @@\n 1\n-2\n+two\n"
            "--- a/two.txt\n+++ b/two.txt\n@@ -1,2 +1,2 @@\n-x\n+ex\n y\n")
    stale = "printf '%s' '{\"old\":\"missing text\",\"new\":\"z\"}' | looper-edit replace f.txt; echo rc=$?"
    script = [
        bash("printf '%s' '{\"old\":\"alpha\",\"new\":\"ALPHA\"}' | looper-edit replace f.txt; echo rc=$?"),
        bash(f"looper-edit patch <<'DIFF'\n{diff}DIFF\necho rc=$?"),
        bash(stale),
        text("done"),
    ]
    run = probe.run(script)
    replaced, patched, refused = [run.result_of(turn["id"]) for turn in script[:3]]
    assert "rc=0" in replaced and "rc=0" in patched, (replaced, patched)
    assert (probe.cwd / "f.txt").read_text() == "ALPHA\nbeta\n"
    assert (probe.cwd / "one.txt").read_text() == "1\ntwo\n" and (probe.cwd / "two.txt").read_text() == "ex\ny\n"
    assert "rc=1" in refused and "no match" in refused, refused
    assert (probe.cwd / "f.txt").read_text() == "ALPHA\nbeta\n"  # the stale edit left the bytes as they were
    probe.observations["e2"] = {"replace": replaced.strip(), "patch": patched.strip(), "stale": refused.strip(),
                                "f_sha256": digest(probe.cwd / "f.txt")}


def test_e3_the_project_file_reaches_the_model_once_inside_its_fence_and_not_as_cli_memory(probe):
    (probe.cwd / "CLAUDE.md").write_text(f"Always use four spaces. {MARKER}\n")
    loaded, skipped = instructions.read(str(probe.cwd))
    assert [item.name for item in loaded] == ["CLAUDE.md"] and not skipped
    command = probe.command(probe.cwd)
    (probe.root / "prompt.md").write_text(
        fixed_instructions(InteractionMode.DIRECT) + "\n\n" + instructions.section(loaded))
    run = probe.run([text("done")], command=command)
    [first, *_] = run.requests
    request = json.dumps({"system": first["system"], "messages": first["messages"]}, ensure_ascii=False)
    assert request.count(MARKER) == 1, request.count(MARKER)
    fenced = '<untrusted source=\\"project_instructions\\" file=\\"CLAUDE.md\\">'
    assert request.index(fenced) < request.index(MARKER) < request.index("</untrusted>", request.index(fenced))
    probe.observations["e3"] = {"marker_count": request.count(MARKER), "system_blocks": len(first["system"] or [])}


def test_e3_positive_control_without_the_boundary_the_cli_loads_the_project_file_as_memory_too(probe):
    (probe.cwd / "CLAUDE.md").write_text(f"Always use four spaces. {MARKER}\n")
    loaded, _ = instructions.read(str(probe.cwd))
    command = probe.command(probe.cwd, control="loose")
    (probe.root / "prompt.md").write_text(
        fixed_instructions(InteractionMode.DIRECT) + "\n\n" + instructions.section(loaded))
    run = probe.run([text("done")], command=command)
    request = json.dumps({"system": run.requests[0]["system"], "messages": run.requests[0]["messages"]})
    assert request.count(MARKER) == 2, request.count(MARKER)  # the fence, and the CLI's own memory copy
    probe.observations["e3_control"] = {"marker_count": request.count(MARKER)}
