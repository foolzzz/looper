"""Run the task set through a disposable ``looper serve`` and read the results back.

Each run gets a new PostgreSQL database (created on the given server and
dropped afterwards), a free port, and a temporary runtime root and working
directories. The server is the same entry point ``looper serve`` runs
(``looper.agents.serve.serve_agent``); ``claude`` on its PATH is either the
scripted stand-in (fake mode) or a wrapper that adds ``--max-budget-usd`` to
every real Claude Code run (real mode). Tasks go through the HTTP API one at
a time, each ``reps`` times (rep 1 of every task, then rep 2, ...), each run in
a working directory and a project of its own; the run stops at the total
spending cap.

Every row has an outcome: ``pass``, ``fail`` (the model's work was wrong) or
``error`` (the run could not be scored: a timeout, the spending cap, or the
harness itself), so a slow or broken run is never counted as the model's
failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import metrics
from . import tasks as task_list
from .tasks import CHOSEN_PROFILE, DEFAULT_GATE_SCRIPT, END_DISCUSSION, EvalTask

REPO_ROOT = Path(__file__).resolve().parents[2]
FAKE_CLAUDE = Path(__file__).resolve().with_name("fake_claude.py")
BASELINES_DIR = Path(__file__).resolve().with_name("baselines")
DEFAULT_OUT_DIR = REPO_ROOT / "evals" / "runs"
RESERVED_PORT = 8767  # the operator's own server
RESERVED_DATABASE = "loopx"  # the operator's own database
# v2: every row has rep and outcome (and error_class and error when it is an error); the run has reps and
# aborted. A v1 row is rep 1, its outcome read from success, timed_out and stopped_by_cap (``compare.outcome``).
SCHEMA = "looper-prompt-eval/v2"
ERROR_TEXT_LIMIT = 300  # characters of an error row's message (and of the reason a run was aborted)

# The server, started the way ``looper serve`` starts it, without depending on the CLI module.
_SERVE = (
    "import sys\n"
    "from pathlib import Path\n"
    "from looper.agents.serve import serve_agent\n"
    "serve_agent(host='127.0.0.1', port=int(sys.argv[1]), runtime_root=Path(sys.argv[2]), work_dir=Path(sys.argv[3]))\n"
)


class EvalError(RuntimeError):
    """The eval could not run (no database, the server did not start, an API call failed)."""


class TaskError(EvalError):
    """A task the harness could not follow to its end; it was cancelled, and it had cost ``cost_usd``."""

    def __init__(self, message: str, *, cost_usd: float) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"  # the run ended and its result or flow is wrong: the model's failure
    ERROR = "error"  # the run could not be scored (ErrorClass): neither a pass nor a fail


class ErrorClass(StrEnum):
    TIMEOUT = "timeout"  # cancelled after the task's timeout, or no reply within the dialog's
    SPENDING_CAP = "spending_cap"  # cancelled at the run's spending cap
    HARNESS = "harness"  # the eval itself failed: an API call, the server, a check that raised


def error_row(error_class: ErrorClass, message: str, **values: Any) -> dict[str, Any]:
    """The fields of a row that is an error of ``error_class``."""
    return {"outcome": Outcome.ERROR.value, "success": False, "error_class": error_class.value, "error": message,
            **values}


def clip_errors(rows: list[dict[str, Any]]) -> None:
    """Shorten every row's error message to ERROR_TEXT_LIMIT; done after ``scrub``, so no local path is cut in
    two and left unreplaced."""
    for row in rows:
        if isinstance(row.get("error"), str):
            row["error"] = row["error"][:ERROR_TEXT_LIMIT]


# --- Database ------------------------------------------------------------------------------------------


def _database_url(url: str, database: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{database}"))


@contextmanager
def fresh_database(admin_url: str) -> Iterator[tuple[str, float]]:
    """A new database on ``admin_url``'s server, dropped afterwards: (its URL, database clock − local clock)."""
    import asyncpg

    admin_db = urllib.parse.urlsplit(admin_url).path.lstrip("/")
    if admin_db == RESERVED_DATABASE:
        raise EvalError(f"refusing to connect to the {RESERVED_DATABASE!r} database; give a URL to 'postgres' "
                        "or a disposable database on the same server")
    name = f"looper_eval_{uuid.uuid4().hex[:12]}"

    async def create() -> float:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
            before = time.time()
            db_now = await conn.fetchval("SELECT extract(epoch FROM clock_timestamp())::float8")
            return float(db_now) - (before + time.time()) / 2
        finally:
            await conn.close()

    async def drop() -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()

    try:
        offset = asyncio.run(create())
    except (OSError, asyncpg.PostgresError) as exc:
        raise EvalError(f"cannot create a database on the given server: {exc}") from None
    try:
        yield _database_url(admin_url, name), offset
    finally:
        asyncio.run(drop())


# --- Server --------------------------------------------------------------------------------------------


def free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port != RESERVED_PORT:
            return port


class Api:
    """JSON over the server's HTTP API."""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, **query: Any) -> Any:
        url = self.base_url + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise EvalError(f"{method} {path} answered {exc.code}: {detail}") from None

    def get(self, path: str, **query: Any) -> Any:
        return self.request("GET", path, **query)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body)


def install_claude(bin_dir: Path, *, mode: str, scenarios: Path | None = None,
                   max_budget_usd: float | None = None, real_claude: str | None = None) -> Path:
    """Put the ``claude`` the server runs in ``bin_dir``: the stand-in, or real Claude Code with a budget."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    if mode == "fake":
        assert scenarios is not None
        target = [sys.executable, str(FAKE_CLAUDE), str(scenarios)]
    else:
        if not real_claude:
            raise EvalError("real mode needs Claude Code: no 'claude' on PATH")
        target = [real_claude]
    extra = ["--max-budget-usd", f"{max_budget_usd:g}"] if mode == "real" and max_budget_usd else []
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(part) for part in target) + ' "$@"'
                      + "".join(f" {shlex.quote(part)}" for part in extra) + "\n")
    script.chmod(0o755)
    return script


@contextmanager
def running_server(*, database_url: str, runtime_root: Path, bin_dir: Path, log_path: Path,
                   home: Path | None = None, startup_timeout: float = 90.0) -> Iterator[Api]:
    """``looper serve`` on a free port; stopped with SIGINT, as the operator would."""
    port = free_port()
    manager_dir = runtime_root / "manager"
    manager_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "LOOPER_DATABASE_URL": database_url, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")]))}
    if home is not None:
        env["HOME"] = str(home)
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVE, str(port), str(runtime_root), str(manager_dir)],
        cwd=str(runtime_root), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    api = Api(f"http://127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + startup_timeout
        while True:
            if process.poll() is not None:
                raise EvalError(f"the server exited ({process.returncode}) before it answered; see {log_path.name}")
            try:
                api.get("/api/status")
                break
            except (OSError, EvalError):
                if time.monotonic() > deadline:
                    raise EvalError(f"the server did not answer within {startup_timeout:g} s") from None
                time.sleep(0.25)
        yield api
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.close()


# --- Working directories ------------------------------------------------------------------------------


def snapshot(work_dir: Path) -> dict[str, tuple[float, str]]:
    """Every file under ``work_dir`` but git's own: relative path → (mtime, sha256)."""
    files = {}
    for path in sorted(work_dir.rglob("*")):
        if path.is_file() and not path.is_symlink() and ".git" not in path.relative_to(work_dir).parts:
            files[path.relative_to(work_dir).as_posix()] = (
                path.stat().st_mtime, hashlib.sha256(path.read_bytes()).hexdigest())
    return files


def changed_files(before: dict[str, tuple[float, str]], after: dict[str, tuple[float, str]]) -> dict[str, float]:
    """Files new or changed since ``before``: relative path → mtime."""
    return {path: mtime for path, (mtime, digest) in after.items()
            if path not in before or before[path] != (mtime, digest)}


# --- One task -----------------------------------------------------------------------------------------


def _git_in(work_dir: Path, *args: str) -> str:
    """A git command in a task's working directory (no hooks, no signing), its output."""
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
                          cwd=work_dir, capture_output=True, text=True, check=True).stdout.strip()


def init_repository(work_dir: Path) -> None:
    """Make ``work_dir`` a git repository on main whose one commit holds what is there."""
    _git_in(work_dir, "init", "-q", "-b", "main")
    _git_in(work_dir, "config", "user.name", "Eval")
    _git_in(work_dir, "config", "user.email", "eval@localhost")
    _git_in(work_dir, "add", "-A")
    _git_in(work_dir, "commit", "-q", "--allow-empty", "-m", "eval: the task's files")


def looper_commits(work_dir: Path, task_id: str) -> dict[str, int]:
    """The commits the task's step workspaces added to the working directory's branch."""
    def count(*args: str) -> int:
        return int(_git_in(work_dir, "rev-list", "--count", "--first-parent", f"--grep=^Looper-Task: {task_id}$",
                           *args, "HEAD") or 0)

    return {"merges": count("--merges"), "checkpoints": count("--no-merges")}


def _all_activity(api: Api, task_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while True:
        page = api.get(f"/api/tasks/{task_id}/activity", since=rows[-1]["seq"] if rows else 0, limit=500)["activity"]
        rows += page
        if len(page) < 500:
            return rows


def _all_events(api: Api, record_id: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        page = api.get(f"/api/llm-records/{record_id}/events", after=events[-1]["seq"] if events else 0,
                       limit=5000)["events"]
        events += page
        if len(page) < 5000:
            return events


def run_task(
    api: Api,
    task: EvalTask,
    work_dir: Path,
    *,
    timeout: float,
    poll: float,
    cap_left: float | None = None,
    clock_offset: float = 0.0,
    settle: float = 15.0,
    step_workspaces: str | None = None,
    chosen_profile_id: str | None = None,
    user_gates: str | None = None,
) -> dict[str, Any]:
    """Create and start ``task`` in ``work_dir``, wait until it ends, and read back what it shows.

    The task is cancelled when its cost reaches ``cap_left`` or it runs longer
    than ``timeout`` seconds. With ``step_workspaces``, ``work_dir`` is a git
    repository and the task's project has that setting. A task whose Planner may
    choose a profile gets ``chosen_profile_id`` on its project's list first. A
    task that asks for gates has them on in its project, whatever ``user_gates``
    says; another task's project has ``user_gates`` (``on`` or ``off``) when
    given. While one of its gates waits for the user, the runner answers it from
    the task's script (``answer_gates``).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    for name, content in task.files.items():
        (work_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (work_dir / name).write_text(content, encoding="utf-8")
    project: dict[str, Any] = {"name": f"eval-{task.id}-{uuid.uuid4().hex[:6]}", "work_dir": str(work_dir)}
    if step_workspaces:
        init_repository(work_dir)
        project["step_workspaces"] = step_workspaces
    before = snapshot(work_dir)
    started = time.monotonic()
    created = api.post("/api/tasks/create", {"requirement": task.requirement, "project": project})
    task_id = created["task_id"]
    try:
        if task.planner_may_choose:
            if chosen_profile_id is None:
                raise EvalError(f"{task.id} needs the profile its Planner may choose")
            api.put(f"/api/projects/{created['project_id']}/allowed-profiles", {"profile_ids": [chosen_profile_id]})
        if task.roles_on:
            api.put(f"/api/projects/{created['project_id']}", {"planner_roles": "on"})
        for template in task.role_templates:
            fields = {k: v for k, v in template.items() if k != "key"}
            api.put(f"/api/projects/{created['project_id']}/role-templates/{template['key']}", fields)
        gates = "on" if task.user_gates else user_gates
        if gates is not None:
            api.put(f"/api/projects/{created['project_id']}", {"user_gates": gates})
        api.post("/api/tasks/start", {"task_id": task_id})
        answered: dict[str, int] = {}  # each gate's script entries posted so far

        chat_pending = task.chat is not None
        edit_pending = task.edit_requirement is not None
        stopped_by_cap = timed_out = False
        while True:
            detail = api.get(f"/api/tasks/{task_id}")
            status = detail.get("status")
            if status in metrics.TERMINAL_STATUSES:
                break
            cost = float((detail.get("cost") or {}).get("cost_usd") or 0.0)
            if cap_left is not None and cost >= cap_left and not stopped_by_cap:
                stopped_by_cap = True
                api.post("/api/tasks/cancel", {"task_id": task_id})
            elif time.monotonic() - started > timeout and not timed_out:
                timed_out = True
                api.post("/api/tasks/cancel", {"task_id": task_id})
            elif answer_gates(api, task, task_id, detail, answered, now=time.time() + clock_offset):
                pass  # the user answered a gate; look again at the next poll
            elif chat_pending and detail.get("steps") and detail.get("phase") in ("executing", "verifying"):
                try:
                    api.post("/api/chat/send", {"text": task.chat, "target": {"kind": "planner", "task_id": task_id}})
                    chat_pending = False
                except EvalError:
                    pass  # the Planner is not live yet; try again at the next poll
            elif edit_pending and detail.get("steps") and detail.get("phase") in ("executing", "verifying"):
                api.put(f"/api/tasks/{task_id}/requirement", {"requirement": task.edit_requirement})
                edit_pending = False
            time.sleep(poll)
        duration = time.monotonic() - started

        # The Planner's last turn and the step runs finish their records as the task ends.
        deadline = time.monotonic() + settle
        while True:
            records = api.get("/api/llm-records", task_id=task_id, limit=500)["records"]
            if all(r.get("status") != "running" for r in records) or time.monotonic() > deadline:
                break
            time.sleep(poll)
        detail = api.get(f"/api/tasks/{task_id}")
        records = sorted(records, key=lambda r: int(r["id"]))
        events = {int(r["id"]): _all_events(api, int(r["id"])) for r in records if metrics.role(r) != "other"}
        result = metrics.task_metrics(
            detail=detail,
            activity=_all_activity(api, task_id),
            records=records,
            events=events,
            changes=changed_files(before, snapshot(work_dir)),
            work_dir=work_dir,
            check_problem=task.check(work_dir),
            expect=task.expect,
            disciplines=task.disciplines,
            # The gates the runner ended because the Planner asked again after the script ran out.
            gate_asks_beyond_script=sum(1 for done in answered.values() if done > len(_script(task))),
            clock_offset=clock_offset,
            now=time.time() + clock_offset,
        )
        if step_workspaces:
            result["looper_commits"] = looper_commits(work_dir, task_id)
    except Exception as exc:
        raise TaskError(f"{type(exc).__name__}: {exc}", cost_usd=_cancel_after_error(api, task_id)) from exc
    if stopped_by_cap:
        result["problems"].append("cancelled at the spending cap")
    if timed_out:
        result["problems"].append(f"cancelled after {timeout:g} s")
    row = {"id": task.id, **result, "duration_s": round(duration, 1), "stopped_by_cap": stopped_by_cap,
           "timed_out": timed_out, "work_dir": str(work_dir)}
    # A task cancelled at the cap or after its timeout is not scored: what it delivered says nothing of the model.
    if stopped_by_cap:
        return {**row, **error_row(ErrorClass.SPENDING_CAP, "cancelled at the spending cap")}
    if timed_out:
        return {**row, **error_row(ErrorClass.TIMEOUT, f"cancelled after {timeout:g} s")}
    return {**row, "outcome": (Outcome.PASS if result["success"] else Outcome.FAIL).value}


def _script(task: EvalTask) -> tuple[str, ...]:
    """What the user answers in ``task``'s gates, in turn (``DEFAULT_GATE_SCRIPT`` without a script)."""
    return task.gate_script or DEFAULT_GATE_SCRIPT


def answer_gates(api: Api, task: EvalTask, task_id: str, detail: dict[str, Any], answered: dict[str, int], *,
                 now: float) -> bool:
    """Play the user: post the next entry of ``task``'s gate script (``_script``) in each of the task's gates that
    waits for the user, once it has waited ``gate_reply_after_s`` since it opened; ``END_DISCUSSION`` ends the
    discussion, and so does a gate that asks again after the script ran out (so the task ends instead of waiting
    for its timeout). Whether anything was posted."""
    script = _script(task)
    posted = False
    for gate in detail.get("gates") or []:
        label, done = gate.get("step"), answered.get(gate.get("step"), 0)
        if gate.get("waiting_on") != "user" or done > len(script) or (done == len(script) and
                                                                     script[-1] == END_DISCUSSION):
            continue
        opened = metrics.timestamp(gate.get("opened_at")) or now
        if now - opened < task.gate_reply_after_s:
            continue
        entry = script[done] if done < len(script) else END_DISCUSSION
        try:
            if entry == END_DISCUSSION:
                api.post(f"/api/tasks/{task_id}/gates/{label}/end", {})
            else:
                api.post(f"/api/tasks/{task_id}/gates/{label}/messages", {"text": entry})
        except EvalError:
            continue  # it stopped waiting meanwhile; look again at the next poll
        answered[label] = done + 1
        posted = True
    return posted


def _cancel_after_error(api: Api, task_id: str) -> float:
    """Cancel a task the harness could not follow (so it runs alongside nothing), and return its cost so far;
    best effort, as the server may be gone."""
    try:
        api.post("/api/tasks/cancel", {"task_id": task_id})
    except Exception:  # it may have ended, or the server may be gone
        pass
    try:
        return float((api.get(f"/api/tasks/{task_id}").get("cost") or {}).get("cost_usd") or 0.0)
    except Exception:
        return 0.0


# --- The task set -------------------------------------------------------------------------------------


# Named sets of tasks for ``--tasks``, resolved against the task set: an id it does not have (yet) is left out.
TASK_SETS: dict[str, tuple[str, ...]] = {
    # The tasks whose pass or fail turns on the Planner's judgment: the opus tier (evals/README.md).
    "planner": ("dependent_steps", "parallel_parts", "interface_first", "supplement_replan", "supplement_no_change",
                "requirement_edit", "roles_tester_docs", "roles_not_needed", "roles_multi_platform",
                "roles_template_fits", "roles_template_unfit", "gate_ambiguous",
                "gate_not_needed", "gate_parallel_branch", "gate_discoverable"),
}


def select_tasks(names: list[str] | None) -> tuple[EvalTask, ...]:
    """The tasks named, a set of TASK_SETS standing for those of its tasks the task set has (all tasks without
    names), in the task set's order; raises ``ValueError`` for an unknown id or a set none of whose tasks exist."""
    if not names:
        return task_list.select(None)
    known = {task.id for task in task_list.TASKS}
    ids: list[str] = []
    for name in names:
        if name not in TASK_SETS:
            ids.append(name)
            continue
        found = [task_id for task_id in TASK_SETS[name] if task_id in known]
        if not found:
            raise ValueError(f"the task set {name!r} has none of its tasks in the task set")
        ids += found
    return task_list.select(ids)


def run_suite(
    tasks: tuple[EvalTask, ...],
    run_one: Callable[[EvalTask, int, float | None], dict[str, Any]],
    *,
    total_cap_usd: float | None,
    reps: int = 1,
    results: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Run every task ``reps`` times, rep-major (rep 1 of every task, then rep 2, ...), so drift over time does not
    land on one task, until their summed cost reaches ``total_cap_usd``; the rest are skipped.

    ``run_one(task, rep, cap_left)`` runs one case with the budget left. A case that raises is an error row of
    class ``harness`` and the run goes on with the next. The rows go into ``results`` as they are made (a new list
    without), so a caller keeps what is done when the run stops midway. Returns the rows and whether the cap
    stopped the run.
    """
    results = [] if results is None else results
    spent = 0.0
    stopped = False
    for rep in range(1, reps + 1):
        for task in tasks:
            if stopped or (total_cap_usd is not None and spent >= total_cap_usd):
                stopped = True
                results.append({"id": task.id, "rep": rep, "skipped": "the spending cap was reached",
                                "cost_usd": 0.0})
                continue
            try:
                row = run_one(task, rep, None if total_cap_usd is None else total_cap_usd - spent)
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                cost = exc.cost_usd if isinstance(exc, TaskError) else 0.0
                message = str(exc) if isinstance(exc, TaskError) else f"{type(exc).__name__}: {exc}"
                row = error_row(ErrorClass.HARNESS, message, cost_usd=cost)
            spent += float(row.get("cost_usd") or 0.0)
            stopped = stopped or bool(row.get("stopped_by_cap"))
            results.append({"id": task.id, "rep": rep, **row})
    return results, stopped


def _git() -> dict[str, Any]:
    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        return done.stdout.strip() if done.returncode == 0 else ""

    return {"commit": git("rev-parse", "HEAD") or None,
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}


def scrub(value: Any, replacements: dict[str, str]) -> Any:
    """``value`` with every local path in ``replacements`` replaced, so a result can be shared."""
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [scrub(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, replacements) for k, v in value.items()}
    return value


def write_result(result: dict[str, Any], path: Path, *, roots: tuple[Path, ...], rows: str) -> dict[str, Any]:
    """``result`` with the local paths of ``roots`` and the home directory replaced (``scrub``) and the error texts
    of its ``rows`` and its ``aborted`` reason shortened, written to ``path``; returns it."""
    result = scrub(result, {**{str(root): "<eval-root>" for root in roots}, str(Path.home()): "~"})
    clip_errors(result[rows])
    if result.get("aborted"):
        result["aborted"] = result["aborted"][:ERROR_TEXT_LIMIT]
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def aborted_reason(exc: BaseException) -> str:
    """Why a run stopped midway, for its ``aborted`` field."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def run(
    *,
    mode: str,
    tasks: tuple[EvalTask, ...],
    admin_url: str,
    out_dir: Path,
    model: str | None = None,
    agent: str | None = None,
    provider: ProviderChoice | None = None,
    register: dict[str, Any] | None = None,
    max_budget_usd: float | None = None,
    total_cap_usd: float | None = None,
    task_timeout: float | None = None,
    keep_work_dirs: bool = False,
    step_workspaces: str | None = None,
    reps: int = 1,
    user_gates: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """Run ``tasks`` ``reps`` times each in ``mode`` (``fake`` or ``real``); returns the run and the JSON file
    written to ``out_dir``. Each (task, rep) runs in a working directory of its own, ``<root>/work/<task>/<rep>``.
    The JSON is written however the run ends: when it stops midway, with what is done and ``aborted`` saying why
    (an interrupt is raised again once it is written).

    Every role runs ``agent`` (by name), else Claude Code with ``provider`` (registered for the run from a preset),
    else the Claude Code agent of ``model`` (``claude-code/<model>``, registered for the run), else the built-in
    agent. ``register`` registers an agent first (``register_agent``), which every role then runs. With a
    provider or a registered agent, Looper prices the runs at its prices, so ``max_budget_usd`` is the per-run
    cap Looper keeps (the system layer's ``run_budget_usd``) instead of Claude Code's ``--max-budget-usd``.
    ``user_gates`` (``on`` or ``off``) is the setting of every task's project but a gate task's, which has them on.
    """
    if mode not in ("fake", "real"):
        raise EvalError(f"mode must be fake or real, not {mode!r}")
    if provider is not None and not provider.api_key:
        raise EvalError(f"a provider needs its API key in ${API_KEY_ENV}")
    if reps < 1:
        raise EvalError(f"reps must be at least 1, not {reps}")
    real_claude = shutil.which("claude") if mode == "real" else None
    git = _git()  # what is being evaluated, before anything runs
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"{stamp}-{mode}.server.log"
    raw_root = Path(tempfile.mkdtemp(prefix="looper-eval-"))
    root = raw_root.resolve()
    timeout = task_timeout or (60.0 if mode == "fake" else 900.0)
    poll = 0.3 if mode == "fake" else 2.0
    results: list[dict[str, Any]] = []
    stopped = False
    aborted: str | None = None
    chosen: dict[str, Any] = {}
    try:
        scenarios = root / "scenarios.json"
        work_dirs = {(task.id, rep): root / "work" / task.id / str(rep) for rep in range(1, reps + 1) for task in tasks}
        fake = {task.id: task.fake for task in tasks}
        scenarios.write_text(json.dumps({os.path.realpath(path): fake[task_id]
                                         for (task_id, _), path in work_dirs.items()}), encoding="utf-8")
        install_claude(root / "bin", mode=mode, scenarios=scenarios,
                       max_budget_usd=None if provider is not None or register is not None else max_budget_usd,
                       real_claude=real_claude)
        (root / "runtime").mkdir()
        home = root / "home" if mode == "fake" else None
        if home is not None:
            home.mkdir()
        with fresh_database(admin_url) as (database_url, clock_offset), running_server(
            database_url=database_url, runtime_root=root / "runtime", bin_dir=root / "bin", log_path=log_path,
            home=home,
        ) as api:
            if register is not None:
                agent = register_agent(api, register)["name"]
            chosen = select_agent(api, agent=agent, model=model, provider=provider, run_budget_usd=max_budget_usd)
            if register is not None:
                api.put("/api/agent-settings/system", {"profile_id": chosen["profile_id"],
                                                       "run_budget_usd": max_budget_usd})
            chosen_profile_id = None
            if any(task.planner_may_choose for task in tasks):
                chosen_profile_id = api.post("/api/agent-profiles", {
                    "name": CHOSEN_PROFILE, **{key: chosen[key] for key in ("runtime_id", "provider_id", "model",
                                                                             "effort")}})["profile"]["profile_id"]
            _, stopped = run_suite(
                tasks,
                lambda task, rep, cap_left: run_task(api, task, work_dirs[task.id, rep], timeout=timeout, poll=poll,
                                                     cap_left=cap_left, clock_offset=clock_offset,
                                                     step_workspaces=step_workspaces,
                                                     chosen_profile_id=chosen_profile_id, user_gates=user_gates),
                total_cap_usd=total_cap_usd,
                reps=reps,
                results=results,
            )
    except BaseException as exc:
        aborted = aborted_reason(exc)
        if not isinstance(exc, Exception):
            raise  # an interrupt: the JSON is still written below
    finally:
        if not keep_work_dirs:
            shutil.rmtree(root, ignore_errors=True)
        path = out_dir / f"{stamp}-{mode}.json"
        run_result = write_result({
            "schema": SCHEMA,
            "mode": mode,
            "model": chosen.get("model") if mode == "real" else None,
            "agent": chosen.get("name"),
            "provider_preset": provider.preset if provider is not None else None,
            "started_at": stamp,
            "git": git,
            "caps": {"max_budget_usd": max_budget_usd, "total_cap_usd": total_cap_usd},
            "step_workspaces": step_workspaces,
            "user_gates": user_gates,
            "reps": reps,
            "stopped_by_cap": stopped,
            "aborted": aborted,
            "total_cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in results), 6),
            "tasks": results,
        }, path, roots=(root, raw_root), rows="tasks")
        if aborted:
            print(f"the run stopped midway ({run_result['aborted']}); what is done is in {path}", file=sys.stderr)
    return run_result, path


def register_agent(api: Api, spec: dict[str, Any]) -> dict[str, Any]:
    """Register the agent of ``spec`` in the eval's database and return it: ``{"runtime": {name?, type},
    "provider": {name, protocols, auth, base_url?, models?, api_key_env?} or {provider_id}, "agent": {name,
    model, effort?}}``. Runtimes are built in: the agent runs on the runtime of that name, else the built-in one
    of that type (``codex`` or ``omp`` when on the server's PATH); a ``provider_id`` names a provider the
    server has, such as the built-in ``prov-codex-login`` (Codex's own login); the provider's API key is read
    from the environment variable ``api_key_env`` names, so no key is in the file or in the run's result."""
    registry = api.get("/api/registry")
    wanted = spec["runtime"]
    runtime = next((r for r in registry["runtimes"] if r["name"] == wanted.get("name")), None) or next(
        (r for r in registry["runtimes"] if r["builtin"] and r["type"] == wanted.get("type")), None)
    if runtime is None:
        raise EvalError(f"no {wanted.get('type')!r} runtime in the eval's server: put its CLI on PATH")
    if (provider_id := spec["provider"].get("provider_id")) is None:
        provider = {k: v for k, v in spec["provider"].items() if k != "api_key_env"}
        if (variable := spec["provider"].get("api_key_env")) is not None:
            if not os.environ.get(variable):
                raise EvalError(f"the provider's API key is read from ${variable}, which is not set")
            provider["api_key"] = os.environ[variable]
        provider_id = api.post("/api/providers", provider)["provider"]["provider_id"]
    elif not any(p["provider_id"] == provider_id for p in registry["providers"]):
        raise EvalError(f"no provider {provider_id!r} in the eval's server")
    return api.post("/api/agent-profiles", {**spec["agent"], "runtime_id": runtime["runtime_id"],
                                            "provider_id": provider_id})["profile"]


# The API key of a provider an eval registers (``--provider-preset``): read from the environment, never a file.
API_KEY_ENV = "LOOPER_EVAL_API_KEY"


@dataclass(frozen=True)
class ProviderChoice:
    """A provider an eval registers from a preset's Anthropic-format API, and the model its agent runs."""

    preset: str
    model: str | None = None  # None: the preset's first model
    base_url: str | None = None  # None: the preset's (another, such as a logging proxy in front of it)
    api_key: str | None = field(default=None, repr=False)


def register_provider_agent(api: Api, choice: ProviderChoice) -> dict[str, Any]:
    """Register ``choice``'s provider and a Claude Code agent of it; returns the agent."""
    registry = api.get("/api/registry")
    preset = next((p for p in registry["provider_presets"] if p["preset_id"] == choice.preset), None)
    if preset is None:
        raise EvalError(f"no provider preset {choice.preset!r}; the presets are "
                        + ", ".join(p["preset_id"] for p in registry["provider_presets"]))
    api_entry = next((a for a in preset["apis"] if "anthropic-messages" in a["protocols"]), None)
    if api_entry is None:
        raise EvalError(f"preset {choice.preset!r} has no Anthropic-format API for Claude Code")
    provider = api.post("/api/providers", {
        "name": preset["name"], "protocols": ["anthropic-messages"], "auth": "api_key", "api_key": choice.api_key,
        "base_url": choice.base_url or api_entry["base_url"], "models": preset["models"], "env": preset["env"],
    })["provider"]
    model = choice.model or preset["models"][0]["name"]
    answer = api.post("/api/agent-profiles", {"name": f"claude-code/{preset['name']}/{model}",
                                              "runtime_id": "rt-claude-code", "provider_id": provider["provider_id"],
                                              "model": model})
    for warning in answer.get("warnings") or []:
        print(f"warning: {warning}", file=sys.stderr)
    return answer["profile"]


def select_agent(api: Api, *, agent: str | None, model: str | None, provider: ProviderChoice | None = None,
                 run_budget_usd: float | None = None) -> dict[str, Any]:
    """Make ``agent`` (a registered agent's name), else Claude Code with ``provider``, else Claude Code with
    ``model``, every role's agent (the system layer); returns it. Without any, every role keeps the built-in
    agent. With ``provider``, the system layer also caps each run at ``run_budget_usd``."""
    registry = api.get("/api/registry")
    agents = {a["name"]: a for a in registry["profiles"]}
    if agent is not None:
        if agent not in agents:
            raise EvalError(f"no agent {agent!r} in the eval's database; it has {', '.join(sorted(agents))}")
        chosen = agents[agent]
    elif provider is not None:
        chosen = register_provider_agent(api, provider)
        api.put("/api/agent-settings/system", {"profile_id": chosen["profile_id"], "run_budget_usd": run_budget_usd})
        return chosen
    elif model:
        name = f"claude-code/{model}"
        chosen = agents.get(name) or api.post("/api/agent-profiles", {
            "name": name, "runtime_id": "rt-claude-code", "provider_id": "prov-claude-subscription",
            "model": model})["profile"]
    else:
        return next(a for a in registry["profiles"] if a["builtin"])
    api.put("/api/agent-settings/system", {"profile_id": chosen["profile_id"]})
    return chosen


def baseline_path(*, mode: str, model: str | None, dialogs: bool = False) -> Path:
    """The committed baseline of the task set's runs (or the dialogs') of ``mode`` and ``model``; for dialogs,
    ``model`` is the Manager's."""
    name = f"real-{model}.json" if mode == "real" else "fake.json"
    return BASELINES_DIR / (f"dialogs-{name}" if dialogs else name)


def compared_model(run: dict[str, Any]) -> str | None:
    """The model a run is compared by: the Manager's for dialogs (``manager_model``, else every role's)."""
    return run.get("manager_model", run.get("model")) if "dialogs" in run else run.get("model")


def previous_run(out_dir: Path, current: Path, *, mode: str, model: str | None, dialogs: bool = False) -> Path | None:
    """The newest earlier run in ``out_dir`` of the same kind (the task set's or the dialogs'), mode and model."""
    earlier = sorted(p for p in out_dir.glob(f"*-{mode}.json") if p.name < current.name)
    for path in reversed(earlier):
        data = json.loads(path.read_text(encoding="utf-8"))
        if ("dialogs" in data) == dialogs and compared_model(data) == model:
            return path
    return None


def comparison_base(choice: str | None, *, out_dir: Path, current: Path, mode: str, model: str | None,
                    dialogs: bool = False) -> tuple[dict[str, Any] | None, str]:
    """The run to compare a new one with, and where it is from: the committed baseline of its kind, mode and
    model (``choice`` None), the newest earlier run in ``out_dir`` (``"previous"``) or the file ``choice`` names.
    None when there is no such run."""
    if choice == "previous":
        path = previous_run(out_dir, current, mode=mode, model=model, dialogs=dialogs)
        if path is None:
            return None, f"no earlier {mode} run of {model or 'the built-in agent'} in {_shown(out_dir)}"
    elif choice is None:
        path = baseline_path(mode=mode, model=model, dialogs=dialogs)
        if not path.is_file():
            return None, f"no committed baseline {_shown(path)}"
    else:
        path = Path(choice)
    return json.loads(path.read_text(encoding="utf-8")), _shown(path)


def _shown(path: Path) -> str:
    """``path`` relative to the repository when it is in it, else its name: no local path is printed for a PR."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name
