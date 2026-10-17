from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The tests read LOOPER_TEST_DATABASE_URL; its name from before the rename still works for one release.
if "LOOPER_TEST_DATABASE_URL" not in os.environ and "LOOPX_TEST_DATABASE_URL" in os.environ:
    os.environ["LOOPER_TEST_DATABASE_URL"] = os.environ["LOOPX_TEST_DATABASE_URL"]

_RUNTIME_ROOT_GUARD_DIR = Path(__file__).resolve().parent / "runtime_root_guard"
sys.path.insert(0, str(_RUNTIME_ROOT_GUARD_DIR))

# Importing the plugin starts the real-runtime-root guard before any test runs;
# re-exporting its fixture and hooks registers them for this suite.
from real_runtime_root_guard_plugin import (  # noqa: E402,F401
    _refuse_real_looper_runtime_root,
    pytest_sessionfinish,
    pytest_terminal_summary,
    pytest_testnodedown,
)
from fake_claude import fake_claude  # noqa: E402
from lark_cli_stand_in import lark_cli_stand_in  # noqa: E402,F401 (a fixture for every test)


# PostgreSQL writes made by the agent tiers (Manager, Planner, TaskRouter, WorkerPool).
_AGENT_DB_WRITES = (
    ("projects", "create_project"),
    ("tasks", "update_task_status"),
    ("tasks", "update_task_phase"),
    ("activity", "insert_activity"),
    ("llm_records", "start_record"),
    ("llm_records", "append_events"),
    ("llm_records", "finish_record"),
    ("llm_records", "add_metadata"),
    ("llm_records", "touch_record"),
    ("llm_records", "update_event"),
)

# Reads the agents make, answered as a database that holds no projects and no settings yet.
_AGENT_DB_READS = (
    ("projects", "list_projects", []),
    ("projects", "find_project_by_name", None),
    ("projects", "get_project", None),
    ("projects", "get_task_project", None),
    ("tasks", "list_tasks", []),
    ("tasks", "others_active_in", False),
    ("activity", "list_activity", []),
    ("system_settings", "get_settings", {}),
    ("agent_setting_layers", "list_layers", []),
    ("library", "list_installed", []),
    ("library", "get_item_files", []),
    ("project_documents", "list_documents", []),
    ("project_documents", "get_content", None),
    ("chat", "recent_conversations", []),
)


class _PlanStore:
    """The Planner's and Manager's saved state, kept in memory the way ``plans`` and ``agent_sessions`` keep it.

    ``tasks`` holds the task rows a plan is read back with (``requirement``,
    ``goal_id``, ``work_dir``, ``project_id``); a test that restarts a Planner
    sets its task there. ``ready`` holds the tasks created ready
    (``create_task``) until one is started (``start_ready_task``). Every
    journal entry gets an id; a supplement (``add_supplement``) is pending
    until ``mark_delivered``, and ``requirements`` holds a task's requirement
    edit not delivered yet (``task_changes.RequirementEdit``), as
    ``task_changes.pending`` reads them. ``gates`` holds each task's user gates
    by step id and ``gate_messages`` their discussions, written and read the
    way ``user_gates`` writes and reads them.
    """

    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, dict[str, dict[str, Any]]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.journal: dict[str, list[dict[str, Any]]] = {}
        self.ready: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[int, dict[str, Any]] = {}  # step_workspaces rows by id
        self.workspace_ids = 0
        self.requirements: dict[str, Any] = {}
        self.gates: dict[str, dict[str, dict[str, Any]]] = {}
        self.gate_messages: list[dict[str, Any]] = []
        self._ids = iter(range(1, 1_000_000))

    async def add_supplement(self, task_id: str, text: str, *, via: str, **_: object) -> int:
        entry_id = next(self._ids)
        self.journal.setdefault(task_id, []).append({"id": entry_id, "step_id": "", "kind": "supplement",
                                                     "summary": text, "learnings": [], "via": via,
                                                     "delivered": False})
        return entry_id

    async def pending(self, task_id: str, **_: object) -> Any:
        from looper.db.task_changes import Supplement, TaskChanges

        return TaskChanges(
            tuple(Supplement(e["id"], e["summary"], e["via"]) for e in self.journal.get(task_id, [])
                  if e["kind"] == "supplement" and not e["delivered"]),
            self.requirements.get(task_id))

    async def mark_delivered(self, task_id: str, changes: Any, **_: object) -> None:
        delivered = {supplement.id for supplement in changes.supplements}
        for entry in self.journal.get(task_id, []):
            if entry.get("id") in delivered:
                entry["delivered"] = True
        if changes.requirement is not None:
            self.requirements.pop(task_id, None)

    async def list_entries(self, task_id: str, **_: object) -> list[dict[str, Any]]:
        keys = {step["id"]: step["key"] for step in self.steps.get(task_id, {}).values()}
        return [{**entry, "step_key": keys.get(entry["step_id"], "")} for entry in self.journal.get(task_id, [])]

    async def create_task(self, *, task_id: str, status: str = "pending", **row: Any) -> None:
        if status == "init":
            self.ready[task_id] = {"id": task_id, **{k: v for k, v in row.items() if k != "pool"}, "status": "pending"}

    async def start_ready_task(self, task_id: str, **_: object) -> dict[str, Any] | None:
        return self.ready.pop(task_id, None)

    async def save(self, task_id: str, *, plan: Any = None, steps: Any = (), journal: Any = (), gates: Any = (),
                   gate_messages: Any = (), **_: object) -> None:
        if plan is not None:
            self.plans[task_id] = json.loads(json.dumps(dict(plan)))
        for step in steps:
            self.steps.setdefault(task_id, {})[step["id"]] = json.loads(json.dumps(dict(step)))
        for entry in journal:
            self.journal.setdefault(task_id, []).append({"id": next(self._ids), "delivered": True,
                                                         **json.loads(json.dumps(dict(entry), default=str))})
        for gate in gates:
            stored = self.gates.setdefault(task_id, {}).get(gate["step_id"])
            if stored is not None and stored["state"] in ("passed", "cancelled", "skipped"):
                continue  # a closed gate stays as it closed
            row = {"task_id": task_id, "first_reply_at": None, "reminders": 0, **(stored or {}), **dict(gate)}
            if stored is not None:
                if gate["state"] == "open" and stored["state"] == "discussing":
                    row["state"] = "discussing"
                row["delivered_through"] = max(stored["delivered_through"], gate["delivered_through"])
                row["opened_at"] = stored["opened_at"] or gate["opened_at"]
            self.gates[task_id][gate["step_id"]] = row
        for message in gate_messages:
            self.gate_messages.append({"id": next(self._ids), "task_id": task_id, "author": None, "via": "",
                                       "agenda_item": None, "option": None, "created_at": None, **dict(message)})
        for gate in gates:
            row = self.gates[task_id][gate["step_id"]]
            late = [m for m in self.gate_messages if m["step_id"] == row["step_id"] and m["role"] == "user"
                    and m["id"] > row["delivered_through"]]
            if row["state"] in ("passed", "cancelled", "skipped") and late:
                # As ``user_gates._keep_late``: what reached the gate as it closed becomes a supplement.
                from looper.db.user_gates import LATE_VIA, late_text

                label = self.steps.get(task_id, {}).get(row["step_id"], {}).get("key", "")
                texts = [text for m in late if (text := late_text(label, m))]
                for text in texts:
                    self.journal.setdefault(task_id, []).append({
                        "id": next(self._ids), "step_id": row["step_id"], "kind": "supplement", "summary": text,
                        "learnings": [], "via": LATE_VIA, "delivered": False})
                if texts:
                    self.gate_messages.append({
                        "id": next(self._ids), "task_id": task_id, "step_id": row["step_id"], "role": "looper",
                        "kind": "late", "text": f"{len(texts)} message(s) reached this gate as it closed",
                        "author": None, "via": "", "agenda_item": None, "option": None, "created_at": None})
                row["delivered_through"] = max(m["id"] for m in late)

    def _gate_row(self, task_id: str, row: dict[str, Any]) -> dict[str, Any]:
        step = self.steps.get(task_id, {}).get(row["step_id"], {})
        undelivered = [m["id"] for m in self.gate_messages if m["step_id"] == row["step_id"] and m["role"] == "user"
                       and m["id"] > row["delivered_through"]]
        failed_through = row.get("failed_through") or 0
        return {**row, "step_key": step.get("key", ""), "step_index": step.get("index", 0),
                "depends_on": list(step.get("depends_on") or []),
                "waiting_for_planner": any(i > failed_through for i in undelivered),
                "not_delivered": sum(1 for i in undelivered if i <= failed_through)}

    async def list_gates(self, task_id: str, **_: object) -> list[dict[str, Any]]:
        rows = [self._gate_row(task_id, row) for row in self.gates.get(task_id, {}).values()]
        return sorted(rows, key=lambda row: row["step_index"])

    async def get_gate(self, task_id: str, label: str, **_: object) -> dict[str, Any] | None:
        return next((row for row in await self.list_gates(task_id) if row["step_key"] == label), None)

    async def pending_gate_messages(self, task_id: str, **_: object) -> list[dict[str, Any]]:
        gates = self.gates.get(task_id, {})
        return [dict(m) for m in self.gate_messages if m["role"] == "user" and m["step_id"] in gates
                and gates[m["step_id"]]["state"] in ("open", "discussing")
                and m["id"] > gates[m["step_id"]]["delivered_through"]]

    async def gate_thread(self, step_id: str, **_: object) -> list[dict[str, Any]]:
        return [dict(m) for m in self.gate_messages if m["step_id"] == step_id]

    async def add_gate_message(self, task_id: str, label: str, message: Any, **_: object) -> tuple[Any, Any]:
        """The user writes ``message`` in the task's gate ``label``, as ``user_gates.add_user_message`` adds it."""
        gate = await self.get_gate(task_id, label)
        if gate is None:
            return None, None
        row = self.gates[task_id][gate["step_id"]]
        if row["state"] not in ("open", "discussing"):
            return gate, None
        row["state"] = "discussing"
        row["first_reply_at"] = row.get("first_reply_at") or "2026-10-17T00:00:00+00:00"
        added = {"id": next(self._ids), "step_id": gate["step_id"], "task_id": task_id, "role": "user",
                 "kind": "message", "author": None, "via": "dashboard", "agenda_item": None, "option": None,
                 "created_at": None, **dict(message)}
        self.gate_messages.append(added)
        return await self.get_gate(task_id, label), dict(added)

    async def load(self, task_id: str, **_: object) -> dict[str, Any] | None:
        if task_id not in self.plans:
            return None
        task = self.tasks.get(task_id, {})
        rows = []
        for step in sorted(self.steps.get(task_id, {}).values(), key=lambda s: s["index"]):
            row = {k: v for k, v in step.items() if k not in ("index", "key")}
            rows.append({**row, "step_index": step["index"], "step_key": step["key"]})
        plan = {"task_id": task_id, **self.plans[task_id],
                **{k: task.get(k, "") for k in ("requirement", "goal_id", "work_dir", "project_id")}}
        keys = {step["id"]: step["key"] for step in self.steps.get(task_id, {}).values()}
        journal = [{**entry, "step_key": keys.get(entry["step_id"], "")} for entry in self.journal.get(task_id, [])]
        gates = {row["step_id"]: row for row in await self.list_gates(task_id)}
        return {"plan": plan, "steps": rows, "journal": journal, "gates": gates}

    async def next_workspace_id(self, **_: object) -> int:
        self.workspace_ids += 1
        return self.workspace_ids

    async def save_workspace(self, row: Any, **_: object) -> None:
        self.workspaces[int(row["id"])] = dict(row)

    async def list_workspaces(self, task_id: str, **_: object) -> list[dict[str, Any]]:
        return [dict(row) for _, row in sorted(self.workspaces.items()) if row["task_id"] == task_id]

    async def list_kept_workspaces(self, **_: object) -> list[dict[str, Any]]:
        return [{**row, "task_status": self.tasks.get(row["task_id"], {}).get("status", "running")}
                for _, row in sorted(self.workspaces.items()) if row.get("removed_at") is None]

    async def save_session(self, agent_id: str, session_id: str | None, totals: Any, *,
                           runtime_type: str | None = None, profile_id: str | None = None,
                           clean_start: bool = False, **_: object) -> None:
        self.sessions[agent_id] = {"session_id": session_id, "session_totals": dict(totals),
                                   "runtime_type": runtime_type, "profile_id": profile_id,
                                   "clean_start": clean_start}

    async def get_session(self, agent_id: str, **_: object) -> dict[str, Any] | None:
        saved = self.sessions.get(agent_id)
        return None if saved is None else {**saved, "session_totals": dict(saved["session_totals"])}


async def _created_project(
    *, name: str, description: str = "", work_dir: str = "", background: str = "", **_: object,
) -> dict:
    return {"id": "proj-000000000000", "name": name, "description": description, "work_dir": work_dir,
            "background": background}


@pytest.fixture(scope="session", autouse=True)
def _no_operator_codex_login(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """No test reaches the operator's Codex login: the login file a Codex login run links to is ``auth.json`` in
    the server's ``CODEX_HOME`` (``runtimes.codex_login.login_file``), here an empty directory of the session's."""
    previous = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = str(tmp_path_factory.mktemp("codex-home"))
    yield
    if previous is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = previous


@pytest.fixture(scope="session", autouse=True)
def _no_real_lark_cli(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """No test reaches the operator's lark-cli (its signed-in bot and user): a ``lark-cli`` that refuses every call
    comes first on PATH; a test that needs one puts the stand-in of ``lark_cli_stand_in`` before it."""
    directory = tmp_path_factory.mktemp("no-lark-cli")
    fake_claude(directory / "lark-cli", "import json, sys\n"
                "print(json.dumps({'ok': False, 'error': {'message': 'lark-cli is not available in tests'}}),"
                " file=sys.stderr)\nsys.exit(2)\n")
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{directory}{os.pathsep}{previous}"
    yield
    os.environ["PATH"] = previous


@pytest.fixture(scope="session")
def _stub_claude(tmp_path_factory: pytest.TempPathFactory) -> Path:
    stub = tmp_path_factory.mktemp("stub-claude") / "claude"
    fake_claude(stub, "import sys\nsys.stdin.read()\n")
    return stub


@pytest.fixture
def stub_claude_on_path(_stub_claude: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make a bare ``claude`` resolve to a stub that answers nothing.

    Each task's Planner starts ``claude`` as soon as it gets its task, so a
    message-flow test that routes a task without a stand-in binary would
    otherwise reach the real CLI (and spend money). The stub waits for its
    stdin to close and exits without output.
    """
    monkeypatch.setenv("PATH", f"{_stub_claude.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return _stub_claude


_PLANNER_CLAUDE = """\
import json, os, sys, time
STATE = {state!r}
REPLIES = json.loads({replies!r})
AUTONOMOUS = json.loads({autonomous!r})
DENIED = json.loads({denied!r})
WRITES = json.loads({writes!r})
def log(name, value):
    with open(os.path.join(STATE, name), 'a') as f:
        f.write(json.dumps(value) + '\\n')
log('argv.jsonl', sys.argv[1:])
log('pids.jsonl', os.getpid())
# Like Claude resuming a session it no longer has: an error result without an init event.
if {forget_sessions!r} and '--resume' in sys.argv:
    sys.stdin.readline()
    print(json.dumps({{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'num_turns': 0,
                      'total_cost_usd': 0, 'errors': ['No conversation found with session ID: '
                                                      + sys.argv[sys.argv.index('--resume') + 1]]}}), flush=True)
    sys.exit(0)
# Like Claude, read the appended system prompt's file when the process starts.
if '--append-system-prompt-file' in sys.argv:
    log('prompts.jsonl', open(sys.argv[sys.argv.index('--append-system-prompt-file') + 1], encoding='utf-8').read())
counter = os.path.join(STATE, 'turns')
# Like Claude, a process that resumes a session carries on from the session's running total.
session_cost = os.path.join(STATE, 'session_cost')
cost = float(open(session_cost).read()) if '--resume' in sys.argv and os.path.exists(session_cost) else 0.0
def emit(event):
    print(json.dumps(event), flush=True)
for line in sys.stdin:
    n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
    open(counter, 'w').write(str(n))
    message = json.loads(line)
    log('turns.jsonl', {{'pid': os.getpid(), 'text': message['message']['content']}})
    reply = REPLIES[n - 1] if n <= len(REPLIES) else REPLIES[-1]
    # Like Claude Code 2.1.285 run with --replay-user-messages: the message's lifecycle and its replay
    # carry its uuid, and so does the result that answers it.
    mid = message.get('uuid') or 'claude-named-%d' % n
    for state in ('queued', 'started'):
        emit({{'type': 'command_lifecycle', 'command_uuid': mid, 'state': state, 'session_id': 'sess-p'}})
    emit({{'type': 'system', 'subtype': 'init', 'session_id': 'sess-p', 'model': 'fake'}})
    emit({{'type': 'user', 'message': {{'role': 'user', 'content': message['message']['content']}},
          'session_id': 'sess-p', 'parent_tool_use_id': None, 'uuid': mid, 'isReplay': True}})
    if reply == '<exit>':
        sys.exit(3)
    if reply == '<hang>':
        time.sleep(60)
    answers = {{'session_id': 'sess-p', 'user_message_uuid': mid, 'user_message_uuids': [mid]}}
    if reply == '<error>':
        emit({{'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'total_cost_usd': cost,
              **answers}})
    elif reply == '<budget>':
        # Like Claude Code 2.1.285 run with --max-budget-usd: the request is made and paid, then the turn
        # ends at the cap without a reply.
        cost += 0.25
        open(session_cost, 'w').write(str(cost))
        emit({{'type': 'result', 'subtype': 'error_max_budget_usd', 'is_error': True, 'total_cost_usd': cost,
              'errors': ['Reached maximum budget ($0.1)'], **answers}})
    else:
        cost += 0.25
        open(session_cost, 'w').write(str(cost))
        for name, content in WRITES.get(str(n), {{}}).items():  # a turn that changes files all the same
            open(os.path.join(os.getcwd(), name), 'w').write(content)
        emit({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': reply}}]}},
              'parent_tool_use_id': None}})
        emit({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': reply, 'total_cost_usd': cost,
              **answers, **({{'permission_denials': DENIED[str(n)]}} if str(n) in DENIED else {{}})}})
    emit({{'type': 'command_lifecycle', 'command_uuid': mid, 'state': 'completed', 'session_id': 'sess-p'}})
    if str(n) in AUTONOMOUS:
        # Like Claude once background work started in this turn ends: a turn of its own, answering no message.
        cost += 0.25
        open(session_cost, 'w').write(str(cost))
        emit({{'type': 'system', 'subtype': 'task_notification', 'task_id': 'bg-%d' % n, 'status': 'completed',
              'session_id': 'sess-p'}})
        emit({{'type': 'system', 'subtype': 'init', 'session_id': 'sess-p', 'model': 'fake'}})
        emit({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': AUTONOMOUS[str(n)]}}]}},
              'parent_tool_use_id': None}})
        emit({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': AUTONOMOUS[str(n)],
              'session_id': 'sess-p', 'total_cost_usd': cost,
              'origin': {{'kind': 'task-notification', 'producer': 'session-task'}}}})
if {linger!r}:
    time.sleep(60)
"""


class FakePlannerClaude:
    """A stand-in resident ``claude`` for a task's Planner.

    It reads stream-json user turns like the real CLI and answers turn N with
    ``replies[N-1]`` (the last reply repeats), counting turns across restarts.
    Like Claude, each result's ``total_cost_usd`` is its session's running
    total (0.25 per answered turn), which a process started with ``--resume``
    carries on from. A reply of ``<exit>`` exits mid-turn
    without a result, ``<hang>`` never answers, ``<error>`` ends the turn
    with an error result and ``<budget>`` ends it at its ``--max-budget-usd`` cap
    (0.25 spent, no reply). With ``linger`` it keeps running after its stdin
    closes; with ``forget_sessions`` it answers any ``--resume`` the way Claude
    answers one for a session it no longer has: an error result, no init. It logs its argv, pid, the system prompt its file held at start
    and every turn it reads. Like Claude Code 2.1.285, each turn's lifecycle
    events, the replay of its message and its result carry the message's uuid.
    After turn N, ``autonomous[N]`` is the reply of a turn it runs on its own, as
    Claude does once background work ends (0.25 more). Turn N's result reports
    ``denied[N]`` as its ``permission_denials``, the tool calls Claude Code refused,
    and turn N writes ``writes[N]`` (file name to content) into its working directory.
    """

    def __init__(
        self, state: Path, replies: tuple[str, ...], *, linger: bool = False, forget_sessions: bool = False,
        autonomous: dict[int, str] | None = None, denied: dict[int, list[dict[str, Any]]] | None = None,
        writes: dict[int, dict[str, str]] | None = None,
    ) -> None:
        self.state = state
        self.bin = fake_claude(state / "fake-claude", _PLANNER_CLAUDE.format(
            state=str(state), replies=json.dumps(replies), linger=linger, forget_sessions=forget_sessions,
            autonomous=json.dumps({str(turn): text for turn, text in (autonomous or {}).items()}),
            denied=json.dumps({str(turn): calls for turn, calls in (denied or {}).items()}),
            writes=json.dumps({str(turn): files for turn, files in (writes or {}).items()}),
        ))

    def _log(self, name: str) -> list[Any]:
        path = self.state / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def argvs(self) -> list[list[str]]:
        return self._log("argv.jsonl")

    def pids(self) -> list[int]:
        return self._log("pids.jsonl")

    def turns(self) -> list[dict[str, Any]]:
        return self._log("turns.jsonl")

    def prompts(self) -> list[str]:
        """The appended system prompt each started process read from its file."""
        return self._log("prompts.jsonl")


@pytest.fixture
def planner_claude(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., FakePlannerClaude]:
    """Build a ``FakePlannerClaude`` answering with the given replies."""

    def make(
        *replies: str, linger: bool = False, forget_sessions: bool = False, autonomous: dict[int, str] | None = None,
        denied: dict[int, list[dict[str, Any]]] | None = None, writes: dict[int, dict[str, str]] | None = None,
    ) -> FakePlannerClaude:
        return FakePlannerClaude(
            tmp_path_factory.mktemp("planner-claude"), replies, linger=linger, forget_sessions=forget_sessions,
            autonomous=autonomous, denied=denied, writes=writes,
        )

    return make


@pytest.fixture
def agent_db(monkeypatch: pytest.MonkeyPatch, stub_claude_on_path: Path) -> SimpleNamespace:
    """Record the agent tiers' database writes instead of needing PostgreSQL.

    The agent system treats PostgreSQL as a hard dependency, so message-flow
    tests would otherwise stop at the first unguarded write. Each write becomes
    an ``AsyncMock`` returning ``None`` (``create_project`` returns the new
    project's row), and each read answers as an empty database. The plans and
    sessions the agents save, the tasks created ready and the step workspaces
    are kept in memory (``store``, a ``_PlanStore``) and read back from there:
    ``save_plan``, ``load_plan``, ``save_session``, ``get_session``,
    ``create_task``, ``start_ready_task`` and the ``*_workspace*`` ones are
    mocks around it. The returned namespace exposes them by
    name so a test can assert what was persisted. A bare ``claude`` is a stub
    that never answers (``stub_claude_on_path``).
    """
    import importlib
    from unittest.mock import AsyncMock

    from looper.agents.tiers import planner as planner_module

    db = SimpleNamespace()
    for module_name, function in _AGENT_DB_WRITES:
        if function == "create_project":
            mock = AsyncMock(side_effect=_created_project)
        else:
            # start_record hands back the new record's id; the other writes return nothing.
            mock = AsyncMock(return_value=1 if function == "start_record" else None)
        monkeypatch.setattr(importlib.import_module(f"looper.db.{module_name}"), function, mock)
        setattr(db, function, mock)
    for module_name, function, value in _AGENT_DB_READS:
        mock = AsyncMock(return_value=value)
        monkeypatch.setattr(importlib.import_module(f"looper.db.{module_name}"), function, mock)
        setattr(db, function, mock)
    from looper.db import agent_sessions, journal as db_journal, plans, step_workspaces, task_changes, tasks
    from looper.db import user_gates

    db.store = store = _PlanStore()
    for module, name, function, method in (
        (plans, "save_plan", "save", store.save), (plans, "load_plan", "load", store.load),
        (agent_sessions, "save_session", "save_session", store.save_session),
        (agent_sessions, "get_session", "get_session", store.get_session),
        (tasks, "create_task", "create_task", store.create_task),
        (tasks, "start_ready_task", "start_ready_task", store.start_ready_task),
        (step_workspaces, "next_workspace_id", "next_id", store.next_workspace_id),
        (step_workspaces, "save_workspace", "save", store.save_workspace),
        (step_workspaces, "list_workspaces", "list_for_task", store.list_workspaces),
        (step_workspaces, "list_kept_workspaces", "list_kept", store.list_kept_workspaces),
        (task_changes, "add_supplement", "add_supplement", store.add_supplement),
        (task_changes, "pending_changes", "pending", store.pending),
        (task_changes, "mark_delivered", "mark_delivered", store.mark_delivered),
        (db_journal, "list_entries", "list_entries", store.list_entries),
        (user_gates, "list_gates", "list_for_task", store.list_gates),
        (user_gates, "get_gate", "get", store.get_gate),
        (user_gates, "pending_gate_messages", "pending", store.pending_gate_messages),
        (user_gates, "gate_thread", "thread", store.gate_thread),
        (user_gates, "add_gate_message", "add_user_message", store.add_gate_message),
    ):
        mock = AsyncMock(side_effect=method)
        monkeypatch.setattr(module, function, mock)
        setattr(db, name, mock)
    # The Planner imports this one by name rather than through its module.
    monkeypatch.setattr(planner_module, "update_task_phase", db.update_task_phase)
    return db


class _Installed:
    """The catalog items the stubbed database lists as installed (``list_installed``), with their files.

    ``global_rows`` are installed globally for every role; ``project_rows``
    maps a project to the skills written in it and installed there for the
    Planner, Workers and Reviewers.
    """

    TASK_ROLES = ("planner", "worker", "reviewer")

    def __init__(self, agent_db: SimpleNamespace) -> None:
        self.global_rows: list[dict[str, Any]] = []
        self.project_rows: dict[str, list[dict[str, Any]]] = {}
        self.files: dict[str, list[dict[str, Any]]] = {}
        agent_db.list_installed.side_effect = self.list_installed
        agent_db.get_item_files.side_effect = lambda item_id, **_: [dict(f) for f in self.files[item_id]]

    def item(self, item_id: str, kind: str, name: str, origin: str, owner: str | None,
             contents: dict[str, str]) -> dict[str, Any]:
        import hashlib

        from looper.agents.library import item_sha256

        files = [{"path": path, "content": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                  "executable": False} for path, text in sorted(contents.items())]
        self.files[item_id] = files
        return {"id": item_id, "kind": kind, "name": name, "origin": origin, "owner_project_id": owner,
                "sha256": item_sha256(files)}

    async def list_installed(self, role: str, project_id: str | None, **_: Any) -> list[dict[str, Any]]:
        rows = [{**row, "scope": "global"} for row in self.global_rows]
        if project_id and role in self.TASK_ROLES:
            rows += [{**row, "scope": "project"} for row in self.project_rows.get(project_id, [])]
        return sorted(rows, key=lambda row: (row["kind"], row["name"]))


@pytest.fixture
def _installed(agent_db: SimpleNamespace) -> _Installed:
    return _Installed(agent_db)


@pytest.fixture
def enabled_library(_installed: _Installed) -> Callable[..., list[dict[str, Any]]]:
    """Install catalog items globally for every role: ``enabled_library((kind, name, {path: content}), ...)``.

    The stubbed database then lists those items as installed (each with the
    version hash of its files) and hands out their files; it returns the item
    rows. Calling it again replaces the global installs.
    """
    import hashlib

    def set_items(*items: tuple[str, str, dict[str, str]]) -> list[dict[str, Any]]:
        _installed.global_rows = [
            _installed.item("lib-" + hashlib.sha256(f"{kind}:{name}".encode()).hexdigest()[:12], kind, name,
                            "machine", None, contents)
            for kind, name, contents in items
        ]
        return [dict(row) for row in _installed.global_rows]

    return set_items


@pytest.fixture
def project_skills(_installed: _Installed) -> Callable[..., list[dict[str, Any]]]:
    """Write skills in projects, each installed in its project for the task roles:
    ``project_skills((project_id, name, description, content), ...)``.

    Each is a catalog item of origin ``project`` whose one file is the
    ``SKILL.md`` the API writes (``project_skills.skill_file``); it returns
    every row. Calling it again replaces the skills.
    """
    from looper.agents.project_skills import skill_file

    def set_skills(*skills: tuple[str, str, str, str]) -> list[dict[str, Any]]:
        _installed.project_rows = {}
        rows = []
        for number, (project_id, name, description, content) in enumerate(skills, start=1):
            row = _installed.item(f"skill-{number:012d}", "skill", name, "project", project_id,
                                  {"SKILL.md": skill_file(name, description, content)})
            _installed.project_rows.setdefault(project_id, []).append(row)
            rows.append(row)
        return [dict(row) for row in rows]

    return set_skills


@pytest.fixture
def project_documents(agent_db: SimpleNamespace) -> Callable[..., list[dict[str, Any]]]:
    """Set the documents of the task's project: ``project_documents((kind, title, summary, source), ...)``.

    ``kind`` is ``text``, ``upload`` or ``path``; ``source`` is the content of a
    text or upload document (an upload is named ``<title>.txt``) and the
    relative path of a path document. The stubbed database then lists those
    rows for every project and hands out their content; it returns the rows.
    Calling it again replaces the documents.
    """
    import hashlib
    from datetime import datetime, timezone

    from looper.agents.knowledge import safe_filename, text_filename

    contents: dict[str, str] = {}

    def set_documents(*documents: tuple[str, str, str, str]) -> list[dict[str, Any]]:
        rows = []
        contents.clear()
        for number, (kind, title, summary, source) in enumerate(documents, start=1):
            document_id = f"doc-{number:012d}"
            stored = kind != "path"
            data = source.encode("utf-8") if stored else b""
            if stored:
                contents[document_id] = source
            rows.append({
                "id": document_id, "project_id": "proj-1", "title": title, "summary": summary, "source_kind": kind,
                "filename": (text_filename(title) if kind == "text" else safe_filename(f"{title}.txt", default="d.txt"))
                if stored else "",
                "path": None if stored else source, "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "created_at": datetime(2026, 10, 13, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 10, 13, tzinfo=timezone.utc),
            })
        agent_db.list_documents.return_value = rows
        return [dict(row) for row in rows]

    agent_db.get_content.side_effect = lambda document_id, **_: contents.get(document_id)
    return set_documents
