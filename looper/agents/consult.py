"""Consults: a question for a seat no run is in, answered by its last run's session (agent room RFC, R1b).

A ``question`` in a task's room for a seat whose run has ended (``Presence.OFFLINE``) does not wait for a run that
may never come: Looper resumes a fork of the seat's last run's session, read only, gives it the questions and posts
its answers in the room as the seat (``via`` ``consult``, the Planner seeing them). When it cannot, the question goes
to the Planner with the reason (``room.FallbackReason``), as in R1a.

- **What it resumes.** The pinned, finished record of the seat's last run (its step's Worker, or Reviewer): its
  session id, its runtime and provider, its system prompt. Claude Code keeps every session itself; Codex and OMP only the
  sessions of runs in a room (``room.run_session_dir``); Grok in the home its recorded command names. The seat's Agent Profile must still run on that runtime and
  provider (a session belongs to both, as the Planner's does), else ``runtime_cannot``. A newer record of that run
  makes the pin unusable (``no_session``), so an active session is never copied.
- **A fork.** Claude Code ``--resume <id> --fork-session``; Codex and OMP a copy of the run's session directory;
  Grok a home of its own with a copy of that one session, resumed with ``--fork-session``. The run's own session
  stays as it was. Claude Code keeps the fork in its own session store, like every Claude Code session, and Looper
  leaves it there (it never changes anything in Claude Code's own directory); the other runtimes' forks are in the
  consult's ``fork_dir``, which is removed.
- **Read only.** The consult's system prompt is the run's, with ``CONSULT_CONTRACT`` in place of its contract.
  Claude Code has only Read, Glob and Grep, with Bash and writing tools refused (not verified on a real run yet);
  Codex has its read-only sandbox, Grok its reading tools. They run where the step ran. OMP has only its reading
  tools (``--tools read,glob,grep``, not verified on a real run) in a detached checkout of the task directory's
  branch, removed afterwards. The checkout keeps writes out of the task directory and ``wrote_files`` catches
  changes inside it. It is not a sandbox: OMP runs as the operator's user, so writes outside the checkout (another
  path or the home directory) are neither prevented nor detected.
- **Bounds.** At most ``room_consults_per_task`` a task, ``room_consult_budget_usd`` and
  ``room_consult_timeout_seconds`` each, 20 turns (Claude Code and Grok); a seat has one consult at a time, and
  questions that come while one is queued join it. One lock per task covers both its limit and its seats. A consult takes a place
  under its task's limit of the seat's kind (Worker or Reviewer), as any run does. Its spend is a run of its own
  (``consult-<12 hex>``), in the task's cost. After each run its recorded cost is checked, falling back to the
  result's own figure. A cost over its cap or still unknown drops the answers on every runtime.
  A provider-priced model without a full price cannot run a consult (``runtime_cannot``).
  One deadline covers writing the prompt, reading the reply and waiting for exit; stderr is drained with a short
  tail kept for errors, and the process tree is ended on success too.
- **Delivery.** Its end state, answers and unanswered questions for the Planner commit together, once. If its
  task's room was turned off, it ends ``failed`` (``room_off``), without messages or notifications.
- **Restarts.** Shutdown leaves consults queued or running; killing a run gives its questions to the Planner.
  Before agents, tasks or HTTP start, the server sweeps leftover checkouts and forks and requeues interrupted
  consults, then runs one per seat, oldest first (``resume_all``); until then, new consults stay queued.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..db import llm_records as db_llm_records, projects as db_projects, room as db_room, steps as db_steps
from ..db import tasks as db_tasks
from .agent_settings import AgentType, Launch, load_agent_settings
from .room import FallbackReason, Member, MemberKind, RoomService, _line, run_session_dir
from .system_prompt import REVIEW_CONTRACT, WORK_CONTRACT, SystemPrompt

logger = logging.getLogger(__name__)

CONSULT_MAX_TURNS = 20
# The question bodies share this many characters in a consult prompt.
CONSULT_QUESTIONS_MAX_CHARS = 20_000
GIT_TIMEOUT_SEC = 30
# The runtimes with their own read-only restriction, so a consult runs where its step ran; the others run in a
# throwaway checkout.
RUNTIME_READ_ONLY = ("claude-code", "codex", "grok")

CONSULT_CONTRACT = """\
## Your answer (required; code reads it)
Your step has ended. Colleagues in the task's room ask you about it. Answer
from what you did and from what you can read now. Change nothing: no file
edits, no commits, no command that writes (this run cannot write, and an
attempt is refused). Say plainly what you do not know.
End your final message with exactly one JSON object, and nothing after it:
{"answers": [{"reply_to": 17, "body": "..."}], "unsure": "..."}
- answers: one per question you can answer; reply_to is its id, body your
  answer, short and exact.
- unsure (optional): what you could not answer, and why."""


def consult_prompt(system_prompt: str) -> str:
    """A run's system prompt with ``CONSULT_CONTRACT`` in place of its contract (it ends with one), else after it."""
    for contract in (WORK_CONTRACT, REVIEW_CONTRACT):
        if system_prompt.endswith(contract):
            return system_prompt[: -len(contract)] + CONSULT_CONTRACT
    return f"{system_prompt.rstrip()}\n\n{CONSULT_CONTRACT}"


def consult_answers(text: str, questions: Sequence[int]) -> tuple[dict[int, str], str]:
    """The answers a consult's final message gives, by question id (only the questions it was asked), and what it
    was unsure of. Raises ``ValueError`` when the message ends with no such object."""
    from .tiers.worker import extract_report

    reply = extract_report(text or "", "answers")
    if reply is None or not isinstance(reply.get("answers"), list):
        raise ValueError("the consult's reply has no answers object")
    asked = set(questions)
    answers: dict[int, str] = {}
    for item in reply["answers"]:
        if not isinstance(item, Mapping):
            continue
        try:
            question = int(item.get("reply_to"))
        except (TypeError, ValueError):
            continue
        body = " ".join(str(item.get("body") or "").split()) if not isinstance(item.get("body"), str) \
            else str(item["body"]).strip()
        if question in asked and body:
            answers[question] = body
    return answers, str(reply.get("unsure") or "").strip()


class _Failed(Exception):
    """A consult that could not answer: why, for the Planner."""

    def __init__(self, reason: FallbackReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class ConsultService:
    """The consults of every task's room: ``ask`` is the room's, the WorkerPool runs them."""

    def __init__(self, *, room: RoomService, worker_pool: Any, runtime_root: Path, claude_bin: str = "claude") -> None:
        self.room = room
        self.worker_pool = worker_pool
        self.runtime_root = runtime_root
        self.claude_bin = claude_bin
        self._running: set[asyncio.Task[Any]] = set()
        self._stopping = False
        self._recovered = False

    # --- Asking ---

    async def ask(self, task_id: str, member: Member, question: Mapping[str, Any]) -> FallbackReason | None:
        """Queue a consult of ``member``'s seat for ``question`` (or add it to the one queued); None when it was,
        else why it cannot be."""
        task = await db_tasks.get_task(task_id)
        project = await db_projects.get_project(task["project_id"]) if task and task.get("project_id") else None
        from .project_execution import ProjectExecution

        limits = ProjectExecution.from_row(project).room_limits
        run = await self._last_run(task_id, member)
        if run is None:
            return FallbackReason.NO_SESSION
        record = await db_room.latest_run(task_id, run)
        runtime = ((((record or {}).get("metadata") or {}).get("profile") or {}).get("runtime") or {}).get("type")
        if record is None or record["status"] == "running" or not record.get("session_id") or not runtime:
            return FallbackReason.NO_SESSION
        session_dir = self._session_dir(runtime, task_id, run, record)
        if session_dir is not None and not session_dir.is_dir():
            return FallbackReason.NO_SESSION
        row, start = await db_room.admit_consult(task_id, seat=member.seat, question=int(question["id"]),
                                                 source_run=run, source_record=record["id"], runtime=runtime,
                                                 limit=limits.consults_per_task)
        if row is None:
            return FallbackReason.LIMIT
        if start:
            self._start(row["id"])
        return None

    def _session_dir(self, runtime: str, task_id: str, run: str, record: Mapping[str, Any]) -> Path | None:
        """Where the run kept its session when the runtime does not keep it in its own place: the directory a room
        run was given (``room.run_session_dir``), else the one its recorded command names (Grok's home)."""
        from .runtimes import adapter

        command = (record.get("metadata") or {}).get("command")
        return run_session_dir(self.runtime_root, runtime, task_id, run) or adapter(runtime).kept_session_dir(
            command if isinstance(command, list) else [])

    @staticmethod
    async def _last_run(task_id: str, member: Member) -> str | None:
        """The agent id of the seat's last run: its step's Worker, Reviewer or review role's Reviewer."""
        step = next((s for s in await db_steps.list_steps(task_id) if (s.get("step_key") or s["id"]) == member.step_key),
                    None)
        if step is None:
            return None
        if member.kind is MemberKind.WORK:
            return step.get("worker_id")
        role_stage = member.role != "reviewer"
        if (step.get("review_stage") == "role") != role_stage and step.get("review_stage") is not None:
            return None  # its Reviewer now is the other review's
        return step.get("reviewer_id")

    def _start(self, consult_id: int) -> None:
        """Start a queued consult only after recovery has swept and requeued the previous server's consults."""
        if self._stopping or not self._recovered:
            return
        task = asyncio.get_running_loop().create_task(self._run(consult_id))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def resume_all(self) -> None:
        """At the server's start: sweep and requeue unfinished consults before any queued consult can start."""
        await asyncio.to_thread(_sweep, self.runtime_root)
        try:
            rows = await db_room.unfinished_consults()
        except Exception:
            logger.warning("cannot read the consults left by the last server", exc_info=True)
            return
        for row in rows:
            if row["state"] == "running":
                await db_room.move_consult(row["id"], "queued", expect=("running",))
        self._recovered = True
        seats: set[tuple[str, str]] = set()
        for row in rows:
            seat = (row["task_id"], row["seat"])
            if seat not in seats:
                seats.add(seat)
                self._start(row["id"])

    # --- Running ---

    async def _run(self, consult_id: int) -> None:
        row = await db_room.get_consult(consult_id)
        if row is None or row["state"] != "queued":
            return
        try:
            await self._run_queued(row)
        finally:
            if not self._stopping:
                await self._next(row["task_id"], row["seat"])

    async def _next(self, task_id: str, seat: str) -> None:
        """Start the seat's next queued consult, once none of its consults runs."""
        if self._stopping:
            return
        try:
            rows = [c for c in await db_room.consults(task_id) if c["seat"] == seat]
        except Exception:
            logger.warning("task %s: cannot read the consults of %s", task_id, seat, exc_info=True)
            return
        if not any(c["state"] == "running" for c in rows) and (queued := [c for c in rows if c["state"] == "queued"]):
            self._start(queued[0]["id"])

    async def _run_queued(self, row: Mapping[str, Any]) -> None:
        consult_id, task_id, seat_name = row["id"], row["task_id"], row["seat"]
        task = await db_tasks.get_task(task_id)
        if task is None or task.get("status") not in db_tasks.ACTIVE_STATUSES:
            # Its task ended while it waited: nobody is left to read the answer.
            await db_room.move_consult(consult_id, "failed", expect=("queued",), detail="its task ended first")
            return
        role, _, step_key = seat_name.partition("@")
        steps = await db_steps.list_steps(task_id)
        step = next((s for s in steps if (s.get("step_key") or s["id"]) == step_key), None)
        kind = MemberKind.WORK if step is not None and role == (step.get("role_key") or "worker") else MemberKind.REVIEW
        agent = AgentType.WORKER if kind is MemberKind.WORK else AgentType.REVIEWER
        run_id = f"consult-{uuid.uuid4().hex[:12]}"
        from .tiers.worker import WorkerProcess

        wp = WorkerProcess(worker_id=run_id, task_id=str((step or {}).get("id") or ""), plan_id="",
                           role=agent, parent_task_id=task_id, step=step_key, instruction="consult")
        try:
            async with self.worker_pool.slot_for(wp):
                _, started = await db_room.start_consult(consult_id, consult_run=run_id)
                if started is not db_room.ConsultStart.STARTED:
                    return
                await self._consult(consult_id, task_id, seat_name, kind, agent, step, run_id)
        except asyncio.CancelledError:
            if not self._stopping:
                await asyncio.shield(self._give_up(consult_id, task_id, seat_name, FallbackReason.STOPPED,
                                                   "its run was stopped"))
            raise
        except _Failed as failed:
            await self._give_up(consult_id, task_id, seat_name, failed.reason, str(failed))
        except Exception as exc:
            logger.exception("task %s: the consult of %s failed", task_id, seat_name)
            await self._give_up(consult_id, task_id, seat_name, FallbackReason.CONTRACT,
                                f"{type(exc).__name__}: {exc}")

    async def _consult(self, consult_id: int, task_id: str, seat_name: str, kind: MemberKind, agent: AgentType,
                       step: Mapping[str, Any] | None, run_id: str) -> None:
        from .project_execution import ProjectExecution

        row = await db_room.get_consult(consult_id)
        assert row is not None
        task = await db_tasks.get_task(task_id)
        if task is None or step is None:
            raise _Failed(FallbackReason.NO_SESSION, "its task or its step is gone")
        project = await db_projects.get_project(task["project_id"]) if task.get("project_id") else None
        limits = ProjectExecution.from_row(project).room_limits
        record = await db_room.run_record(row["source_record"])
        metadata = (record or {}).get("metadata") or {}
        if record is None or not record.get("session_id") or record["status"] == "running":
            raise _Failed(FallbackReason.NO_SESSION, "its last run kept no finished session")
        latest = await db_room.latest_run(task_id, row["source_run"])
        if latest is not None and latest["id"] > record["id"]:
            raise _Failed(FallbackReason.NO_SESSION, "its seat started another run meanwhile")
        # The seat's profile now, which must still run where the session is: its runtime and its provider.
        role_key = step.get("role_key") if kind is MemberKind.WORK else (
            step.get("review_role_key") if seat_name.split("@", 1)[0] != "reviewer" else None)
        version = step.get("role_version") if kind is MemberKind.WORK else step.get("review_role_version")
        settings = await load_agent_settings(agent, task.get("project_id"), task_id, step_id=step["id"],
                                             task_role=(role_key, int(version)) if role_key and version else None)
        if settings.profile_error:
            raise _Failed(FallbackReason.RUNTIME_CANNOT, settings.profile_error)
        before = metadata.get("profile") or {}
        if (settings.profile.runtime.type, settings.profile.provider.id) != (
                (before.get("runtime") or {}).get("type"), (before.get("provider") or {}).get("id")):
            raise _Failed(FallbackReason.RUNTIME_CANNOT, "the seat's Agent Profile now runs on another runtime or "
                                                         "provider than its session")
        budget = min(limits.consult_budget_usd, settings.max_budget_usd or limits.consult_budget_usd)
        launch = Launch(replace(settings, max_budget_usd=budget), role=agent, consult=True)
        from .profile_registry import cost_basis

        basis = cost_basis(launch.profile)
        if basis["source"] == "provider_price" and launch.model in basis["unpriced"]:
            raise _Failed(FallbackReason.RUNTIME_CANNOT, "its model has no full price with its provider, so "
                                                         "room_consult_budget_usd cannot hold")
        runtime = launch.runtime
        # Where it runs: where the step ran, its runtime keeping it from writing; else a throwaway checkout.
        cwd, snapshot = str(metadata.get("cwd") or task.get("work_dir") or ""), None
        if not cwd or not Path(cwd).is_dir():
            cwd = str(task.get("work_dir") or "")
        if not cwd or not Path(cwd).is_dir():
            raise _Failed(FallbackReason.NO_SNAPSHOT, "the directory its step ran in, and its task's, are gone")
        fork_dir = self.runtime_root.absolute() / ".local" / runtime.type / "consult-sessions" / task_id / run_id
        try:
            read_only = "runtime"
            if runtime.type not in RUNTIME_READ_ONLY:
                creating = asyncio.create_task(asyncio.to_thread(
                    _snapshot, Path(task.get("work_dir") or ""), self.runtime_root / ".local" / "consults" / run_id))
                try:
                    snapshot = await asyncio.shield(creating)
                except asyncio.CancelledError:
                    snapshot = await creating
                    raise
                if snapshot is None:
                    raise _Failed(FallbackReason.NO_SNAPSHOT, "its task directory is not a git checkout, and its runtime "
                                                              "cannot be kept from writing there")
                cwd, read_only = str(snapshot[2]), "snapshot"
            questions = [m for m in [await db_room.get(task_id, int(q)) for q in row["questions"]] if m is not None]
            system_prompt = SystemPrompt(consult_prompt(str(metadata.get("system_prompt") or "")), {
                "consult": {"seat": seat_name, "source_record": record["id"], "questions": [m["id"] for m in questions],
                            "read_only": read_only}})
            prompt = await self._prompt(task_id, seat_name, step, questions)
            session_dir = self._session_dir(runtime.type, task_id, str(row["source_run"]), record)
            from .runtimes import RuntimeUnsupported

            try:
                prompt_file = system_prompt.write_file(self.runtime_root)
                command = runtime.consult_command(launch.executable(self.claude_bin), cwd=cwd, prompt_file=prompt_file,
                                                  launch=launch, session_id=str(record["session_id"]),
                                                  session_dir=session_dir, fork_dir=fork_dir,
                                                  max_turns=CONSULT_MAX_TURNS)
            except RuntimeUnsupported as exc:
                raise _Failed(FallbackReason.RUNTIME_CANNOT, str(exc)) from None
            asked = ", ".join(f"#{m['id']}" for m in questions)
            await self._note(task_id, run_id, f"Consulting {seat_name} for question(s) {asked} (read only: {read_only})")
            result, started, timed_out = await self._execute(
                task_id, step, run_id, command, cwd, prompt, system_prompt, launch, limits.consult_timeout_seconds)
            wrote = await asyncio.to_thread(_changed, snapshot[2]) if snapshot is not None else []
            denied = len(result.get("permission_denials") or []) if result else 0
            cost = await _run_cost(task_id, run_id)
            if timed_out:
                raise _Failed(FallbackReason.TIMEOUT, f"no answer within {limits.consult_timeout_seconds} s "
                                                      "(room_consult_timeout_seconds)")
            if not started:
                raise _Failed(FallbackReason.SESSION_GONE, "its last run's session could not be resumed")
            if result is None:
                raise _Failed(FallbackReason.CONTRACT, "its run ended without a result")
            if runtime.budget_error(result) is not None:
                raise _Failed(FallbackReason.BUDGET, f"it reached its spend cap of {budget:g} USD (room_consult_budget_usd)")
            if cost is None and isinstance(result.get("total_cost_usd"), (int, float)):
                cost = float(result["total_cost_usd"])
            if cost is None:
                raise _Failed(FallbackReason.BUDGET, f"its cost is unknown, so its cap of {budget:g} USD "
                                                    "(room_consult_budget_usd) cannot be checked: its answers are dropped")
            if cost > budget:
                raise _Failed(FallbackReason.BUDGET, f"it cost {cost:.4f} USD, over its cap of {budget:g} USD "
                                                    "(room_consult_budget_usd): its answers are dropped")
            if wrote:
                raise _Failed(FallbackReason.WROTE_FILES, f"it changed {', '.join(wrote[:10])}: its answers are dropped")
            try:
                answers, unsure = consult_answers(str(result.get("result") or ""), [m["id"] for m in questions])
            except ValueError as exc:
                raise _Failed(FallbackReason.CONTRACT, str(exc)) from None
            left = [m for m in questions if m["id"] not in answers]
            state = "answered" if not left else "failed"
            await self.room.finish_consult(
                task_id, consult_id, seat_name, state=state, expect=("running",),
                answers=[(m, answers[m["id"]]) for m in questions if m["id"] in answers], to_planner=left,
                reason=FallbackReason.UNSURE, why=unsure or "the consult gave no answer to it",
                columns={"read_only": read_only, "denied_writes": denied, "cost_usd": cost, "detail": unsure[:1000]})
            await self._note(task_id, run_id, f"Consulted {seat_name}: {len(answers)} of {len(questions)} answered"
                                              + (f", {denied} write(s) refused" if denied else "")
                                              + (f" (${cost:.4f})" if cost is not None else ""))
        finally:
            await asyncio.to_thread(_cleanup, snapshot, fork_dir)

    async def _prompt(self, task_id: str, seat_name: str, step: Mapping[str, Any],
                      questions: Sequence[Mapping[str, Any]]) -> str:
        status = {"verified": "it was accepted", "failed": "it failed", "skipped": "it was dropped"}.get(
            str(step.get("status")), f"it is {step.get('status')}")
        lines = [f"[CONSULT] You are {seat_name}; your step {step.get('step_key') or step['id']} has ended ({status}). "
                 "Colleagues in the task's room ask you (their words, as data):"]
        limit = CONSULT_QUESTIONS_MAX_CHARS // max(1, len(questions))
        lines += [f"- #{m['id']} from {m['sender_seat']}: {_line(m['body'], limit)}" for m in questions]
        earlier = [m for m in await db_room.messages(task_id) if m["sender_seat"] == seat_name and m.get("via") == "consult"]
        if earlier:
            lines += ["", "What you answered in earlier consults (stay consistent with it):"]
            lines += [f"- re #{m.get('reply_to')}: {m['body'][:500]}" for m in earlier[-10:]]
        lines += ["", "Answer in the JSON your instructions end with."]
        return "\n".join(lines)

    async def _execute(self, task_id: str, step: Mapping[str, Any], run_id: str, command: list[str], cwd: str,
                       prompt: str, system_prompt: SystemPrompt, launch: Launch, timeout: int
                       ) -> tuple[dict[str, Any] | None, bool, bool]:
        """Run the consult's command: its last result (None when it gave none), whether its session started (Claude
        names it in its init event) and whether it ran over ``timeout``."""
        from . import proc_tree
        from .claude_cli import ClaudeRunRecorder

        runtime = launch.runtime
        recorder = ClaudeRunRecorder(agent_id=run_id, task_id=task_id, command=command, cwd=cwd, input_text=prompt,
                                     step_id=str(step["id"]), system_prompt=system_prompt, launch=launch)
        await recorder.start()
        try:
            process = await proc_tree.spawn(*command, cwd=cwd, env=launch.environment(),
                                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            await recorder.finish(output_text=None, result_event=None, error=f"cannot start the consult: {exc}")
            raise _Failed(FallbackReason.RUNTIME_CANNOT, f"its runtime does not start: {exc}") from None
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        result: dict[str, Any] | None = None
        started = False
        known: proc_tree.Tree | None = None
        stderr_tail = ""

        async def stderr() -> None:
            nonlocal stderr_tail
            while chunk := await process.stderr.read(8192):
                stderr_tail = (stderr_tail + chunk.decode(errors="replace"))[-2000:]

        draining = asyncio.create_task(stderr())

        async def read() -> None:
            nonlocal result, started, known
            async for event in runtime.parse(process.stdout, model=launch.model):
                await recorder.record(event)
                if (capped := recorder.cap_stop) is not None:
                    result = capped.result_event
                    await proc_tree.end(process, label=f"consult {run_id}")
                    return
                if event.get("type") == "system" and event.get("subtype") == "init":
                    started = True
                if event.get("type") == "result":
                    result = event
                    known = await proc_tree.begin_end(process)
                    if not process.stdin.is_closing():
                        process.stdin.close()

        async def run() -> None:
            try:
                await runtime.write_turn(process.stdin, prompt)
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                pass
            await read()
            await process.wait()

        error: str | None = None
        try:
            await asyncio.wait_for(run(), timeout=timeout)
            if result is None or result.get("is_error"):
                error = "the consult gave no result"
        except asyncio.TimeoutError:
            error = f"the consult ran over {timeout} s"
            return None, started, True
        except BaseException:
            error = "the consult was stopped"
            raise
        finally:
            try:
                await proc_tree.end(process, label=f"consult {run_id}", known=known)
            finally:
                if not draining.done():
                    draining.cancel()
                await asyncio.gather(draining, return_exceptions=True)
                await recorder.finish(output_text=str((result or {}).get("result") or "") or None,
                                      result_event=result, error=(error + "\n" + stderr_tail if stderr_tail else error)
                                      if error else None)
        return result, started, False

    async def _give_up(self, consult_id: int, task_id: str, seat_name: str, reason: FallbackReason,
                       detail: str) -> None:
        """A consult that could not answer: its questions go to the Planner, the askers are told."""
        row = await db_room.get_consult(consult_id)
        if row is None:
            return
        questions = [m for q in row["questions"] if (m := await db_room.get(task_id, int(q))) is not None]
        row = await self.room.finish_consult(
            task_id, consult_id, seat_name, state="fallback" if reason in _BEFORE_RUNNING else "failed",
            expect=("queued", "running"), answers=[], to_planner=questions, reason=reason, why=detail,
            columns={"fallback_reason": reason.value, "detail": detail[:1000]})
        if row is None:
            return
        await self._note(task_id, row.get("consult_run") or seat_name,
                         f"Could not consult {seat_name} ({reason.value}): {detail}"[:500])

    @staticmethod
    async def _note(task_id: str, worker: str, summary: str) -> None:
        from ..db import activity as db_activity

        try:
            await db_activity.insert_activity(task_id=task_id, worker=worker, kind="phase", summary=summary[:500])
        except Exception:
            logger.debug("could not note a consult", exc_info=True)

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._running):
            task.cancel()
        if self._running:
            await asyncio.wait(list(self._running))


# The reasons a consult gives up before its run started (its questions went to the Planner without one).
_BEFORE_RUNNING = (FallbackReason.NO_SESSION, FallbackReason.RUNTIME_CANNOT, FallbackReason.NO_SNAPSHOT,
                   FallbackReason.LIMIT)


async def _run_cost(task_id: str, run_id: str) -> float | None:
    try:
        record = await db_room.latest_run(task_id, run_id)
        if record is None:
            return None
        row = await db_llm_records.get_record(int(record["id"]))
        value = (row or {}).get("cost_usd")
        return float(value) if value is not None else None
    except Exception:
        logger.warning("task %s: cannot read the cost of consult %s", task_id, run_id, exc_info=True)
        return None


def _git(work_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(work_dir), *args], capture_output=True,
                          text=True, timeout=GIT_TIMEOUT_SEC, check=False)


def _snapshot(work_dir: Path, target: Path) -> tuple[Path, Path, Path] | None:
    """A detached checkout of ``work_dir``'s branch at ``target`` (``git worktree add --detach``): its repository, the
    checkout, and where the task directory is in it; None outside a git checkout."""
    if not work_dir.is_dir():
        return None
    top = _git(work_dir, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    repo = Path(top.stdout.strip())
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git(repo, "worktree", "add", "--detach", str(target), "HEAD").returncode != 0:
        return None
    try:
        inside = target / work_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        inside = target
    return repo, target, inside


def _changed(directory: Path) -> list[str]:
    """What is changed or added in the checkout ``directory`` is in."""
    done = _git(directory, "status", "--porcelain")
    return [line[3:] for line in done.stdout.splitlines() if line.strip()] if done.returncode == 0 else []


def _cleanup(snapshot: tuple[Path, Path, Path] | None, fork_dir: Path) -> None:
    """Remove a consult's checkout and its forked session."""
    if snapshot is not None:
        repo, target, _ = snapshot
        try:
            _git(repo, "worktree", "remove", "--force", str(target))
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(target, ignore_errors=True)
    shutil.rmtree(fork_dir, ignore_errors=True)


def _sweep(runtime_root: Path) -> None:
    """Remove leftover consult checkouts and forks; no consult runs yet when startup calls this."""
    for target in (runtime_root / ".local" / "consults").glob("*"):
        if target.is_dir():
            try:
                _git(target, "worktree", "remove", "--force", str(target.absolute()))
            except (OSError, subprocess.SubprocessError):
                pass
            shutil.rmtree(target, ignore_errors=True)
    for sessions in (runtime_root / ".local").glob("*/consult-sessions"):
        for child in sessions.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)


def consulting(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    """The seats a consult runs for now."""
    return {row["seat"] for row in rows if row["state"] == "running"}

