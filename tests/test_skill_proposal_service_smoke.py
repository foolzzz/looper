"""Isolated serve: a Planner proposes, the dashboard confirms, and later runs load the skill."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from urllib.parse import urlsplit

import asyncpg
import pytest

from evals.prompt_regression import runner
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_serve_delivers_a_confirmed_proposal_to_the_next_task_and_accepts_a_manager_proposal(tmp_path, monkeypatch):
    database = urlsplit(str(DSN))
    assert database.hostname in {"127.0.0.1", "localhost"} and database.port in {54331, 5432}
    assert database.path.lstrip("/") not in {"", "looper", "loopx"}
    runtime, home, bin_dir, work = (tmp_path / name for name in ("runtime", "home", "bin", "work"))
    home.mkdir()
    work.mkdir()
    proposal = {"action": "create", "name": "release-check", "description": "Use before a project release.",
                "content": "# Release check\n1. Run the tests.\n2. Read the result before publishing.\n",
                "why": "The same release checks were repeated and apply to future releases."}
    scenario = {"plan": [{"id": "s1", "instruction": "Record the release checks.", "review": False,
                           "acceptance_criteria": ["The release checks are recorded."], "depends_on": []}],
                "attempts": {"s1": [{"status": "completed", "files": {"checks.txt": "Tests, then results.\n"}}]},
                "skill_proposals": [proposal], "skill_proposals_step": "s1"}
    scripts = {str(work.resolve()): scenario}
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(json.dumps(scripts))
    fake = runner.install_claude(bin_dir, mode="fake", scenarios=scenarios)
    assert fake is not None
    wrapper = fake.read_text().removeprefix("#!/bin/sh\n")
    fake.write_text("#!/bin/sh\n"
                    "if [ \"$1\" = '--version' ]; then echo '2.1.285 (fixture runtime)'; exit 0; fi\n"
                    "if [ \"$1 $2\" = 'auth status' ]; then echo '{\"loggedIn\": true}'; exit 0; fi\n" + wrapper)
    for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "lark-cli", "coco"):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\nimport sys\n"
                              "if '--version' in sys.argv: print('fixture runtime'); sys.exit(0)\n"
                              "print('No external login in this fixture', file=sys.stderr)\nsys.exit(1)\n")
        executable.chmod(0o755)
    environment = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
                   "GROK_HOME": str(home / ".grok"), "CURSOR_CONFIG_DIR": str(home / ".cursor-config"),
                   "CURSOR_DATA_DIR": str(home / ".cursor-data"), "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "LOOPER_TELEGRAM_BOT_TOKEN": "",
                   "LOOPER_EVAL_REAL_CLAUDE": ""}
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
                        "sys.exit(main(['serve', '--host', '127.0.0.1', '--port', sys.argv[1], "
                        "'--runtime-root', sys.argv[2]]))\n")

    def finish(api, task_id):
        deadline = time.monotonic() + 60
        while True:
            detail = api.post("/api/v1/tasks/detail", {"task_id": task_id})["result"]
            if detail["status"] in {"completed", "failed", "stopped", "cancelled"}:
                assert detail["status"] == "completed", detail
                return
            assert time.monotonic() < deadline, "scripted task did not finish"
            time.sleep(.1)

    with fresh_database_without_recording(str(DSN)) as (url, _):
        async def query(sql, *arguments):
            conn = await asyncpg.connect(url, timeout=10, command_timeout=15)
            try:
                return [dict(row) for row in await conn.fetch(sql, *arguments)]
            finally:
                await conn.close()

        with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                                   notes_root=home / "notes", env=environment, startup_timeout=60,
                                   log_path=tmp_path / "serve.log") as api:
            project_id = api.post("/api/v1/project_admin/create", {
                "name": "Skill proposal fixture", "work_dir": str(work)})["result"]["project"]["project_id"]
            api.post("/api/v1/project_admin/update", {"project_id": project_id,
                     "fields": {"task_workspaces": "in_place", "operator_notes": "off", "agent_room": "off"}})
            api.post("/api/v1/project_skills/proposals/set_mode", {"project_id": project_id, "mode": "on"})

            def start(requirement):
                task_id = api.post("/api/v1/tasks/create", {
                    "project_id": project_id, "requirement": requirement})["result"]["task_id"]
                api.post("/api/v1/ops/task_control/start", {"task_id": task_id})
                finish(api, task_id)
                return task_id

            first = start("Record the repeated release procedure.")
            pending, = api.post("/api/v1/project_skills/proposals/list", {
                "project_id": project_id})["result"]["proposals"]
            assert pending["status"] == "pending" and pending["name"] == "release-check"
            assert pending["evidence"] == [{"task": first, "step": "s1"}]
            assert asyncio.run(query("SELECT via FROM skill_proposal_events WHERE proposal_id=$1 "
                                    "AND action='propose'", pending["proposal_id"])) == [{"via": "planner"}]
            source, = asyncio.run(query("SELECT r.agent_id FROM skill_proposal_events e JOIN llm_records r "
                                        "ON r.id=e.record_id WHERE e.proposal_id=$1 AND r.task_id=$2",
                                        pending["proposal_id"], first))
            assert source["agent_id"] == f"planner-{first}"
            activity = asyncio.run(query("SELECT summary FROM activity WHERE task_id=$1", first))
            assert any("Skill proposal" in row["summary"] and "created" in row["summary"] for row in activity)
            assert asyncio.run(query("SELECT id FROM library_items WHERE owner_project_id=$1", project_id)) == []
            # The dashboard's authenticated operation binds confirmation to the content the operator saw.
            confirmed = api.post("/api/v1/project_skills/proposals/confirm", {
                "project_id": project_id, "proposal_id": pending["proposal_id"],
                "content_sha256": pending["content_sha256"]})["result"]
            assert confirmed["proposal"]["status"] == "confirmed"

            scenario.pop("skill_proposals")
            scenarios.write_text(json.dumps(scripts))
            second = start("Use the project release procedure in another task.")
            records = asyncio.run(query("SELECT agent_id, input_text, metadata FROM llm_records "
                                        "WHERE task_id=$1 ORDER BY id", second))
            plan_turns = [row for row in records if row["agent_id"].startswith("planner-")
                          and "[PLAN]" in (row["input_text"] or "")]
            assert len(plan_turns) == 1
            assert f"skill proposal p-{pending['proposal_id']} release-check: confirmed" in plan_turns[0]["input_text"]
            for kind in ("planner-", "worker-"):
                runs = [json.loads(row["metadata"]) for row in records if row["agent_id"].startswith(kind)]
                assert runs and all(any(item["name"] == "release-check" and item["sha256"] ==
                                        pending["content_sha256"] for item in run["library"]["items"]) for run in runs)

            message = "Turn the repeated project checklist into a skill proposal."
            scripts["manager"] = {message: [["looper_skill_propose", {
                **proposal, "project": "Skill proposal fixture", "name": "manager-check", "evidence": [{"task": first, "step": "s1"}]}]]}
            scenarios.write_text(json.dumps(scripts))
            api.post("/api/v1/operator_chat/send", {"text": message, "session_id": "skill-proposal-fixture",
                                                  "message_id": "propose-manager"})
            deadline = time.monotonic() + 30
            while True:
                pending = api.post("/api/v1/project_skills/proposals/list", {"project_id": project_id})["result"]["proposals"]
                if pending:
                    break
                assert time.monotonic() < deadline, "Manager did not create the proposal"
                time.sleep(.1)
            manager, = pending
            assert manager["name"] == "manager-check" and manager["status"] == "pending"
            events = asyncio.run(query("SELECT via FROM skill_proposal_events WHERE proposal_id=$1 "
                                       "AND action='propose'", manager["proposal_id"]))
            assert events == [{"via": "manager"}]
            assert len(asyncio.run(query("SELECT id FROM library_items WHERE owner_project_id=$1", project_id))) == 1
