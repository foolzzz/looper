"""Public serve → failed check → Worker retry → measured review → acceptance and restart readback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from urllib.parse import urlsplit

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.db import step_check_runs
from tests.db_support import DSN, fresh_database_without_recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


def test_serve_gates_acceptance_and_delivers_retry_and_reviewer_evidence(tmp_path, monkeypatch):
    # The task card's isolated local server or CI's PostgreSQL, never the operator's database.
    database = urlsplit(str(DSN))
    assert database.hostname in {"127.0.0.1", "localhost"} and database.port in {54331, 5432}
    assert database.path.lstrip("/") not in {"", "looper", "loopx"}
    runtime, home, bin_dir, work = (tmp_path / name for name in ("runtime", "home", "bin", "work"))
    for directory in (home, work):
        directory.mkdir()
    for arguments in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture User"),
                      ("config", "user.email", "fixture@example.invalid")):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *arguments], cwd=work, check=True,
                       capture_output=True)
    (work / "baseline.txt").write_text("original source\n")
    subprocess.run(["git", "-C", str(work), "add", "baseline.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                    "commit", "-qm", "Fixture baseline"], check=True, capture_output=True)
    checks = [
        {"id": "command", "criterion": 1, "type": "command", "command":
         "if test \"$(cat delivered.txt)\" = 'fixture delivered'; then printf 'measured success\\n'; "
         "else printf 'measured failure\\n'; exit 1; fi",
         "timeout_seconds": 10, "network": True},
        {"id": "report", "criterion": 2, "type": "artifact",
         "command": "printf 'acceptance artifact\\n' > acceptance-report.txt", "artifact": ["acceptance-report.txt"],
         "timeout_seconds": 10, "network": True},
        {"id": "fenced", "criterion": 1, "type": "command", "command": "touch must-not-run", "required": False},
    ]
    script = {str(work.resolve()): {
        "plan": [{"id": "s1", "instruction": "Create the fixture deliverable.",
                  "acceptance_criteria": ["The command measures its result.", "A report is saved."],
                  "depends_on": [], "review": True, "checks": checks}],
        "attempts": {"s1": [
            {"status": "completed", "files": {"delivered.txt": "fixture incomplete\n"}},
            {"status": "completed", "files": {"delivered.txt": "fixture delivered\n"}}]},
    }}
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(json.dumps(script))
    fake = runner.install_claude(bin_dir, mode="fake", scenarios=scenarios)
    assert fake is not None
    # Probe/auth calls are deterministic too. Every prompt is handled by the shared fake CLI.
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
    environment = {
        "CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
        "GROK_HOME": str(home / ".grok"), "CURSOR_CONFIG_DIR": str(home / ".cursor-config"),
        "CURSOR_DATA_DIR": str(home / ".cursor-data"), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"), "LOOPER_TELEGRAM_BOT_TOKEN": "",
        "LOOPER_EVAL_REAL_CLAUDE": "",  # even an inherited real-mode setting cannot escape the fixture
    }
    monkeypatch.setattr(runner, "_SERVE", "import sys\nfrom looper.cli import main\n"
        "from looper.agents import proc_tree\n"
        "spawn = proc_tree.spawn\n"
        "async def fenced_spawn(*args, **kwargs):\n"
        "    if args[0].endswith(('sandbox-exec', 'unshare')):\n"
        "        raise RuntimeError('fixture control plane fenced')\n"
        "    return await spawn(*args, **kwargs)\n"
        "proc_tree.spawn = fenced_spawn\n"
        "sys.exit(main(['serve', '--host', '127.0.0.1', '--port', sys.argv[1], '--runtime-root', sys.argv[2]]))\n")
    with fresh_database_without_recording(str(DSN)) as (url, _):
        server = dict(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                      notes_root=home / "notes", env=environment, startup_timeout=60)
        with runner.running_server(**server, log_path=tmp_path / "before.log") as api:
            owner = api.post("/api/v1/project_admin/create", {
                "name": "Acceptance fixture", "work_dir": str(work)})["result"]["project"]["project_id"]
            api.post("/api/v1/project_admin/update", {"project_id": owner,
                     "fields": {"task_workspaces": "in_place", "operator_notes": "off", "agent_room": "off"}})
            permission = api.post("/api/v1/acceptance_checks/update", {
                "project_id": owner, "acceptance_checks": "network"})
            assert permission["result"] == {"acceptance_checks": "network"}
            task_id = api.post("/api/v1/tasks/create", {
                "project_id": owner, "requirement": "Create the fixture deliverable and record check evidence."})[
                    "result"]["task_id"]
            api.post("/api/v1/ops/task_control/start", {"task_id": task_id})
            deadline = time.monotonic() + 90
            while True:
                detail = api.post("/api/v1/tasks/detail", {"task_id": task_id})["result"]
                if detail["status"] in {"completed", "failed", "stopped", "cancelled"}:
                    break
                assert time.monotonic() < deadline, "scripted task did not finish"
                time.sleep(0.1)
            assert detail["status"] == "completed", detail
            evidence = api.post("/api/v1/task_evidence/get", {"task": task_id, "step": "s1"})["result"]
            saved_step, = evidence["steps"]
            first_command, second_command = [row for row in saved_step["runs"] if row["check_id"] == "command"]
            assert (first_command["attempt"], first_command["state"], first_command["exit_code"]) == (1, "failed", 1)
            assert first_command["output_tail"] == "measured failure\n"
            assert (second_command["attempt"], second_command["state"], second_command["exit_code"]) == (2, "passed", 0)
            measured = {row["check_id"]: row for row in saved_step["runs"]}
            assert set(measured) == {"command", "report", "fenced"}
            assert measured["command"]["output_tail"] == "measured success\n"
            assert measured["report"]["state"] == "passed"
            assert (measured["fenced"]["state"], measured["fenced"]["error_kind"]) == ("error", "evidence_unavailable")
            assert measured["fenced"]["finished_at"] is not None
            assert not (work / "must-not-run").exists()
            assert all(row["guarded"] and row["head_sha"] and row["content_sha"] for row in measured.values())
            artifact, = measured["report"]["artifacts"]
            assert artifact == {"name": "1.txt", "mime": "text/plain", "bytes": 20,
                                "sha256": hashlib.sha256(b"acceptance artifact\n").hexdigest()}
            assert "path" not in artifact
            files = list((runtime / "evidence" / task_id).rglob("1.txt"))
            assert len(files) == 2 and all(path.read_bytes() == b"acceptance artifact\n" for path in files)
            assert not (work / "acceptance-report.txt").exists()  # generated in the private checkout

            async def recorded_prompts():
                conn = await asyncpg.connect(url)
                try:
                    rows = await conn.fetch("SELECT run_kind, metadata FROM llm_records WHERE task_id=$1 "
                        "AND run_kind IN ('worker', 'reviewer') ORDER BY id LIMIT 10", task_id)
                    return [(row["run_kind"], json.loads(row["metadata"])["system_prompt"]) for row in rows]
                finally:
                    await conn.close()

            async def settled_attempts():
                conn = await asyncpg.connect(url)
                try:
                    return [(row["attempt"], row["decision"], row["failure_kind"]) for row in await conn.fetch(
                        "SELECT attempt, decision, failure_kind FROM step_attempts WHERE task_id=$1 "
                        "AND step_key='s1' ORDER BY attempt", task_id)]
                finally:
                    await conn.close()

            # The measured failure marks the first attempt; the accepted one has no failure.
            assert asyncio.run(settled_attempts()) == [(1, "retry", "check_failed"), (2, "accept", None)]
            prompts = asyncio.run(recorded_prompts())
            workers = [prompt for kind, prompt in prompts if kind == "worker"]
            reviews = [prompt for kind, prompt in prompts if kind == "reviewer"]
            assert len(workers) == 2 and len(reviews) == 1
            assert "Failed required acceptance checks:" not in workers[0]
            assert "Failed required acceptance checks:" in workers[1]
            assert "measured failure" in workers[1] and "Command:" in workers[1]
            assert 'source="acceptance_output"' in workers[1]
            assert "Acceptance evidence" in reviews[0] and "command command: PASSED" in reviews[0]
            assert "measured success" in reviews[0] and 'source="acceptance_output"' in reviews[0]
            before = saved_step["runs"]
            api.post("/api/v1/acceptance_checks/update", {"project_id": owner, "acceptance_checks": "off"})
            disabled = api.post("/api/v1/task_evidence/get", {"task": task_id, "step": "s1"})["result"]["steps"][0]
            assert disabled == saved_step  # permission changes retain definitions and completed evidence

        # A prior stopped process's running row must settle at startup, without rewriting terminal history.
        async def unfinished():
            pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
            try:
                return await step_check_runs.create_run(task_id, saved_step["step_id"], checks[0], 2, pool=pool)
            finally:
                await pool.close()

        pending = asyncio.run(unfinished())
        with runner.running_server(**server, log_path=tmp_path / "after.log") as api:
            after = api.post("/api/v1/task_evidence/get", {"task": task_id, "step": "s1"})["result"]["steps"][0]["runs"]
            assert after[:len(before)] == before  # completed attempts retain the same ids, timestamps and bytes
            assert len(after) == len(before) + 1 and after[-1]["id"] == pending["id"]
            assert after[-1]["state"] == "error" and after[-1]["error_kind"] == "interrupted"
            assert all(path.read_bytes() == b"acceptance artifact\n" for path in files)


@pytest.mark.parametrize("ending", ["completed", "failed", "disabled"])
def test_serve_persists_result_evidence_and_code_notice(tmp_path, monkeypatch, ending):
    with _result_server(tmp_path, monkeypatch) as (api, url, work):
        detail = _run_result_task(api, url, work, ending)
        summary = detail["result_summary"] if ending != "failed" else detail["error"]
        assert "## Evidence\n- s1 c1 (criterion 1): " in summary
        if ending == "failed":
            assert summary.index("## Why it ended") < summary.index("## Evidence")
            assert ": failed at " in summary
        else:
            headings = [summary.index("## " + title) for title in
                        ("Delivered", "How to use", "Verification", "Open issues", "Evidence")]
            assert headings == sorted(headings)
            assert ": passed at " in summary
            appendix = detail["process_record"]["delivery_summary"]
            assert summary.endswith(appendix)
            assert summary.index("## Evidence") < summary.index(appendix)
        notice_path = tmp_path / "notices.jsonl"
        deadline = time.monotonic() + 10
        while not notice_path.exists():
            assert time.monotonic() < deadline, "code notice was not published"
            time.sleep(0.05)
        notices = [json.loads(line) for line in notice_path.read_text().splitlines()]
        notice, = [item for item in notices if item["chat"] == "fixture-recipient"]
        assert f"Checks: 1 {'failed' if ending == 'failed' else 'passed'}" in notice["text"]
        assert "## Evidence" not in notice["text"] and "c1 (criterion" not in notice["text"]
        assert "private-output-marker" not in notice["text"]


def _result_server(tmp_path, monkeypatch, *, mode="network"):
    """An isolated serve fixture; the only channel endpoint is a recorded bus envelope."""
    from contextlib import contextmanager

    @contextmanager
    def serve():
        runtime, home, bin_dir, work = (tmp_path / name for name in ("runtime", "home", "bin", "work"))
        home.mkdir()
        work.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Fixture User"),
                     ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=work, check=True,
                           capture_output=True)
        (work / "baseline.txt").write_text("original source\n")
        subprocess.run(["git", "add", "baseline.txt"], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                        "commit", "-qm", "Fixture baseline"], cwd=work, check=True, capture_output=True)
        checks = [{"id": "c1", "criterion": 1, "type": "command", "command":
                   "printf 'private-output-marker\\n'; test \"$(cat delivered.txt)\" = delivered",
                   "timeout_seconds": 10, **({"network": True} if mode == "network" else {})}]
        scenario = {"plan": [{"id": "s1", "instruction": "Write the fixture deliverable.",
            "acceptance_criteria": ["The file contains delivered."], "depends_on": [], "review": False,
            "checks": checks}], "attempts": {"s1": [{"status": "completed",
                "files": {"delivered.txt": "delivered\n"}}]}}
        scenarios = tmp_path / "scenarios.json"
        scenarios.write_text(json.dumps({str(work.resolve()): scenario}))
        fake = runner.install_claude(bin_dir, mode="fake", scenarios=scenarios)
        wrapper = fake.read_text().removeprefix("#!/bin/sh\n")
        fake.write_text("#!/bin/sh\n"
            "if [ \"$1\" = '--version' ]; then echo '2.1.285 (fixture runtime)'; exit 0; fi\n"
            "if [ \"$1 $2\" = 'auth status' ]; then echo '{\"loggedIn\": true}'; exit 0; fi\n" + wrapper)
        for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "lark-cli", "coco"):
            executable = bin_dir / name
            executable.write_text(f"#!{sys.executable}\nimport sys\n"
                "if '--version' in sys.argv: print('fixture runtime'); sys.exit(0)\n"
                "sys.exit(1)\n")
            executable.chmod(0o755)
        monkeypatch.setattr(runner, "_SERVE", "import sys, json\nfrom pathlib import Path\n"
            "from looper.cli import main\nfrom looper.agents.bus import MessageBus\n"
            "from looper.agents.message import MessageType\n"
            "publish = MessageBus.publish\n"
            "async def recorded(self, envelope):\n"
            "    if envelope.message_type == MessageType.CHAT_NOTICE:\n"
            "        with (Path(sys.argv[2]).parent / 'notices.jsonl').open('a') as out:\n"
            "            out.write(json.dumps(envelope.payload) + '\\n')\n"
            "    return await publish(self, envelope)\n"
            "MessageBus.publish = recorded\n"
            "sys.exit(main(['serve', '--host', '127.0.0.1', '--port', sys.argv[1], '--runtime-root', sys.argv[2]]))\n")
        environment = {"CLAUDE_CONFIG_DIR": str(home / ".claude"), "CODEX_HOME": str(home / ".codex"),
            "GROK_HOME": str(home / ".grok"), "CURSOR_CONFIG_DIR": str(home / ".cursor-config"),
            "CURSOR_DATA_DIR": str(home / ".cursor-data"), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"), "LOOPER_TELEGRAM_BOT_TOKEN": "", "LOOPER_EVAL_REAL_CLAUDE": ""}
        with fresh_database_without_recording(str(DSN)) as (url, _):
            with runner.running_server(database_url=url, runtime_root=runtime, bin_dir=bin_dir, home=home,
                    notes_root=home / "notes", env=environment, startup_timeout=60,
                    log_path=tmp_path / "serve.log") as api:
                api.post("/api/v1/workflows/put", {"key": "evidence-fixture", "name": "Evidence fixture",
                                                 "max_step_retries": 0})
                yield api, url, work
    return serve()


def _run_result_task(api, url, work, ending):
    scenarios = work.parent / "scenarios.json"
    scripts = json.loads(scenarios.read_text())
    script = scripts[str(work.resolve())]
    if ending == "failed":
        script["attempts"]["s1"][0]["files"]["delivered.txt"] = "incomplete\n"
    if ending == "disabled":
        script["plan"].append({"id": "g1", "kind": "gate", "topic": "Review measured result",
            "agenda": [{"question": "End this fixture?"}], "depends_on": ["s1"]})
    scenarios.write_text(json.dumps(scripts))
    owner = api.post("/api/v1/project_admin/create", {"name": "验收证据示例", "work_dir": str(work)})[
        "result"]["project"]["project_id"]
    api.post("/api/v1/project_admin/update", {"project_id": owner, "fields": {
        "task_workspaces": "in_place", "operator_notes": "off", "agent_room": "off"}})
    mode = "network" if script["plan"][0]["checks"][0].get("network") else "local"
    api.post("/api/v1/acceptance_checks/update", {"project_id": owner, "acceptance_checks": mode})
    task_id = api.post("/api/v1/tasks/create", {"project_id": owner, "workflow": "evidence-fixture",
        "requirement": "生成示例交付并记录验收证据"})["result"]["task_id"]

    async def synthetic_notice_target():
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("UPDATE tasks SET origin_channel_id='channel-lark', chat_id='fixture-recipient', "
                               "chat_kind='user' WHERE id=$1", task_id)
        finally:
            await conn.close()
    asyncio.run(synthetic_notice_target())
    api.post("/api/v1/ops/task_control/start", {"task_id": task_id})
    deadline = time.monotonic() + 90
    disabled = False
    while True:
        detail = api.post("/api/v1/tasks/detail", {"task_id": task_id})["result"]
        if ending == "disabled" and not disabled and any(g.get("waiting_on") == "user" for g in detail.get("gates", [])):
            measured = api.post("/api/v1/task_evidence/get", {"task": task_id})["result"]["steps"][0]["runs"]
            assert measured and measured[-1]["state"] == "passed"
            api.post("/api/v1/acceptance_checks/update", {"project_id": owner, "acceptance_checks": "off"})
            api.post("/api/v1/ops/gates/reply", {"task_id": task_id, "gate": "g1", "text": "结束示例"})
            disabled = True
        if detail["status"] in {"completed", "failed", "stopped", "cancelled"}:
            break
        assert time.monotonic() < deadline, "scripted result task did not finish"
        time.sleep(0.1)
    assert detail["status"] == ("failed" if ending == "failed" else "completed"), detail
    assert ending != "disabled" or disabled
    return detail
