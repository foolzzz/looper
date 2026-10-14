from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUNTIME_ROOT_GUARD_DIR = Path(__file__).resolve().parent / "runtime_root_guard"
sys.path.insert(0, str(_RUNTIME_ROOT_GUARD_DIR))

# Importing the plugin starts the real-runtime-root guard before any test runs;
# re-exporting its fixture and hooks registers them for this suite.
from real_runtime_root_guard_plugin import (  # noqa: E402,F401
    _refuse_real_loopx_runtime_root,
    pytest_sessionfinish,
    pytest_terminal_summary,
    pytest_testnodedown,
)
from loopx.canary.runner import SMOKE_SUITE_CHOICES  # noqa: E402


def pytest_addoption(parser) -> None:
    group = parser.getgroup("loopx-smoke-suite")
    group.addoption(
        "--loopx-smoke-suite",
        "--smoke-suite",
        dest="loopx_smoke_suite",
        choices=sorted(SMOKE_SUITE_CHOICES),
        default=None,
        help=(
            "Opt in to the canary smoke-suite facade and pass this selector "
            "through to the LoopX runner."
        ),
    )
    group.addoption(
        "--loopx-smoke-module",
        "--smoke-module",
        action="append",
        default=[],
        dest="loopx_smoke_modules",
        help="Module token filter passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-exclude-module",
        "--smoke-exclude-module",
        action="append",
        default=[],
        dest="loopx_smoke_exclude_modules",
        help="Module token exclusion passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-script",
        "--smoke-script",
        action="append",
        default=[],
        dest="loopx_smoke_scripts",
        help="examples/**/*-smoke.py selector passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-profile",
        "--smoke-profile",
        action="append",
        default=[],
        dest="loopx_smoke_profiles",
        help=(
            "Smoke-suite or catalog profile selector passed through to the LoopX "
            "runner. Repeat or comma-separate."
        ),
    )
    group.addoption(
        "--loopx-smoke-family",
        "--smoke-family",
        action="append",
        default=[],
        dest="loopx_smoke_families",
        help="Catalog family selector passed through to the LoopX runner. Repeat or comma-separate.",
    )
    group.addoption(
        "--loopx-smoke-include-deep-checks",
        "--smoke-include-deep-checks",
        action="store_true",
        default=False,
        dest="loopx_smoke_include_deep_checks",
        help="Include deep catalog checks when profile or family selectors are used.",
    )
    group.addoption(
        "--loopx-smoke-limit",
        "--smoke-limit",
        type=int,
        default=0,
        dest="loopx_smoke_limit",
        help="Maximum selected checks to run. Defaults to all selected checks.",
    )
    group.addoption(
        "--loopx-smoke-offset",
        "--smoke-offset",
        type=int,
        default=0,
        dest="loopx_smoke_offset",
        help="Skip this many matched checks before applying the smoke-suite limit.",
    )
    group.addoption(
        "--loopx-smoke-timeout",
        "--smoke-timeout",
        type=float,
        default=120.0,
        dest="loopx_smoke_timeout",
        help="Per-check timeout in seconds for each subprocess smoke.",
    )


@pytest.fixture(autouse=True)
def _outside_agent_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run tests as the owner even when the suite itself runs inside an agent Turn.

    A LoopX Turn host marks its model process with ``LOOPX_AGENT_TURN``, and
    user-gate decisions refuse it. Tests that exercise that guard set it.
    """

    monkeypatch.delenv("LOOPX_AGENT_TURN", raising=False)


_GIT_ENV_OVERRIDES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


# PostgreSQL writes made by the agent tiers (Manager, Planner, TaskRouter, WorkerPool).
_AGENT_DB_WRITES = (
    ("projects", "create_project"),
    ("tasks", "create_task"),
    ("tasks", "update_task_status"),
    ("tasks", "update_task_phase"),
    ("steps", "upsert_steps"),
    ("steps", "update_step_status"),
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
    ("library", "list_enabled_items", []),
    ("library", "get_item_files", []),
    ("project_documents", "list_documents", []),
    ("project_documents", "get_content", None),
)


async def _created_project(
    *, name: str, description: str = "", work_dir: str = "", background: str = "", **_: object,
) -> dict:
    return {"id": "proj-000000000000", "name": name, "description": description, "work_dir": work_dir,
            "background": background}


@pytest.fixture
def stub_claude_on_path(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make a bare ``claude`` resolve to a stub that answers nothing.

    Each task's Planner starts ``claude`` as soon as it gets its task, so a
    message-flow test that routes a task without a stand-in binary would
    otherwise reach the real CLI (and spend money). The stub waits for its
    stdin to close and exits without output.
    """
    bin_dir = tmp_path_factory.mktemp("stub-claude")
    stub = bin_dir / "claude"
    stub.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return stub


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
    without a result, ``<hang>`` never answers, and ``<error>`` ends the turn
    with an error result. With ``linger`` it keeps running after its stdin
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
        self.bin = str(state / "fake-claude")
        script = Path(self.bin)
        script.write_text(f"#!{sys.executable}\n" + _PLANNER_CLAUDE.format(
            state=str(state), replies=json.dumps(replies), linger=linger, forget_sessions=forget_sessions,
            autonomous=json.dumps({str(turn): text for turn, text in (autonomous or {}).items()}),
        ))
        script.chmod(0o755)

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
    project's row), and each read answers as an empty database. The returned
    namespace exposes them by name so a test can assert what was persisted.
    A bare ``claude`` is a stub that never answers (``stub_claude_on_path``).
    """
    import importlib
    from unittest.mock import AsyncMock

    from loopx.agents.tiers import planner as planner_module

    db = SimpleNamespace()
    for module_name, function in _AGENT_DB_WRITES:
        if function == "create_project":
            mock = AsyncMock(side_effect=_created_project)
        else:
            # start_record hands back the new record's id; the other writes return nothing.
            mock = AsyncMock(return_value=1 if function == "start_record" else None)
        monkeypatch.setattr(importlib.import_module(f"loopx.db.{module_name}"), function, mock)
        setattr(db, function, mock)
    for module_name, function, value in _AGENT_DB_READS:
        mock = AsyncMock(return_value=value)
        monkeypatch.setattr(importlib.import_module(f"loopx.db.{module_name}"), function, mock)
        setattr(db, function, mock)
    # The Planner imports this one by name rather than through its module.
    monkeypatch.setattr(planner_module, "update_task_phase", db.update_task_phase)
    return db


@pytest.fixture
def enabled_library(agent_db: SimpleNamespace) -> Callable[..., list[dict[str, Any]]]:
    """Set the library items the agents load: ``enabled_library((kind, name, {path: content}), ...)``.

    The stubbed database then lists those items as the enabled ones (each
    with the version hash of its files) and hands out their files; it
    returns the item rows. Calling it again replaces the enabled set.
    """
    import hashlib

    from loopx.agents.library import item_sha256

    files_by_id: dict[str, list[dict[str, Any]]] = {}

    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def set_items(*items: tuple[str, str, dict[str, str]]) -> list[dict[str, Any]]:
        rows = []
        for kind, name, contents in items:
            files = [{"path": path, "content": text, "sha256": _sha(text), "executable": False}
                     for path, text in sorted(contents.items())]
            item_id = "lib-" + _sha(f"{kind}:{name}")[:12]
            files_by_id[item_id] = files
            rows.append({"id": item_id, "kind": kind, "name": name, "sha256": item_sha256(files)})
        agent_db.list_enabled_items.return_value = rows
        return [dict(row) for row in rows]

    agent_db.get_item_files.side_effect = lambda item_id, **_: [dict(f) for f in files_by_id[item_id]]
    return set_items


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

    from loopx.agents.knowledge import safe_filename, text_filename

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


@pytest.fixture
def independent_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A linked git worktree of a private temporary repository.

    A multi-agent goal only settles accountable delivery that was refreshed
    from an independent git worktree. Without an explicit delivery workspace,
    refresh-state and ``turn run-once`` capture the process cwd, so a test that
    relies on the default would pass from a linked worktree of the LoopX
    checkout and fail from the primary checkout. Tests use this worktree as
    their delivery workspace instead: pass it with --delivery-workspace-path,
    or ``monkeypatch.chdir`` into it the way the dispatcher starts a Turn in
    its workspace. Inherited ``GIT_*`` repository overrides (for example from
    a git hook) are cleared so git discovery only sees the temporary paths.
    """

    for name in _GIT_ENV_OVERRIDES:
        monkeypatch.delenv(name, raising=False)
    origin = tmp_path / "delivery-origin"
    worktree = tmp_path / "delivery-worktree"
    git = [
        "git",
        "-c", "user.name=LoopX Test",
        "-c", "user.email=loopx-test@example.invalid",
        "-c", "commit.gpgsign=false",
        "-c", "init.defaultBranch=main",
    ]
    subprocess.run([*git, "init", "--quiet", str(origin)], check=True)
    subprocess.run(
        [*git, "-C", str(origin), "commit", "--quiet", "--allow-empty", "-m", "init"],
        check=True,
    )
    subprocess.run(
        [*git, "-C", str(origin), "worktree", "add", "--quiet", "--detach", str(worktree)],
        check=True,
    )
    return worktree
