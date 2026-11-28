"""The task directory's instruction files reach a direct session's prompt, fenced after the brief, every turn."""
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

from looper.agents import task_manager_alerts
from looper.agents import task_manager_project_instructions as project_instructions
from looper.agents.tiers import task_manager as tm
from looper.db import tasks
from tests.db_support import run_db, until
from tests.task_manager_support import NativeTaskManager, Sink, submit_and_wait


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True).stdout.strip()


def _prompt(command: list[str]) -> str:
    return Path(command[command.index("--append-system-prompt-file") + 1]).read_text()


def test_direct_prompt_carries_the_fenced_files_and_an_edit_resumes_the_session(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, directory = await native.create(db)
        (directory / "AGENTS.md").write_text("Indent with four spaces.\n")
        (directory.parent / "CLAUDE.md").write_text("PARENT FILE\n")  # the operator's directory, never read
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id)
            [launch] = native.records("launch")
            prompt = _prompt(launch["command"])
            brief_end = prompt.index('<untrusted source="task_requirement">')
            assert prompt.index(project_instructions.LEAD) > brief_end
            assert prompt.endswith(
                project_instructions.LEAD + "\n\n"
                '<untrusted source="project_instructions" file="AGENTS.md">\nIndent with four spaces.\n\n</untrusted>'
                "\n\n" + project_instructions.END)
            assert "PARENT FILE" not in prompt

            await submit_and_wait(pool, task_id, message_id="unchanged")
            assert len(native.records("launch")) == 1  # same files, same signature: the resident is kept

            (directory / "AGENTS.md").write_text("Indent with tabs.\n")
            await submit_and_wait(pool, task_id, message_id="edited")
            launches = native.records("launch")
            assert len(launches) == 2
            session = native.records("input")[0]["session"]
            assert launches[1]["command"][-2:] == ["--resume", session]
            changed = _prompt(launches[1]["command"])
            assert "Indent with tabs." in changed and "Indent with four spaces." not in changed
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_orchestrated_prompt_does_not_read_the_files(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, directory = await native.create(db, status="pending")
        (directory / "AGENTS.md").write_text("Indent with four spaces.\n")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            agent = pool.agent(task_id)
            _, _, _, prompt = await agent.prepare()
            assert agent.mode == "orchestrated" and tasks.interaction_mode("pending") is tasks.InteractionMode.ORCHESTRATED
            assert "project_instructions" not in prompt.text and project_instructions.LEAD not in prompt.text
            assert "Indent with four spaces." not in prompt.text
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def _notices(sink):
    return [payload for kind, payload in sink.events if kind == "sandbox_notice"
            and payload["notice"] == "project_instructions_skipped"]


def test_a_skipped_file_is_reported_once_per_change_and_never_reaches_the_model(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, directory = await native.create(db)
        secret = tmp_path / "outside" / "secret"
        secret.parent.mkdir()
        secret.write_text("OUTSIDE-SECRET-TEXT\n")
        (directory / "CLAUDE.md").symlink_to(secret)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            first = await submit_and_wait(pool, task_id, message_id="m1")
            [notice] = _notices(first)
            assert notice["detail"] == "CLAUDE.md: outside"
            assert "OUTSIDE-SECRET-TEXT" not in _prompt(native.records("launch")[0]["command"]) + repr(first.events)
            assert "OUTSIDE-SECRET-TEXT" not in repr(native.records("input"))
            second = await submit_and_wait(pool, task_id, message_id="m2")
            assert _notices(second) == []
            (directory / "AGENTS.md").write_bytes(b"bad \xff\xfe")
            third = await submit_and_wait(pool, task_id, message_id="m3")
            [both] = _notices(third)
            assert both["detail"] == "AGENTS.md: not_utf8; CLAUDE.md: outside"
            (directory / "CLAUDE.md").unlink()
            (directory / "AGENTS.md").write_text("fine\n")
            fixed = await submit_and_wait(pool, task_id, message_id="m4")
            assert _notices(fixed) == []
            (directory / "CLAUDE.md").symlink_to(secret)
            again = await submit_and_wait(pool, task_id, message_id="m5")
            assert [n["detail"] for n in _notices(again)] == ["CLAUDE.md: outside"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=90)


def test_orchestrated_mode_never_reports_a_skipped_file(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, directory = await native.create(db, status="pending")
        (directory / "CLAUDE.md").symlink_to(tmp_path / "nowhere")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            sink = await submit_and_wait(pool, task_id, message_id="m1")
            assert _notices(sink) == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def _refuse_readback(native, content) -> None:
    """Make the stand-in CLI report ``content`` over its effective settings ({} restores a clean readback)."""
    native.transcript.with_name("readback-override.json").write_text(json.dumps(content))


WIDER = {"get_settings": {"effective": {"sandbox": {"excludedCommands": ["git"]}}}}


def test_a_turn_the_readback_refuses_reports_no_skipped_file_and_the_next_turn_does(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    monkeypatch.setattr(task_manager_alerts, "invariant_violation", AsyncMock())

    async def check(db):
        task_id, _, directory = await native.create(db)
        (directory / "CLAUDE.md").symlink_to(tmp_path / "nowhere" / "CLAUDE.md")
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            _refuse_readback(native, WIDER)
            refused = Sink()
            await pool.submit(task_id, "reply", message_id="m1", sink=refused)
            await refused.done()
            assert refused.finals[-1][1] == "refused" and "sandbox.excludedCommands" in refused.finals[-1][0]
            assert _notices(refused) == []  # the notice comes after the readback
            await until(lambda: not pool.in_turn(task_id), "the refused turn's cleanup")
            _refuse_readback(native, {})
            admitted = await submit_and_wait(pool, task_id, message_id="m2")
            assert [n["message_id"] for n in _notices(admitted)] == ["m2"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_the_fresh_process_after_a_failed_resume_reports_nothing_before_its_readback(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    monkeypatch.setattr(task_manager_alerts, "invariant_violation", AsyncMock())

    async def check(db):
        task_id, _, directory = await native.create(db)

        class ChangeBeforeTheFreshLaunch(Sink):
            async def event(self, kind, payload):
                await super().event(kind, payload)
                if kind.value == "session_fresh":  # sent before the fresh launch reads the files again
                    (directory / "CLAUDE.md").symlink_to(tmp_path / "nowhere" / "CLAUDE.md")
                    _refuse_readback(native, WIDER)

        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id, message_id="m1")
            await pool.close(task_id)
            (directory / "reject-resume").touch()  # the resumed process exits before init
            sink = ChangeBeforeTheFreshLaunch()
            await pool.submit(task_id, "two", message_id="m2", sink=sink)
            await sink.done()
            assert sink.finals[-1][1] == "refused" and len(native.records("launch")) == 3
            assert _notices(sink) == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_a_turn_refused_for_a_moved_head_does_not_use_up_the_skip_notice(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)

    async def check(db):
        task_id, _, directory = await native.create(db)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            await submit_and_wait(pool, task_id, message_id="m1")  # records the task branch
            branch = _git(directory, "symbolic-ref", "HEAD")
            _git(directory, "symbolic-ref", "HEAD", "refs/heads/elsewhere")
            (directory / "CLAUDE.md").symlink_to(tmp_path / "nowhere" / "CLAUDE.md")
            refused = Sink()
            result = await pool.submit(task_id, "reply", message_id="m2", sink=refused)
            assert result.state == "refused" and result.reason is tm.RefuseReason.HEAD_MOVED
            await refused.done()
            assert _notices(refused) == []
            await until(lambda: not pool.in_turn(task_id), "the refused turn's cleanup")
            _git(directory, "symbolic-ref", "HEAD", branch)
            admitted = await submit_and_wait(pool, task_id, message_id="m3")
            assert [n["message_id"] for n in _notices(admitted)] == ["m3"]
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=60)


def test_a_refused_directory_never_has_its_instruction_files_opened(monkeypatch, tmp_path):
    native = NativeTaskManager(monkeypatch, tmp_path)
    opened: list[str] = []
    real_read = project_instructions.read
    monkeypatch.setattr(project_instructions, "read", lambda work_dir: opened.append(str(work_dir)) or real_read(work_dir))

    async def check(db):
        repository = tmp_path / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True, capture_output=True)
        child = repository / "nested"
        child.mkdir()
        (child / "AGENTS.md").write_text("SHOULD-NEVER-BE-READ\n")
        task_id, _, _ = await native.create(db, path=child, git=False)
        pool = tm.TaskManagerPool(tmp_path / "runtime", native.executable, max_resident=lambda: 1)
        try:
            result = await pool.submit(task_id, "reply", message_id="nested", sink=Sink())
            assert result.state == "refused" and result.reason is tm.RefuseReason.WORKSPACE_NOT_REPO_ROOT
            assert opened == []  # the admission refused before any instruction file was read
            assert native.records("launch") == []
        finally:
            await pool.shutdown()
    run_db(check, monkeypatch, timeout=30)
