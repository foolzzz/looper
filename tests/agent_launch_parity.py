"""What each role's Claude Code is started with, captured through the real agent tiers.

``capture(root, scenario)`` runs the Manager, the Planner, a Worker and a
Reviewer of ``scenario`` the way the server does (``ManagerAgent._run_turn``,
``PlannerAgent._run_turn``, ``WorkerPool._execute``) against the database the
caller set up (``connection._pool``), each with a stand-in ``claude`` that
writes down what it was started with: its argv (the executable included), the
environment variables Looper set, the appended system prompt and the first
user turn on its stdin. Paths under ``root`` read ``<root>``; the
directories above it, which differ between machines, read ``<above>`` in the
isolation settings.

``seed_projects`` writes the projects, and ``seed_rest`` the tasks, with SQL and API
functions that work on the tables of the v2.12 registry and of this code alike:
``tests/model_launch_parity.py`` runs them on the old checkout that made the
v2.12 launch fixtures and on the converted rows.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from fake_claude import fake_claude  # noqa: E402

PROJECT_A, PROJECT_B = "proj-0000000000a1", "proj-0000000000b1"
TASKS = {PROJECT_A: "task-0000000000a1", PROJECT_B: "task-0000000000b1"}
PROJECTS = {PROJECT_A: ("calc", "Calc background."), PROJECT_B: ("docs", "")}
REQUIREMENT = "Add an add(a, b) function to calc.py."

# The one scenario: no agent setting layers; the roles captured ("role" or "role@project").
SCENARIOS: dict[str, dict[str, Any]] = {
    "unconfigured": {
        "cases": ["manager", f"planner@{PROJECT_A}", f"worker@{PROJECT_A}", f"reviewer@{PROJECT_A}"],
    },
}


async def seed_projects(pool: Any) -> None:
    await pool.executemany(
        "INSERT INTO projects (id, name, background) VALUES ($1, $2, $3)",
        [(project_id, name, background) for project_id, (name, background) in PROJECTS.items()],
    )


async def seed_rest(scenario: str, root: Path) -> None:
    """The tasks (``scenario`` is the old checkouts' signature, whose scenarios differ in what else they seed)."""
    from looper.db import tasks

    for project_id, task_id in TASKS.items():
        await tasks.create_task(task_id=task_id, requirement=REQUIREMENT, project_id=project_id,
                                work_dir=str(_work_dir(root, project_id)), status="running")


def _work_dir(root: Path, project_id: str) -> Path:
    return root / "work" / project_id


# A stand-in claude: it writes down what it was started with when it reads its first user turn, and answers
# every turn the way Claude Code 2.1.285 does with --replay-user-messages (lifecycle events and a replay named
# by the message's uuid, then a result that names it).
_CLAUDE = """\
import json, os, sys
argv = sys.argv[1:]
prompt = None
if any(f in argv for f in ('--append-system-prompt-file', '--system-prompt-file')):
    prompt = open(argv[argv.index(next(f for f in ('--append-system-prompt-file', '--system-prompt-file') if f in argv)) + 1], encoding='utf-8').read()
seen = {{'executable': sys.argv[0], 'argv': argv, 'env': dict(os.environ), 'system_prompt': prompt}}
def emit(event):
    print(json.dumps(event), flush=True)
reply = json.dumps({{'status': 'completed', 'summary': 'ok', 'passed': True, 'findings': []}})
for n, line in enumerate(sys.stdin, 1):
    message = json.loads(line)
    if n == 1:
        seen['stdin'] = message['message']['content']
        with open({out!r}, 'w', encoding='utf-8') as f:
            json.dump(seen, f)
    mid = message.get('uuid')
    if mid:
        for state in ('queued', 'started'):
            emit({{'type': 'command_lifecycle', 'command_uuid': mid, 'state': state}})
    emit({{'type': 'system', 'subtype': 'init', 'session_id': 'sess-parity', 'model': 'stand-in'}})
    if mid:
        emit({{'type': 'user', 'message': {{'role': 'user', 'content': message['message']['content']}},
              'session_id': 'sess-parity', 'parent_tool_use_id': None, 'uuid': mid, 'isReplay': True}})
    emit({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': reply}}]}},
          'parent_tool_use_id': None}})
    emit({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': reply, 'total_cost_usd': 0,
          'session_id': 'sess-parity', **({{'user_message_uuid': mid, 'user_message_uuids': [mid]}} if mid else {{}})}})
    if mid:
        emit({{'type': 'command_lifecycle', 'command_uuid': mid, 'state': 'completed'}})
"""

_MEMORY_NAMES = (".claude/rules/**", ".claude/CLAUDE.md", "CLAUDE.md")


def _isolation_settings(raw: str, root: str) -> str:
    """The ``--settings`` value with the directories above ``root`` as ``<above>`` (each name once).

    The value must be what ``json.dumps(..., ensure_ascii=False, separators=(",", ":"))`` writes, so the
    normalized text stands for the bytes.
    """
    settings = json.loads(raw)
    if json.dumps(settings, ensure_ascii=False, separators=(",", ":")) != raw:
        raise AssertionError(f"--settings is not in its canonical form: {raw}")
    patterns = settings.get("claudeMdExcludes", [])
    # Each excluded CLAUDE.md has its AGENTS.md beside it, which Claude Code reads where a project has no CLAUDE.md;
    # the fixtures predate them, so they are set aside once checked.
    agents = [pattern for pattern in patterns if pattern.endswith("/AGENTS.md")]
    beside = [pattern.removesuffix("CLAUDE.md") + "AGENTS.md" for pattern in patterns if pattern.endswith("/CLAUDE.md")]
    if sorted(agents) != sorted(beside):
        raise AssertionError(f"an excluded CLAUDE.md without its AGENTS.md, or the other way round: {patterns}")
    excludes: list[str] = []
    for pattern in (pattern for pattern in patterns if pattern not in agents):
        if not pattern.startswith(root + "/"):
            name = next(n for n in _MEMORY_NAMES if pattern.endswith("/" + n))
            pattern = f"<above>/{name}"
        if pattern not in excludes:
            excludes.append(pattern)
    settings["claudeMdExcludes"] = excludes
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":")).replace(root, "<root>")


def _normalized(seen: dict[str, Any], root: str, executable: str, environ: dict[str, str]) -> dict[str, Any]:
    from model_launch_parity import with_planner_file_reads

    argv = with_planner_file_reads(seen["argv"])
    if "--settings" in argv:
        at = argv.index("--settings") + 1
        argv[at] = _isolation_settings(argv[at], root)
    if any(f in argv for f in ("--append-system-prompt-file", "--system-prompt-file")):
        # The file is named by its text's hash, and the text names directories under root.
        at = argv.index(next(f for f in ("--append-system-prompt-file", "--system-prompt-file") if f in argv)) + 1
        path = Path(argv[at])
        if path.name != hashlib.sha256((seen["system_prompt"] or "").encode("utf-8")).hexdigest() + ".md":
            raise AssertionError(f"the system prompt file {path} is not named by its text's hash")
        argv[at] = str(path.with_name("<sha256 of the system prompt>.md"))
    return {
        "executable": "<claude>" if seen["executable"] == executable else seen["executable"],
        "argv": [arg.replace(root, "<root>") for arg in argv],
        # The variables Looper set: those whose value differs from the server's.
        "env": {key: value for key, value in sorted(seen["env"].items()) if environ.get(key) != value},
        "system_prompt": (seen["system_prompt"] or "").replace(root, "<root>"),
        "stdin": seen["stdin"].replace(root, "<root>"),
    }


def without_agent_teams(argv: list[str]) -> list[str]:
    """``argv`` as a run without agent teams has it: less ``--teammate-mode in-process``, which it must have, and with
    SendMessage denied beside ListAgents, where a run with teams keeps it for its teammates (``claude_cli.peer_args``)."""
    at = argv.index("--teammate-mode")
    assert argv[at + 1] == "in-process", argv
    argv = argv[:at] + argv[at + 2:]
    denied = argv.index("--disallowedTools") + 1
    assert argv[denied] == "ListAgents", argv
    return [*argv[:denied], "ListAgents,SendMessage", *argv[denied + 1:]]


async def _manager(claude: str, root: Path) -> None:
    from looper.agents.message import MANAGER_ADDRESS, AgentAddress, MessageEnvelope, MessageType
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    work_dir = root / "manager"
    work_dir.mkdir(exist_ok=True)
    manager = ManagerAgent(config=ManagerConfig(claude_bin=claude, work_dir=work_dir), runtime_root=root / "runtime")

    async def _quiet(*_args: Any, **_kwargs: Any) -> None:
        return None

    manager._send_status = _quiet  # type: ignore[method-assign]
    envelope = MessageEnvelope.create(source=AgentAddress(agent_type="channel", agent_id="web", channel="web"),
                                      target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
                                      payload={"text": "What can you do?"})
    try:
        await manager._run_turn("What can you do?", envelope)
    finally:
        await manager._claude.stop()


async def _planner(claude: str, root: Path, project_id: str) -> None:
    from looper.agents.tiers.planner import PlannerAgent, TaskPlan

    task_id = TASKS[project_id]
    planner = PlannerAgent(task_id=task_id, runtime_root=root / "runtime", claude_bin=claude)
    plan = TaskPlan(plan_id="plan-parity", goal_id="", requirement=REQUIREMENT,
                    work_dir=str(_work_dir(root, project_id)), project_id=project_id)
    planner._plan = plan
    await planner._refresh_project(plan)
    try:
        await planner._run_turn(planner._plan_message(plan))
    finally:
        await planner._claude.stop()


async def _worker(claude: str, root: Path, project_id: str, *, verify: bool) -> None:
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool

    task_id = TASKS[project_id]
    payload: dict[str, Any] = {
        "task_id": "step-parity", "parent_task_id": task_id, "plan_id": "plan-parity", "step_key": "add",
        "instruction": "Write add() in calc.py.", "acceptance_criteria": ["add(1, 2) == 3"],
        "validation_commands": ["python -c 'import calc'"], "requirement": REQUIREMENT,
        "context": {"work_dir": str(_work_dir(root, project_id))},
        "dependencies": [{"step": "init", "summary": "created the package", "changed_files": ["calc.py"]}],
    }
    if verify:
        payload["work_result"] = {"status": "completed", "summary": "added add()", "changed_files": {"calc.py": "add"}}
    pool = WorkerPool(config=WorkerConfig(claude_bin=claude), runtime_root=root / "runtime")
    await pool._execute(MessageEnvelope.create(
        source=planner_address(task_id), target=worker_address("reviewer-parity" if verify else "worker-parity"),
        message_type=MessageType.VERIFY_REQUEST if verify else MessageType.WORK_ITEM, payload=payload,
    ), is_verify=verify)


async def capture(root: Path, scenario: str) -> dict[str, dict[str, Any]]:
    """What every case of ``scenario`` started Claude Code with; ``root`` holds the runtime and work directories.

    The caller has set up the database (``seed_*``) and ``HOME`` (under ``root``), and unset
    ``CLAUDE_CONFIG_DIR``.
    """
    root = root.resolve()
    (root / "bin").mkdir(exist_ok=True)
    (root / "seen").mkdir(exist_ok=True)
    captured: dict[str, dict[str, Any]] = {}
    for case in SCENARIOS[scenario]["cases"]:
        role, _, project_id = case.partition("@")
        out = root / "seen" / f"{role}-{project_id or 'none'}.json"
        claude = fake_claude(root / "bin" / f"claude-{role}-{project_id or 'none'}", _CLAUDE.format(out=str(out)))
        if project_id:
            _work_dir(root, project_id).mkdir(parents=True, exist_ok=True)
        environ = dict(os.environ)
        if role == "manager":
            await _manager(claude, root)
        elif role == "planner":
            await _planner(claude, root, project_id)
        else:
            await _worker(claude, root, project_id, verify=role == "reviewer")
        captured[case] = _normalized(json.loads(out.read_text(encoding="utf-8")), str(root), claude, environ)
    return captured
