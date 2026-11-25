"""Migration entry points use only synthetic documents and a disposable git vault."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from looper.agents import knowledge_migrate, knowledge_commands
from looper.db import projects
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _vault(tmp_path):
    root = (tmp_path / "vault").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Example")
    _git(root, "config", "user.email", "example@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.hooksPath", str(root / ".git/hooks"))
    (root / "index.md").write_text("Synthetic vault.\n")
    _git(root, "add", "--", "index.md")
    _git(root, "commit", "-q", "-m", "Initial vault")
    return root


async def _seed(pool, tmp_path, *, setting=True, directory="projects/example"):
    root = _vault(tmp_path)
    project = await projects.create_project(name="Example", pool=pool)
    await pool.execute("UPDATE projects SET knowledge_dir=$2 WHERE id=$1", project["id"], directory)
    if setting:
        await pool.execute("INSERT INTO system_settings(key,value) VALUES('knowledge_vault',$1)", json.dumps({
            "path": str(root), "writable": [], "rules_file": None,
            "worktree_root": str((tmp_path / "worktrees").resolve())}))
    for kind, identity, filename, content, path in (
        ("text", "doc-text", "guide.md", "Original.\n", None),
        ("upload", "doc-upload", "large.txt", "x" * 9000, None),
        ("path", "doc-path", "", None, "docs/reference.md"),
    ):
        await pool.execute("""INSERT INTO project_documents
            (id,project_id,title,summary,source_kind,filename,content,path,size_bytes,sha256)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""", identity, project["id"], f"{kind} guide",
            f"About {kind}", kind, filename, content, path, len((content or "Reference").encode()),
            hashlib.sha256((content or "Reference").encode()).hexdigest())
    return root, project


def test_default_cli_preview_does_not_write_and_reports_all_sources(tmp_path, monkeypatch):
    async def run(pool):
        root, _ = await _seed(pool, tmp_path)
        before = _git(root, "rev-parse", "HEAD")
        args = SimpleNamespace(project="example", execute=False, runtime_root_path=tmp_path / "runtime")
        code, text = await knowledge_commands._command(pool, args)
        result = json.loads(text)
        assert code == 0 and result["state"] == "dry_run"
        assert len(result["files"]) == 3 and sum(item["over_8kb"] for item in result["files"]) == 1
        assert result["files"][2]["reference"] is True
        assert not (root / "projects").exists() and not (tmp_path / "runtime").exists()
        assert _git(root, "rev-parse", "HEAD") == before and _git(root, "status", "--porcelain") == ""
    run_db(run, monkeypatch)


def test_execute_exports_backup_then_commits_once_and_verifies_every_file(tmp_path, monkeypatch):
    async def run(pool):
        root, project = await _seed(pool, tmp_path)
        before = [dict(row) for row in await pool.fetch("SELECT * FROM project_documents ORDER BY id")]
        backup_events = []

        def on_backup(report):
            data = Path(report["path"]).read_bytes()
            assert report["sha256"] == hashlib.sha256(data).hexdigest()
            exported = {row["id"]: row for row in json.loads(data)["documents"]}
            assert len(exported) == 3 and not (root / "projects").exists()
            for row in before:
                assert exported[row["id"]] == {
                    key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}
            backup_events.append(report)

        result = await knowledge_migrate.migrate(pool, project["name"], tmp_path / "runtime", execute=True,
                                                 on_backup=on_backup)
        assert result["state"] == "committed" and len(backup_events) == 1
        assert result["backup"] == backup_events[0]
        assert Path(result["backup"]["path"]).stat().st_mode & 0o777 == 0o600
        assert _git(root, "rev-list", "--count", "HEAD") == "2"
        assert "project_documents" in _git(root, "show", "-s", "--format=%B")
        for item in result["files"]:
            assert item["verified"] is True
            assert hashlib.sha256((root / item["target"]).read_bytes()).hexdigest() == item["sha256"]
        assert [dict(row) for row in await pool.fetch("SELECT * FROM project_documents ORDER BY id")] == before
        repeated = await knowledge_migrate.migrate(pool, "Example", tmp_path / "runtime", execute=True)
        assert repeated["state"] == "unchanged" and all(item["verified"] for item in repeated["files"])
        assert _git(root, "rev-list", "--count", "HEAD") == "2"
    run_db(run, monkeypatch)


@pytest.mark.parametrize("problem", ["missing_vault", "missing_directory", "missing_project"])
def test_missing_configuration_refuses_before_any_backup(tmp_path, monkeypatch, problem):
    async def run(pool):
        root, _ = await _seed(pool, tmp_path, setting=problem != "missing_vault",
                              directory=None if problem == "missing_directory" else "projects/example")
        with pytest.raises(ValueError, match={"missing_vault": "vault", "missing_directory": "knowledge_dir",
                                              "missing_project": "project"}[problem]):
            await knowledge_migrate.migrate(pool, "Missing" if problem == "missing_project" else "Example",
                                           tmp_path / "runtime", execute=True)
        assert not (tmp_path / "runtime").exists() and _git(root, "rev-list", "--count", "HEAD") == "1"
    run_db(run, monkeypatch)


def test_execute_reports_all_conflicting_files_without_overwriting_or_backup(tmp_path, monkeypatch):
    async def run(pool):
        root, _ = await _seed(pool, tmp_path)
        preview = await knowledge_migrate.migrate(pool, "Example", tmp_path / "runtime")
        targets = preview["files"][:2]
        for item in targets:
            path = root / item["target"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Operator edit.\n")
        result = await knowledge_migrate.migrate(pool, "Example", tmp_path / "runtime", execute=True)
        assert result["state"] == "refused" and result["conflicts"] == [item["target"] for item in targets]
        assert not (tmp_path / "runtime").exists()
        assert all((root / item["target"]).read_text() == "Operator edit.\n" for item in targets)
        assert _git(root, "rev-list", "--count", "HEAD") == "1"
    run_db(run, monkeypatch)


def test_actual_cli_uses_the_selected_database_for_preview_and_execute(tmp_path, monkeypatch):
    import asyncio
    import os
    import sys
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    async def run(pool):
        root, _ = await _seed(pool, tmp_path)
        schema = await pool.fetchval("SELECT current_schema()")
        url = urlunsplit(urlsplit(DSN)._replace(query=urlencode([
            *parse_qsl(urlsplit(DSN).query), ("search_path", schema)])))
        config = tmp_path / "server.toml"
        config.write_text(f"[database]\nurl = {json.dumps(url)}\n")
        config.chmod(0o600)
        argv = [sys.executable, "-m", "looper.cli", "knowledge", "migrate", "--project", "Example",
                "--config", str(config), "--runtime-root", str(tmp_path / "runtime")]
        env = {key: value for key, value in os.environ.items() if key != "LOOPER_AGENT_RUN"}
        preview = await asyncio.to_thread(subprocess.run, argv, env=env, capture_output=True, text=True, timeout=30)
        assert preview.returncode == 0, preview.stderr
        assert json.loads(preview.stdout)["state"] == "dry_run" and not (root / "projects").exists()
        executed = await asyncio.to_thread(subprocess.run, [*argv, "--execute"], env=env, capture_output=True,
                                           text=True, timeout=30)
        assert executed.returncode == 0, executed.stderr
        result = json.loads(executed.stdout)
        assert result["state"] == "committed" and all(item["verified"] for item in result["files"])
        assert result["backup"]["sha256"] in executed.stderr and "backup:" in executed.stderr
    run_db(run, monkeypatch)


def test_readback_reports_a_file_changed_after_the_merge(tmp_path, monkeypatch):
    from looper.agents import vault_commit

    async def run(pool):
        root, _ = await _seed(pool, tmp_path)
        original = vault_commit.commit
        changed = []

        async def edited_after_merge(config, files, message, **kwargs):
            result = await original(config, files, message, **kwargs)
            path = next(iter(files))
            (root / path).write_text("Concurrent editor.\n")
            changed.append(path)
            return result

        monkeypatch.setattr(vault_commit, "commit", edited_after_merge)
        result = await knowledge_migrate.migrate(pool, "Example", tmp_path / "runtime", execute=True)
        assert result["state"] == "verification_failed"
        assert [item["target"] for item in result["files"] if not item["verified"]] == changed
        assert sum(item["verified"] for item in result["files"]) == 2
    run_db(run, monkeypatch)
