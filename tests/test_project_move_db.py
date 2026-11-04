"""Directory moves preserve project files, references and git worktrees on PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from looper.agents import operator_notes, project_move, projects as api
from looper.db import projects, tasks
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def isolated_notes_root(monkeypatch, tmp_path):
    from looper import child_env

    monkeypatch.setattr(operator_notes, "_root", str(tmp_path / "operator-notes"))
    # A basetemp inside the worktree must not inherit the repository's own notes key.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"GIT_CEILING_DIRECTORIES"})


async def _project(pool, old, *, name="Move example"):
    old.mkdir()
    (old / "subdir").mkdir()
    (old / "subdir" / "kept.txt").write_text("keep these files\n")
    return await projects.create_project(name=name, work_dir=str(old), pool=pool)


async def _schedule(pool, project_id, work_dir, *, schedule_id="command", kind="command"):
    await pool.execute(
        "INSERT INTO schedules (id, project_id, name, cron, timezone, requirement, work_dir, kind, command) "
        "VALUES ($1,$2,$1,'0 * * * *','UTC','Do the scheduled work',$3,$4,$5)",
        schedule_id, project_id, str(work_dir), kind, "true" if kind == "command" else None,
    )


async def _rows(pool):
    """Compare whole rows, so a refusal cannot silently update unrelated state."""
    return {
        table: [dict(row) for row in await pool.fetch(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("projects", "schedules", "tasks", "task_worktrees", "task_steps", "step_workspaces",
                      "manager_task_templates", "llm_records", "schedule_runs", "task_user_gates", "project_activity")
    }


def test_move_preserves_files_rewrites_schedules_and_keeps_task_records(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        await _schedule(pool, project["id"], old / "subdir", schedule_id="agent", kind="agent")
        await _schedule(pool, project["id"], str(old) + "-other", schedule_id="outside")
        await _schedule(pool, project["id"], "", schedule_id="default", kind="agent")
        await _schedule(pool, project["id"], "", schedule_id="inherited-command")
        other = await projects.create_project(name="Other", work_dir=str(old), pool=pool)
        await _schedule(pool, other["id"], old, schedule_id="other-project")
        for task_id, status in (("ready", "init"), ("waiting", "stopped"), ("finished", "completed")):
            await tasks.create_task(task_id=task_id, requirement="Keep the recorded paths", project_id=project["id"],
                                    work_dir=str(old / "subdir"), workspace_dir=str(old), status=status, pool=pool)
        await pool.execute("INSERT INTO task_steps (id,task_id,step_index,instruction,status,kind) "
                           "VALUES ('wait-step','waiting',0,'Wait for input','pending','wait')")
        await pool.execute("INSERT INTO task_waits (step_id,task_id,topic,spec,state) "
                           "VALUES ('wait-step','waiting','Waiting','{}','waiting')")
        recorded_tasks = [dict(row) for row in await pool.fetch("SELECT * FROM tasks ORDER BY id")]
        preview = await api.preview_project_move(project, {"to": str(new)}, db=pool)
        impact = api.project_move_impact(project, preview)
        assert "default" in impact and "inherited-command" in impact
        result = await api.move_project_dir(project["id"], {"to": str(new)})

        assert result["project"]["work_dir"] == str(new)
        assert (result["old_dir"], result["new_dir"], result["link_old"]) == (str(old), str(new), True)
        assert old.is_symlink() and old.resolve() == new.resolve()
        assert (new / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        paths = {row["id"]: row["work_dir"] for row in await pool.fetch("SELECT id,work_dir FROM schedules")}
        assert paths == {"command": str(new), "agent": str(new / "subdir"), "outside": str(old) + "-other",
                         "default": "", "inherited-command": "", "other-project": str(old)}
        assert [dict(row) for row in await pool.fetch("SELECT * FROM tasks ORDER BY id")] == recorded_tasks
        activity = await pool.fetch("SELECT kind,summary FROM project_activity WHERE project_id=$1", project["id"])
        assert [dict(row) for row in activity] == [
            {"kind": "directory_moved", "summary": f"directory moved from {old} to {new}; notes not moved: notes root does not exist"}]

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("name", "directory"), [
    (" ..Hello / WORLD---v1.. ", "hello-world-v1"), ("Keep_This.v1", "keep_this.v1"), ("中文项目", None),
])
def test_move_without_destination_uses_the_creation_directory_name(monkeypatch, tmp_path, name, directory):
    old, root = tmp_path / "old", tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(api, "_root", str(root))

    async def check(pool):
        project = await _project(pool, old, name=name)
        result = await api.move_project_dir(project["id"], {})
        expected = root / (directory or project["id"])
        assert result["new_dir"] == str(expected)
        assert expected.is_dir() and (expected / "subdir" / "kept.txt").is_file()
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(expected)

    run_db(check, monkeypatch, timeout=30)


def test_move_can_omit_old_path_link(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        assert result["link_old"] is False
        assert not os.path.lexists(old)
        assert (new / "subdir" / "kept.txt").is_file()
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("status", tasks.ACTIVE_STATUSES)
@pytest.mark.parametrize("link_old", [False, True])
def test_move_refuses_active_task_between_turns(monkeypatch, tmp_path, status, link_old):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        # An active task blocks the move even with no running call or step, and
        # even when its own directory is outside the project directory.
        await tasks.create_task(task_id="between-turns", requirement="Continue next turn",
                                project_id=project["id"], work_dir=str(old) + "-other", status=status, pool=pool)
        data = {"to": str(new), "link_old": link_old}
        before = await _rows(pool)
        for operation in (lambda: api.preview_project_move(project, data, db=pool),
                          lambda: api.move_project_dir(project["id"], data)):
            with pytest.raises(api.RequestError) as error:
                await operation()
            assert error.value.status == api.HTTP_409
            assert await _rows(pool) == before
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        await tasks.update_task_status("between-turns", "stopped", pool=pool)
        assert (await api.move_project_dir(project["id"], data))["new_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("dependency", "state"), [
    ("task-work-dir", "init"), ("task-work-dir", "stopped"), ("task-work-dir", "completed"),
    ("task-workspace", "stopped"), ("step-repository", "active"), ("step-checkout", "active"),
    ("step-repository", "merged"), ("step-checkout", "discarded"), ("template", ""),
])
def test_move_without_link_refuses_own_project_directory_dependencies(monkeypatch, tmp_path, dependency, state):
    old, new = tmp_path / "old", tmp_path / "new"
    (tmp_path / "alias").symlink_to(old, target_is_directory=True)

    async def check(pool):
        project = await _project(pool, old)
        reference = str(tmp_path / "alias" / "subdir")
        outside = str(old) + "-other"
        await tasks.create_task(task_id="recorded-task", requirement="Keep recorded paths", project_id=project["id"],
                                work_dir=reference if dependency == "task-work-dir" else outside,
                                workspace_dir=reference if dependency == "task-workspace" else "",
                                status=state if dependency.startswith("task-") else "stopped", pool=pool)
        if dependency.startswith("step-"):
            await pool.execute("INSERT INTO task_steps (id,task_id,step_index,instruction) "
                               "VALUES ('recorded-step','recorded-task',0,'Keep work')")
            await pool.execute("INSERT INTO step_workspaces "
                               "(task_id,step_id,repo_dir,path,target_branch,branch,base_commit,state) "
                               "VALUES ('recorded-task','recorded-step',$1,$2,'main','step-branch','base',$3)",
                               reference if dependency == "step-repository" else outside,
                               reference if dependency == "step-checkout" else outside, state)
        elif dependency == "template":
            await pool.execute("INSERT INTO manager_task_templates (manager_id,key,name,project_id,work_dir) "
                               "VALUES ('manager','recorded','Recorded',$1,$2)", project["id"], reference)
        data = {"to": str(new), "link_old": False}
        before = await _rows(pool)
        for operation in (lambda: api.preview_project_move(project, data, db=pool),
                          lambda: api.move_project_dir(project["id"], data)):
            with pytest.raises(api.RequestError) as error:
                await operation()
            assert error.value.status == api.HTTP_409
            assert await _rows(pool) == before
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        # The same inactive references remain usable when the old path is linked.
        result = await api.move_project_dir(project["id"], {**data, "link_old": True})
        assert result["new_dir"] == str(new)
        assert old.is_symlink() and (Path(reference) / "kept.txt").read_text() == "keep these files\n"

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("case", "message"), [
    ("no-directory", "directory"), ("same", "to already exists"),
    ("relative", "absolute"), ("inside", "inside|within"),
    ("empty", "to already exists; choose a path that does not exist"),
    ("occupied", "empty|exists"), ("file", "empty|directory|exists"),
    ("parent-missing", "parent|exist"), ("parent-readonly", "writ"),
    ("cross-device", "filesystem|file system|device"),
    ("step-running", "progress|running|active|step"), ("run-open", "progress|running|active|run"),
    ("step-deciding", "progress|running|active|step"), ("step-review", "progress|running|active|step"),
    ("command-running", "progress|running|active|schedule"), ("schedule-gating", "gate|gating|progress"),
    ("gate-open", "gate|progress"), ("gate-discussing", "gate|progress"),
    ("no-root", "root|destination|to"),
])
def test_move_refusals_preserve_files_and_rows(monkeypatch, tmp_path, case, message):
    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr(api, "_root", "")

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        data = {"to": str(new)}
        if case == "no-directory":
            await projects.update_project(project["id"], work_dir="", pool=pool)
            data["to"] = "relative-destination"
        elif case == "same":
            data["to"] = str(old)
        elif case == "relative":
            data["to"] = "relative-destination"
        elif case == "inside":
            data["to"] = str(old / "inside")
        elif case == "empty":
            new.mkdir()
        elif case == "occupied":
            new.mkdir()
            (new / "unrelated.txt").write_text("leave me alone")
        elif case == "file":
            new.write_text("leave me alone")
        elif case == "parent-missing":
            data["to"] = str(tmp_path / "missing-parent" / "new")
        elif case == "parent-readonly":
            access = os.access
            monkeypatch.setattr(api.os, "access", lambda path, mode, *args, **kwargs:
                                False if Path(path) == new.parent else access(path, mode, *args, **kwargs))
        elif case == "cross-device":
            stat = os.stat

            def other_device(path, *args, **kwargs):
                result = stat(path, *args, **kwargs)
                if Path(path) == new.parent:
                    values = list(result)
                    values[2] += 1  # st_dev
                    return os.stat_result(values)
                return result

            monkeypatch.setattr(api.os, "stat", other_device)
        elif case == "no-root":
            data = {}
        elif case in ("command-running", "schedule-gating"):
            await pool.execute("INSERT INTO schedule_runs (id,schedule_id,scheduled_for,trigger,status) "
                               "VALUES ('schedule-run','command',now(),'manual',$1)",
                               "running" if case == "command-running" else "gating")
        else:
            # Keep the task inactive so each child-state guard is tested on its own.
            await tasks.create_task(task_id="busy", requirement="Busy task", project_id=project["id"],
                                    work_dir=str(old), status="stopped", pool=pool)
            if case == "run-open":
                await pool.execute("INSERT INTO llm_records (agent_id,task_id,status) "
                                   "VALUES ('planner-busy','busy','running')")
            else:
                await pool.execute("INSERT INTO task_steps (id,task_id,step_index,instruction,status,kind) "
                                   "VALUES ('busy-step','busy',0,'Work',$1,$2)",
                                   {"step-running": "dispatched", "step-deciding": "deciding",
                                    "step-review": "completed"}.get(case, "pending"),
                                   "work" if case.startswith("step-") else "gate")
                if case.startswith("gate-"):
                    await pool.execute("INSERT INTO task_user_gates (step_id,task_id,topic,state) "
                                       "VALUES ('busy-step','busy','Choose',$1)", case.removeprefix("gate-"))
        if case in ("no-directory", "same", "relative", "inside", "empty", "occupied", "file", "parent-missing",
                    "parent-readonly", "cross-device"):
            # Path errors must be returned before the otherwise-blocking open run.
            await tasks.create_task(task_id="later-refusal", requirement="Open run", project_id=project["id"],
                                    work_dir=str(old), status="running", pool=pool)
            await pool.execute("INSERT INTO llm_records (agent_id,task_id,status) "
                               "VALUES ('planner-later','later-refusal','running')")
        before = await _rows(pool)
        with pytest.raises(api.RequestError, match=message) as error:
            await api.move_project_dir(project["id"], data)
        assert error.value.status in (api.HTTP_400, api.HTTP_409)
        assert await _rows(pool) == before
        assert old.is_dir() and not old.is_symlink()
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        if case == "empty":
            assert error.value.status == api.HTTP_409
            assert new.is_dir() and not list(new.iterdir())
        elif case == "occupied":
            assert (new / "unrelated.txt").read_text() == "leave me alone"
        elif case == "file":
            assert new.read_text() == "leave me alone"
        else:
            assert not new.exists()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("link_old", [True, False])
def test_failed_database_commit_restores_directory_and_all_rows(monkeypatch, tmp_path, link_old):
    import asyncpg

    old, new = tmp_path / "old", tmp_path / "new"
    renamed = []
    rename = os.rename

    def track_rename(source, destination, *args, **kwargs):
        result = rename(source, destination, *args, **kwargs)
        renamed.append((Path(source), Path(destination)))
        return result

    monkeypatch.setattr(api.os, "rename", track_rename)

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        await _schedule(pool, project["id"], old / "subdir", schedule_id="agent", kind="agent")
        checkout = old / "checkout" if link_old else tmp_path / "checkout"
        await tasks.create_task(task_id="stopped-task", requirement="Keep worktree paths on rollback",
                                project_id=project["id"], work_dir=str(checkout), status="stopped", pool=pool)
        await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                           "VALUES ('stopped-task',$1,$2,'looper/stopped-task/task','base')",
                           str(old), str(checkout))
        await pool.execute("""
            CREATE FUNCTION refuse_directory_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'directory commit refused' USING ERRCODE = '23514'; END $$;
            CREATE CONSTRAINT TRIGGER refuse_directory_commit AFTER UPDATE ON projects
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION refuse_directory_commit();
        """)
        before = await _rows(pool)
        with pytest.raises((asyncpg.CheckViolationError, api.RequestError), match="directory commit refused"):
            await api.move_project_dir(project["id"], {"to": str(new), "link_old": link_old})
        assert await _rows(pool) == before
        assert renamed == [(old, new), (new, old)]
        assert old.is_dir() and not old.is_symlink()
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert not os.path.lexists(new)

    run_db(check, monkeypatch, timeout=30)


def test_failed_old_path_symlink_restores_files_and_rows(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        before = await _rows(pool)
        observed = []

        def fail_link(target, link, **kwargs):
            observed.append((Path(target), Path(link)))
            assert new.is_dir() and not old.exists()
            raise OSError("cannot create old path symlink")

        monkeypatch.setattr(api.os, "symlink", fail_link)
        with pytest.raises(api.RequestError, match="cannot create old path symlink") as error:
            await api.move_project_dir(project["id"], {"to": str(new)})
        assert error.value.status == api.HTTP_400
        assert observed == [(new, old)]
        assert await _rows(pool) == before
        assert old.is_dir() and not old.is_symlink()
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert not os.path.lexists(new)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("interruption", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("link_state", ["kept", "missing", "unwritable", "inaccessible", "disabled"])
def test_interruption_after_commit_keeps_moved_files_and_rows(monkeypatch, tmp_path, caplog,
                                                             interruption, link_state):
    old, new = tmp_path / "old", tmp_path / "new"
    renames = []
    rename = os.rename

    def track_rename(source, destination, **kwargs):
        renames.append((Path(source), Path(destination)))
        return rename(source, destination, **kwargs)

    monkeypatch.setattr(project_move.os, "rename", track_rename)

    async def check(pool):
        project = await _project(pool, old)
        await _schedule(pool, project["id"], old)
        release = type(pool).release
        interrupted = False

        async def release_after_commit(self, conn, *args, **kwargs):
            nonlocal interrupted
            if self is pool and not interrupted and new.is_dir() and not conn.is_in_transaction():
                # Pool release awaits after COMMIT, before the transaction helper returns.
                assert await conn.fetchval("SELECT work_dir FROM projects WHERE id = $1", project["id"]) == str(new)
                interrupted = True
                if link_state in ("missing", "unwritable"):
                    old.unlink()
                if link_state == "unwritable":
                    def fail_link(*args, **kwargs):
                        raise OSError("old path is not writable")
                    monkeypatch.setattr(project_move.os, "symlink", fail_link)
                elif link_state == "inaccessible":
                    is_symlink = Path.is_symlink

                    def inaccessible_link(path):
                        if path == old:
                            raise PermissionError("old path is not accessible")
                        return is_symlink(path)

                    monkeypatch.setattr(Path, "is_symlink", inaccessible_link)
                await release(self, conn, *args, **kwargs)
                raise interruption()
            return await release(self, conn, *args, **kwargs)

        monkeypatch.setattr(type(pool), "release", release_after_commit)
        with pytest.raises(interruption):
            await api.move_project_dir(project["id"], {"to": str(new), "link_old": link_state != "disabled"})
        assert interrupted
        assert renames == [(old, new)]
        assert (new / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        assert await pool.fetchval("SELECT work_dir FROM schedules WHERE id = 'command'") == str(new)
        if link_state in ("unwritable", "inaccessible"):
            assert os.path.lexists(old) == (link_state == "inaccessible")
            assert any(record.levelname == "ERROR" and str(old) in record.message
                       and str(new) in record.message and "link is missing" in record.message
                       for record in caplog.records)
        elif link_state == "disabled":
            assert not os.path.lexists(old)
        else:
            assert old.is_symlink() and old.resolve() == new.resolve()

    run_db(check, monkeypatch, timeout=30)


def test_file_inventory_cap_is_explicit_in_confirmation(monkeypatch, tmp_path):
    from looper.agents import project_move

    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr(project_move, "FILE_LIMIT", 2)

    async def check(pool):
        project = await _project(pool, old)
        (old / "second.txt").write_bytes(b"second")
        (old / "third.txt").write_bytes(b"third")
        total_bytes = sum(path.stat().st_size for path in old.rglob("*") if path.is_file())
        before = await _rows(pool)
        preview = await api.preview_project_move(project, {"to": str(new)}, db=pool)
        text = api.project_move_impact(project, preview)
        assert preview.truncated and preview.files == 3
        assert preview.bytes == total_bytes
        assert "more than 2 files" in text
        assert f"至少 {total_bytes} 字节" in text
        assert await _rows(pool) == before
        assert old.is_dir() and not new.exists()

    run_db(check, monkeypatch, timeout=30)


def test_inventory_bounds_directory_only_trees(monkeypatch, tmp_path):
    from looper.agents import project_move

    old, new = tmp_path / "old", tmp_path / "new"
    monkeypatch.setattr(project_move, "ENTRY_LIMIT", 2)

    async def check(pool):
        old.mkdir()
        directory = old
        for index in range(10):
            directory /= f"directory-{index}"
            directory.mkdir()
        project = await projects.create_project(name="Directory tree", work_dir=str(old), pool=pool)
        scans = []
        scandir = os.scandir

        def bounded_scan(path):
            scans.append(path)
            assert len(scans) <= 2, "inventory exceeded its directory entry budget"
            return scandir(path)

        monkeypatch.setattr(project_move.os, "scandir", bounded_scan)
        preview = await api.preview_project_move(project, {"to": str(new)}, db=pool)
        assert preview.truncated and preview.files == 0 and preview.bytes == 0
        assert "扫描达到上限" in api.project_move_impact(project, preview)
        assert scans
        assert not new.exists()

    run_db(check, monkeypatch, timeout=30)


def _git(directory, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(directory), *args],
                          check=True, capture_output=True, text=True, timeout=10).stdout.strip()


@pytest.mark.parametrize("common_name", ["repo.git", "modules"])
def test_move_worktree_of_bare_repository(monkeypatch, tmp_path, common_name):
    seed, bare = tmp_path / "seed", tmp_path / common_name
    old, new = tmp_path / "old", tmp_path / "new"
    seed.mkdir()
    (seed / "tracked.txt").write_text("keep these files\n")
    _git(seed, "init", "-q")
    _git(seed, "add", ".")
    _git(seed, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
         "commit", "-qm", "Initial files")
    _git(tmp_path, "clone", "--bare", str(seed), str(bare))
    _git(bare, "worktree", "add", "-qb", "moving", str(old))
    base = _git(old, "rev-parse", "HEAD")
    assert _git(old, "status", "--porcelain") == ""

    async def check(pool):
        project = await projects.create_project(name="Bare repository worktree", work_dir=str(old), pool=pool)
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        assert result["repairs"] and all(repair["ok"] for repair in result["repairs"])
        assert not old.exists() and (new / "tracked.txt").read_text() == "keep these files\n"
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        assert _git(new, "status", "--porcelain") == ""
        assert _git(new, "rev-parse", "HEAD") == base
        assert str(new.resolve()) in _git(bare, "worktree", "list", "--porcelain")
        assert str(old.resolve()) not in _git(bare, "worktree", "list", "--porcelain")

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("nested", [False, True], ids=["project-repo", "nested-repo"])
def test_move_repairs_external_linked_task_worktree(monkeypatch, tmp_path, nested):
    old, new, linked = tmp_path / "old", tmp_path / "new", tmp_path / "task-worktree"

    async def check(pool):
        project = await _project(pool, old)
        repo = old / "repository" if nested else old
        repo.mkdir(exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "--allow-empty", "-qm", "Initial files")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "worktree", "add", "-qb", "looper/task-example/task", str(linked))
        # Only the live task_worktrees row references the old repository here;
        # it is rewritten by the move, so no historical path needs the link.
        await tasks.create_task(task_id="task-example", requirement="Keep its worktree", project_id=project["id"],
                                work_dir=str(linked), status="stopped", pool=pool)
        await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                           "VALUES ('task-example',$1,$2,'looper/task-example/task',$3)", str(repo), str(linked), base)
        # Positive control: this really is a working linked checkout before the move.
        assert _git(linked, "rev-parse", "HEAD") == base
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": False})
        assert not old.exists()
        assert result["repairs"]
        assert _git(linked, "status", "--porcelain") == ""
        assert _git(linked, "rev-parse", "HEAD") == base
        moved_repo = new / "repository" if nested else new
        assert str(linked.resolve()) in _git(moved_repo, "worktree", "list", "--porcelain")
        assert str(moved_repo.resolve()) in (linked / ".git").read_text()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("parent_symlink", [False, True], ids=["direct-parent", "symlink-parent"])
@pytest.mark.parametrize("layout", ["external-worktree", "both-inside", "linked-source"])
def test_deleting_stopped_task_after_move_preserves_uncommitted_files(monkeypatch, tmp_path, layout, parent_symlink):
    old, new = tmp_path / "old", tmp_path / "new"
    if parent_symlink:
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        old = tmp_path / "alias" / "old"
    repo = tmp_path / "repository" if layout == "linked-source" else old
    linked = (old if layout == "linked-source" else
              old / "checkout" if layout == "both-inside" else tmp_path / "checkout")
    branch = "looper/stopped-task/task"

    async def check(pool):
        repo.mkdir()
        (repo / "tracked.txt").write_text("original\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "tracked.txt")
        _git(repo, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "-qm", "Initial files")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "worktree", "add", "-qb", branch, str(linked))
        (linked / "tracked.txt").write_text("uncommitted edit\n")
        (linked / "untracked.txt").write_text("uncommitted new file\n")
        assert "tracked.txt" in _git(linked, "status", "--porcelain")
        assert "?? untracked.txt" in _git(linked, "status", "--porcelain")
        project = await projects.create_project(name="Preserve stopped work", work_dir=str(old), pool=pool)
        await _schedule(pool, project["id"], old.resolve())
        await tasks.create_task(task_id="stopped-task", requirement="Keep unfinished work",
                                project_id=project["id"], work_dir=str(linked), workspace_dir=str(repo),
                                status="stopped", pool=pool)
        await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                           "VALUES ('stopped-task',$1,$2,$3,$4)",
                           _git(repo, "rev-parse", "--show-toplevel"), str(linked.resolve()), branch, base)

        preview = await api.preview_project_move(project, {"to": str(new), "link_old": True}, db=pool)
        assert [row["id"] for row in preview.schedules] == ["command"]
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": True})
        assert result["repairs"] and all(row["ok"] for row in result["repairs"])
        assert old.is_symlink() and old.resolve() == new.resolve()
        assert await pool.fetchval("SELECT work_dir FROM schedules WHERE id='command'") == str(new)
        moved_repo = repo if layout == "linked-source" else new
        moved_linked = new if layout == "linked-source" else new / "checkout" if layout == "both-inside" else linked
        assert (moved_linked / "tracked.txt").read_text() == "uncommitted edit\n"
        assert (moved_linked / "untracked.txt").read_text() == "uncommitted new file\n"

        assert await api.delete_task("stopped-task", None) == {"deleted": "stopped-task"}
        assert await tasks.get_task("stopped-task", pool=pool) is None
        assert _git(moved_repo, "show", f"{branch}:tracked.txt") == "uncommitted edit"
        assert _git(moved_repo, "show", f"{branch}:untracked.txt") == "uncommitted new file"
        assert not moved_linked.exists()
        assert str(moved_linked.resolve()) not in _git(moved_repo, "worktree", "list", "--porcelain")

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("dependency", ["project", "task-work-dir", "task-workspace", "task-repository",
                                        "task-checkout", "step-repository", "step-checkout", "schedule", "template"])
def test_move_without_link_refuses_other_project_directory_dependencies(monkeypatch, tmp_path, dependency):
    old, new, linked = tmp_path / "old", tmp_path / "new", tmp_path / "checkout"
    (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)

    async def check(pool):
        project = await _project(pool, old)
        _git(old, "init", "-q")
        _git(old, "add", ".")
        _git(old, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "-qm", "Initial files")
        base = _git(old, "rev-parse", "HEAD")
        branch = "looper/other-task/task"
        _git(old, "worktree", "add", "-qb", branch, str(linked))
        (linked / "subdir" / "kept.txt").write_text("unfinished edit\n")
        (linked / "untracked.txt").write_text("unfinished new file\n")
        reference = str(tmp_path / "alias" / "old")
        other = await projects.create_project(name="Other", work_dir=reference if dependency == "project" else "",
                                               pool=pool)
        await tasks.create_task(task_id="other-task", requirement="Keep unfinished work", project_id=other["id"],
                                work_dir=reference if dependency == "task-work-dir" else str(linked),
                                workspace_dir=reference if dependency == "task-workspace" else "",
                                status="stopped", pool=pool)
        if dependency.startswith("task-") and dependency not in ("task-work-dir", "task-workspace"):
            await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                               "VALUES ('other-task',$1,$2,$3,$4)",
                               reference if dependency == "task-repository" else str(linked),
                               reference if dependency == "task-checkout" else str(linked), branch, base)
        elif dependency.startswith("step-"):
            await pool.execute("INSERT INTO task_steps (id,task_id,step_index,instruction) "
                               "VALUES ('other-step','other-task',0,'Keep work')")
            await pool.execute("INSERT INTO step_workspaces "
                               "(task_id,step_id,repo_dir,path,target_branch,branch,base_commit) "
                               "VALUES ('other-task','other-step',$1,$2,'main',$3,$4)",
                               reference if dependency == "step-repository" else str(linked),
                               reference if dependency == "step-checkout" else str(linked), branch, base)
        elif dependency == "schedule":
            await _schedule(pool, other["id"], reference)
        elif dependency == "template":
            await pool.execute("INSERT INTO manager_task_templates (manager_id,key,name,project_id,work_dir) "
                               "VALUES ('manager','other','Other',$1,$2)", other["id"], reference)
        before = await _rows(pool)
        for operation in (lambda: api.preview_project_move(project, {"to": str(new), "link_old": False}, db=pool),
                          lambda: api.move_project_dir(project["id"], {"to": str(new), "link_old": False})):
            with pytest.raises(api.RequestError) as error:
                await operation()
            assert error.value.status == api.HTTP_409
            assert await _rows(pool) == before
            assert old.is_dir() and not old.is_symlink() and not new.exists()
            assert "?? untracked.txt" in _git(linked, "status", "--porcelain")
        if dependency == "task-repository":
            assert await api.delete_task("other-task", None) == {"deleted": "other-task"}
            assert _git(old, "show", f"{branch}:subdir/kept.txt") == "unfinished edit"
            assert _git(old, "show", f"{branch}:untracked.txt") == "unfinished new file"

    run_db(check, monkeypatch, timeout=30)


def test_git_repair_failure_is_reported_after_successful_move(monkeypatch, tmp_path):
    from looper.agents import step_workspaces

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        _git(old, "init", "-q")
        await _schedule(pool, project["id"], old)
        attempted = []

        async def fail_repair(directory, *args, **kwargs):
            attempted.append((Path(directory), args))
            raise step_workspaces.GitError("simulated worktree repair failure")

        monkeypatch.setattr(step_workspaces, "_git", fail_repair)
        result = await api.move_project_dir(project["id"], {"to": str(new)})
        assert attempted == [(new, ("worktree", "repair"))]
        assert result["repairs"] == [{"repository": str(new), "ok": False,
                                      "output": "simulated worktree repair failure"}]
        assert old.is_symlink() and old.resolve() == new.resolve()
        assert (new / "subdir" / "kept.txt").read_text() == "keep these files\n"
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        assert await pool.fetchval("SELECT work_dir FROM schedules WHERE id='command'") == str(new)
        assert await pool.fetchval("SELECT count(*) FROM project_activity WHERE project_id=$1", project["id"]) == 1

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize(("layout", "managed", "parent_symlink"), [
    ("linked-source", True, False), ("both-inside", True, False), ("both-inside", False, False),
    ("both-inside", False, True),
])
def test_move_repairs_moved_linked_checkout(monkeypatch, tmp_path, layout, managed, parent_symlink):
    old, new = tmp_path / "old", tmp_path / "new"
    if parent_symlink:
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        old = tmp_path / "alias" / "old"

    async def check(pool):
        if layout == "linked-source":
            repo, linked = tmp_path / "external-main", old
        else:
            old.mkdir()
            repo, linked = old / "repository", old / "linked"
            # Discover the linked checkout first, before its common repository.
            linked.mkdir()
        repo.mkdir()
        (repo / "kept.txt").write_text("tracked content\n")
        _git(repo, "init", "-q")
        _git(repo, "add", "kept.txt")
        _git(repo, "-c", "user.name=Directory move test", "-c", "user.email=move@example.invalid",
             "commit", "-qm", "Initial files")
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "worktree", "add", "-qb", "looper/moving-task/task", str(linked))
        assert _git(linked, "status", "--porcelain") == ""
        project = await projects.create_project(name="Moved linked checkout", work_dir=str(old), pool=pool)
        if managed:
            await tasks.create_task(task_id="moving-task", requirement="Preserve linked checkout",
                                    project_id=project["id"], work_dir=str(linked), workspace_dir=str(repo),
                                    status="stopped", pool=pool)
            await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                               "VALUES ('moving-task',$1,$2,'looper/moving-task/task',$3)", str(repo), str(linked), base)
        source = str(old.resolve())
        result = await api.move_project_dir(project["id"], {"to": str(new), "link_old": managed})
        moved_linked = new if layout == "linked-source" else new / "linked"
        moved_main = repo if layout == "linked-source" else new / "repository"
        if managed:
            assert old.is_symlink() and old.resolve() == new.resolve()
        else:
            assert not old.exists()
        assert result["repairs"] and all(row["ok"] for row in result["repairs"])
        assert _git(moved_linked, "status", "--porcelain") == ""
        assert _git(moved_linked, "rev-parse", "HEAD") == base
        assert _git(moved_main, "status", "--porcelain") == ""
        registered = _git(moved_main, "worktree", "list", "--porcelain")
        assert str(moved_linked.resolve()) in registered
        assert source not in registered
        assert str(moved_main.resolve()) in (moved_linked / ".git").read_text()
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


def test_existing_database_adds_project_activity_idempotently(monkeypatch, tmp_path):
    from looper.db import connection

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await pool.execute("DROP TABLE project_activity")
        await pool.execute("DELETE FROM schema_migrations WHERE name='project_activity'")
        before = dict(await pool.fetchrow("SELECT * FROM projects WHERE id=$1", project["id"]))
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
            assert not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM schema_migrations "
                                           "WHERE name='project_activity')")
            assert await conn.fetchval("SELECT to_regclass('project_activity')") is None
        await connection.ensure_schema(pool)
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)
        assert dict(await pool.fetchrow("SELECT * FROM projects WHERE id=$1", project["id"])) == before
        assert (old / "subdir" / "kept.txt").read_text() == "keep these files\n"
        await api.move_project_dir(project["id"], {"to": str(new)})
        assert await pool.fetchval("SELECT count(*) FROM project_activity WHERE project_id=$1", project["id"]) == 1
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("mutation", ["none", "active-run", "destination", "destination-after-check", "project"])
def test_move_scans_before_transaction_and_rechecks_state(monkeypatch, tmp_path, mutation):
    from contextlib import asynccontextmanager
    from looper.db import connection, llm_records

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await tasks.create_task(task_id="stopped", requirement="Wait", project_id=project["id"],
                                work_dir=str(old), status="stopped", pool=pool)
        transaction = connection.transaction
        inventory, repositories = project_move._inventory, project_move._repositories
        calls = []
        in_transaction = False

        def scanned(kind, scan, directory):
            from looper.agents.scheduler import _project_firings

            assert not in_transaction
            assert project["id"] not in _project_firings or not _project_firings[project["id"]].deleting
            calls.append(kind)
            return scan(directory)

        @asynccontextmanager
        async def tracked_transaction(**kwargs):
            nonlocal in_transaction
            assert calls == ["inventory", "repositories"]
            if mutation == "active-run":
                await llm_records.start_record(agent_id="planner-stopped", task_id="stopped", pool=pool)
            elif mutation == "destination":
                new.mkdir()
            elif mutation == "project":
                await projects.update_project(project["id"], name="Changed", pool=pool)
            async with transaction(**kwargs) as conn:
                in_transaction = True
                try:
                    yield conn
                finally:
                    in_transaction = False

        monkeypatch.setattr(project_move, "_inventory", lambda directory: scanned("inventory", inventory, directory))
        monkeypatch.setattr(project_move, "_repositories", lambda directory: scanned("repositories", repositories, directory))
        monkeypatch.setattr(connection, "transaction", tracked_transaction)
        preview = project_move.preview

        async def preview_with_late_destination(*args, **kwargs):
            result = await preview(*args, **kwargs)
            if in_transaction and mutation == "destination-after-check":
                new.mkdir()
            return result

        monkeypatch.setattr(project_move, "preview", preview_with_late_destination)
        if mutation == "none":
            result = await api.move_project_dir(project["id"], {"to": str(new)})
            assert result["new_dir"] == str(new) and old.is_symlink()
        else:
            with pytest.raises(api.RequestError) as error:
                await api.move_project_dir(project["id"], {"to": str(new)})
            assert error.value.status == api.HTTP_409
            assert not old.is_symlink() and (old / "subdir" / "kept.txt").is_file()
            assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(old)
        assert calls == ["inventory", "repositories"]

    run_db(check, monkeypatch, timeout=30)


def test_move_without_destination_skips_existing_paths(monkeypatch, tmp_path):
    old, root = tmp_path / "old", tmp_path / "root"
    root.mkdir()
    (root / "example").mkdir()
    monkeypatch.setattr(api, "_root", str(root))

    async def check(pool):
        project = await _project(pool, old, name="Example")
        result = await api.move_project_dir(project["id"], {})
        assert result["new_dir"] == str(root / "example-2")
        assert (root / "example").is_dir() and not list((root / "example").iterdir())
        assert (root / "example-2" / "subdir" / "kept.txt").is_file()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("case", ["move", "occupied", "off", "failure", "same-key", "repo-root", "subdirectory"])
def test_move_operator_notes_after_commit(monkeypatch, tmp_path, case):
    old, new = tmp_path / "old", tmp_path / "new"
    if case == "subdirectory":
        repo = tmp_path / "repository"
        repo.mkdir()
        old = repo / "old"
    root = operator_notes.notes_root()

    async def check(pool):
        project = await _project(pool, old)
        if case in ("repo-root", "subdirectory"):
            repo = old if case == "repo-root" else old.parent
            _git(repo, "init", "-q")
        if case == "off":
            await projects.update_project(project["id"], execution={"operator_notes": "off"}, pool=pool)
        if case == "same-key":
            main = tmp_path / "main"
            main.mkdir()
            _git(main, "init", "-q")
            _git(main, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "Initial")
            _git(main, "worktree", "add", "-qb", "linked", str(old / "linked"))
            await projects.update_project(project["id"], work_dir=str(old / "linked"), pool=pool)
        source = Path((await projects.get_project(project["id"], pool=pool))["work_dir"])
        old_key = operator_notes.notes_key(str(source))
        old_notes = root / operator_notes.directory_name(old_key)
        new_notes = root / operator_notes.directory_name(str(new.resolve()))
        (old_notes / "memory").mkdir(parents=True)
        (old_notes / "memory" / "MEMORY.md").write_text("Keep operator knowledge")
        if case == "occupied":
            new_notes.mkdir()
            (new_notes / "keep.txt").write_text("Existing new notes")
        rename = os.rename
        loop = asyncio.get_running_loop()

        def rename_after_commit(src, dst, *args, **kwargs):
            if Path(src) == old_notes:
                assert new.is_dir() and source.is_symlink()
                saved = asyncio.run_coroutine_threadsafe(
                    projects.get_project(project["id"], pool=pool), loop).result(timeout=5)
                assert saved["work_dir"] == str(new), "notes moved before the database committed"
                if case == "failure":
                    raise OSError("notes on another filesystem")
            return rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(project_move.os, "rename", rename_after_commit)
        result = await api.move_project_dir(project["id"], {"to": str(new)})
        notes = result["notes"]
        assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
        summary = await pool.fetchval("SELECT summary FROM project_activity WHERE project_id=$1", project["id"])
        if case in ("move", "repo-root"):
            assert notes == {"moved": True, "from": old_notes.name, "to": new_notes.name}
            assert not old_notes.exists()
            assert "notes moved" in summary
            assert operator_notes.read_notes(str(new)).text.endswith("Keep operator knowledge")
        else:
            assert notes["moved"] is False
            assert (old_notes / "memory" / "MEMORY.md").read_text() == "Keep operator knowledge"
            assert notes["reason"] in summary
            if case == "occupied":
                assert notes["reason"] == f"{new_notes.name} already exists"
                assert (new_notes / "keep.txt").read_text() == "Existing new notes"
            elif case == "off":
                assert list(root.iterdir()) == [old_notes]
            elif case == "failure":
                assert "another filesystem" in notes["reason"]
            elif case in ("same-key", "subdirectory"):
                assert notes["reason"] == f"they belong to {old_key}, which does not move"
        assert (new / ("subdir/kept.txt" if case != "same-key" else ".git")).is_file()

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("registered", [False, True])
def test_move_does_not_repair_submodule_as_linked_worktree(monkeypatch, tmp_path, registered):
    from looper.agents import step_workspaces

    old, new, module = tmp_path / "old", tmp_path / "new", tmp_path / "module"

    async def check(pool):
        project = await _project(pool, old)
        module.mkdir()
        _git(module, "init", "-q")
        _git(module, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-qm", "Module")
        _git(old, "init", "-q")
        _git(old, "-c", "protocol.file.allow=always", "submodule", "add", str(module), "child")
        marker = (old / "child" / ".git").read_text()
        assert marker.strip() == "gitdir: ../.git/modules/child"
        assert _git(old / "child", "status", "--porcelain") == ""
        if registered:
            await tasks.create_task(task_id="module-task", requirement="Preserve checkout", project_id=project["id"],
                                    work_dir=str(old / "child"), status="stopped", pool=pool)
            await pool.execute("INSERT INTO task_worktrees (task_id,repo_dir,path,branch,base_commit) "
                               "VALUES ('module-task',$1,$2,'module','base')", str(old), str(old / "child"))
        git = step_workspaces._git
        repairs = []

        async def tracked_git(repo, *args, **kwargs):
            repairs.append((Path(repo), args))
            return await git(repo, *args, **kwargs)

        monkeypatch.setattr(step_workspaces, "_git", tracked_git)
        result = await api.move_project_dir(project["id"], {"to": str(new)})
        assert result["repairs"] and all(row["ok"] for row in result["repairs"])
        assert (new, ("worktree", "repair")) in repairs
        assert all(str(new / "child") not in args[2:] for _, args in repairs)
        assert (new / "child" / ".git").read_text() == marker
        assert _git(new / "child", "status", "--porcelain") == ""

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("link_old", [False, True])
@pytest.mark.parametrize("module_name", ["child", "worktrees/child"])
def test_move_refuses_submodule_checkout_preserving_files_and_rows(monkeypatch, tmp_path, link_old, module_name):
    superproject, module = tmp_path / "superproject", tmp_path / "module"
    old, new = superproject / module_name, tmp_path / "new"
    for repo in (superproject, module):
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-qm", "Initial")
    _git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", str(module), module_name)
    (old / "kept.txt").write_text("Keep uncommitted files")
    marker = (old / ".git").read_text()

    async def check(pool):
        project = await projects.create_project(name="Submodule", work_dir=str(old), pool=pool)
        await _schedule(pool, project["id"], old)
        before = await _rows(pool)
        for operation in (lambda: api.preview_project_move(project, {"to": str(new)}, db=pool),
                          lambda: api.move_project_dir(project["id"], {"to": str(new), "link_old": link_old})):
            with pytest.raises(api.RequestError) as error:
                await operation()
            assert error.value.status == api.HTTP_400
            assert str(error.value) == "the project's directory is a git submodule checkout; move the superproject instead"
            assert await _rows(pool) == before
            assert not new.exists() and not old.is_symlink()
            assert (old / ".git").read_text() == marker
            assert (old / "kept.txt").read_text() == "Keep uncommitted files"
            assert _git(old, "rev-parse", "--show-toplevel") == str(old.resolve())

    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["start", "restart"])
def test_task_start_during_move_waits_and_runs_after_commit(monkeypatch, tmp_path, action):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from looper.agents import scheduler
    from looper.agents.bus import MessageBus
    from looper.agents.message import MANAGER_ADDRESS, TASK_ROUTER_ADDRESS, MessageType, planner_address
    from looper.agents.tiers.planner import PlannerAgent
    from looper.agents.tiers.task import TaskRouter
    from looper.db import connection

    old, new = tmp_path / "old", tmp_path / "new"

    async def check(pool):
        project = await _project(pool, old)
        await tasks.create_task(task_id="waiting-task", requirement="Run once", project_id=project["id"],
                                work_dir=str(old), status=tasks.READY if action == "start" else "stopped", pool=pool)
        router = TaskRouter(runtime_root=tmp_path / "runtime")
        bus = MessageBus(runtime_root=tmp_path / "runtime")
        router._bus = bus
        monkeypatch.setattr(PlannerAgent, "start", AsyncMock())
        monkeypatch.setattr(PlannerAgent, "stop", AsyncMock())
        transaction = connection.transaction
        pending = None

        @asynccontextmanager
        async def transaction_during_move(**kwargs):
            nonlocal pending
            assert scheduler._project_firings[project["id"]].deleting
            if action == "start":
                answer = await api.start_ready_task("waiting-task", bus, MANAGER_ADDRESS)
                assert answer["started"] == "waiting-task"
            else:
                await api.control_task("waiting-task", "restart", bus, MANAGER_ADDRESS)
            envelope = await bus.get_queue(TASK_ROUTER_ADDRESS.agent_id).get()
            pending = asyncio.create_task((router._handle_start if action == "start" else router._handle_restart)(envelope))
            # Drain the task snapshot read so the request has actually reached the barrier.
            entered = asyncio.Event()
            get_task = tasks.get_task

            async def snapshot(*args, **kwargs):
                row = await get_task(*args, **kwargs)
                entered.set()
                return row

            monkeypatch.setattr(tasks, "get_task", snapshot)
            await entered.wait()
            await asyncio.sleep(0)
            assert not pending.done(), "the Router dropped an acknowledged request"
            PlannerAgent.start.assert_not_awaited()
            async with transaction(**kwargs) as conn:
                yield conn

        async def check_repair(*args):
            assert project["id"] not in scheduler._project_firings or not scheduler._project_firings[project["id"]].deleting
            await pending
            PlannerAgent.start.assert_awaited_once()
            request = await bus.get_queue(planner_address("waiting-task").agent_id).get()
            assert request.message_type is MessageType.TASK_REQUEST
            assert request.payload["work_dir"] == str(old) and old.resolve() == new.resolve()
            assert old.is_symlink() and (new / "subdir" / "kept.txt").exists()
            assert (await projects.get_project(project["id"], pool=pool))["work_dir"] == str(new)
            assert (await tasks.get_task("waiting-task", pool=pool))["status"] in tasks.ACTIVE_STATUSES
            return []

        monkeypatch.setattr(connection, "transaction", transaction_during_move)
        monkeypatch.setattr(project_move, "_repair", check_repair)
        try:
            await api.move_project_dir(project["id"], {"to": str(new)})
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await router.stop()
            await bus.shutdown()

    run_db(check, monkeypatch, timeout=30)


def test_schedule_gate_completing_during_barrier_waits_instead_of_failing(monkeypatch, tmp_path):
    from looper.agents import scheduler
    from looper.agents.schedule_gate import GateResult
    from looper.db import schedules as db_schedules
    from tests.schedule_support import run_scheduled

    async def check(env):
        started, finish, completing = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run_gate(*args, **kwargs):
            started.set()
            await finish.wait()
            completing.set()
            return GateResult(0, "claimed input", "")

        monkeypatch.setattr(scheduler, "run_gate", run_gate)
        schedule = await env.enabled(gate_command="printf 'claimed input'")
        run_id = await env.scheduler.run_now(schedule["schedule_id"])
        await asyncio.wait_for(started.wait(), 5)
        async with scheduler.quiesce_project(env.project_id, kind=scheduler.ProjectQuiescence.MOVE):
            finish.set()
            await asyncio.wait_for(completing.wait(), 5)
            # Give completion a chance to acquire a connection and update the run on the old path.
            await asyncio.sleep(0.05)
            run = await db_schedules.get_run(run_id)
            assert run["status"] == "gating", run
            assert not env.bus.of("task_start")
        await asyncio.wait_for(env.scheduler.idle(), 5)
        run = await db_schedules.get_run(run_id)
        assert run["status"] == "running" and run["error"] is None, run
        assert run["task_id"] in env.bus.of("task_start")
        assert "claimed input" in (await tasks.get_task(run["task_id"]))["requirement"]

    run_scheduled(check, monkeypatch, tmp_path)
