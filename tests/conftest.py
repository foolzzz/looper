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


# PostgreSQL writes made by the agent tiers (Manager, Planner, TaskRouter, WorkerPool).
_AGENT_DB_WRITES = (
    ("projects", "create_project"),
    ("tasks", "create_task"),
    ("tasks", "update_task_status"),
    ("tasks", "update_task_phase"),
    ("activity", "insert_activity"),
    ("llm_records", "start_record"),
    ("llm_records", "append_events"),
    ("llm_records", "finish_record"),
    ("llm_records", "touch_record"),
    ("llm_records", "update_event"),
)

# Reads the agents make, answered as a database that holds no projects and no settings yet.
_AGENT_DB_READS = (
    ("projects", "list_projects", []),
    ("projects", "find_project_by_name", None),
    ("projects", "get_project", None),
    ("projects", "get_task_project", None),
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
    sets its task there.
    """

    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, dict[str, dict[str, Any]]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.journal: dict[str, list[dict[str, Any]]] = {}

    async def save(self, task_id: str, *, plan: Any = None, steps: Any = (), journal: Any = (), **_: object) -> None:
        if plan is not None:
            self.plans[task_id] = json.loads(json.dumps(dict(plan)))
        for step in steps:
            self.steps.setdefault(task_id, {})[step["id"]] = json.loads(json.dumps(dict(step)))
        for entry in journal:
            self.journal.setdefault(task_id, []).append(json.loads(json.dumps(dict(entry))))

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
        return {"plan": plan, "steps": rows, "journal": journal}

    async def save_session(self, agent_id: str, session_id: str | None, totals: Any, *,
                           runtime_type: str | None = None, session_agent_id: str | None = None,
                           clean_start: bool = False, **_: object) -> None:
        self.sessions[agent_id] = {"session_id": session_id, "session_totals": dict(totals),
                                   "runtime_type": runtime_type, "session_agent_id": session_agent_id,
                                   "clean_start": clean_start}

    async def get_session(self, agent_id: str, **_: object) -> dict[str, Any] | None:
        saved = self.sessions.get(agent_id)
        return None if saved is None else {**saved, "session_totals": dict(saved["session_totals"])}


async def _created_project(
    *, name: str, description: str = "", work_dir: str = "", background: str = "", **_: object,
) -> dict:
    return {"id": "proj-000000000000", "name": name, "description": description, "work_dir": work_dir,
            "background": background}


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
        emit({{'type': 'assistant', 'message': {{'content': [{{'type': 'text', 'text': reply}}]}},
              'parent_tool_use_id': None}})
        emit({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': reply, 'total_cost_usd': cost,
              **answers}})
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
    Claude does once background work ends (0.25 more).
    """

    def __init__(
        self, state: Path, replies: tuple[str, ...], *, linger: bool = False, forget_sessions: bool = False,
        autonomous: dict[int, str] | None = None,
    ) -> None:
        self.state = state
        self.bin = fake_claude(state / "fake-claude", _PLANNER_CLAUDE.format(
            state=str(state), replies=json.dumps(replies), linger=linger, forget_sessions=forget_sessions,
            autonomous=json.dumps({str(turn): text for turn, text in (autonomous or {}).items()}),
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
    ) -> FakePlannerClaude:
        return FakePlannerClaude(
            tmp_path_factory.mktemp("planner-claude"), replies, linger=linger, forget_sessions=forget_sessions,
            autonomous=autonomous,
        )

    return make


@pytest.fixture
def agent_db(monkeypatch: pytest.MonkeyPatch, stub_claude_on_path: Path) -> SimpleNamespace:
    """Record the agent tiers' database writes instead of needing PostgreSQL.

    The agent system treats PostgreSQL as a hard dependency, so message-flow
    tests would otherwise stop at the first unguarded write. Each write becomes
    an ``AsyncMock`` returning ``None`` (``create_project`` returns the new
    project's row), and each read answers as an empty database. The plans and
    sessions the agents save are kept in memory (``store``, a ``_PlanStore``)
    and read back from there: ``save_plan``, ``load_plan``, ``save_session`` and
    ``get_session`` are mocks around it. The returned namespace exposes them by
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
    from looper.db import agent_sessions, plans

    db.store = store = _PlanStore()
    for module, name, function, method in (
        (plans, "save_plan", "save", store.save), (plans, "load_plan", "load", store.load),
        (agent_sessions, "save_session", "save_session", store.save_session),
        (agent_sessions, "get_session", "get_session", store.get_session),
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
