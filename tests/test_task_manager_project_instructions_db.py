"""The task directory's instruction files reach a direct session's prompt, fenced after the brief, every turn."""
from pathlib import Path

from looper.agents import task_manager_project_instructions as project_instructions
from looper.agents.tiers import task_manager as tm
from looper.db import tasks
from tests.db_support import run_db
from tests.task_manager_support import NativeTaskManager, submit_and_wait


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
