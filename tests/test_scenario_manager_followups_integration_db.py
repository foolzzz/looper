"""The portable scenario graph still executes and reads back as one contract."""

from __future__ import annotations

import pytest

from looper.agents import config_transfer, library
from looper.db import connection, tasks
from looper.db.connection import ensure_schema
from tests.db_support import DSN, fresh_schema, run_db
from tests.test_api_v1_schedule_writes_db import _api
from tests.test_config_transfer_db import _item

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OWNER = "manager-reports"


def test_transferred_graph_executes_owned_template_and_reads_back_capabilities(tmp_path, monkeypatch):
    async def run(source):
        profile_name = await source.fetchval(
            "SELECT name FROM agent_profiles WHERE id='agent-claude-code-opus'")
        document = {
            "format": config_transfer.V4_FORMAT,
            "managers": [{
                "id": OWNER, "name": "Reports", "system_prompt": "Review public reports.",
                "enabled": True, "tools": "tasks", "task_scope": "own", "task_policy": "templates_only",
                "installs": [{"item": {"kind": "skill", "name": "report-helper"}, "enabled": True}],
                "templates": [{
                    "key": "report", "name": "Public report", "project": "Reports",
                    "requirement": "Inspect {{url}}.", "planner_instructions": "Check public sources.",
                    "inputs": [{"key": "url", "label": "Source URL", "kind": "url", "required": True}],
                    "profiles": {"reviewer": profile_name}, "role_templates": ["sources"],
                }],
            }],
            "channels": [{
                "channel_id": "channel-telegram-2", "channel_type": "telegram", "manager_id": OWNER,
                "display_name": "Reports bot", "enabled": True, "config": {"allowed_user_ids": ["42"]},
            }],
            "catalog": [_item("skill", "report-helper", [{
                "path": "SKILL.md", "content": "---\nname: report-helper\ndescription: Check public sources\n---\nCheck public sources.\n",
            }], origin="upload")],
            "projects": [{
                "name": "Reports", "execution": {"planner_roles": "on"},
                "role_templates": [{"key": "sources", "kind": "review", "role_prompt": "Check sources."}],
                "schedules": [{
                    "name": "Daily report", "cron": "0 9 * * *", "timezone": "UTC",
                    "requirement": "Summarize the result.", "manager": OWNER,
                    "template_key": "report", "template_inputs": {"url": "https://example.org/report"},
                }],
            }],
        }
        async with source.acquire() as conn:
            seeded = await config_transfer.run_import(conn, document, apply=True)
        assert not any(counts.get("skipped") for counts in seeded.view()["counts"].values()), seeded.view()
        source_root = tmp_path / "source"
        source_root.mkdir()
        async with _api(source_root, source) as call:
            status, exported = await call("/api/v1/config/export", {"include_projects": True})
            assert status == 200, exported
        portable = exported["document"]
        assert portable["format"] == config_transfer.V4_FORMAT
        exported_owner = next(row for row in portable["managers"] if row["id"] == OWNER)
        assert exported_owner["installs"] == document["managers"][0]["installs"]
        assert portable["projects"][0]["schedules"][0]["manager"] == OWNER
        assert next(row for row in portable["channels"] if row["channel_id"] == "channel-telegram-2")["manager_id"] == OWNER

        async with fresh_schema() as target:
            await ensure_schema(target)
            with monkeypatch.context() as target_patch:
                target_patch.setattr(connection, "_pool", target)
                target_root = tmp_path / "target"
                target_root.mkdir()
                async with _api(target_root, target) as call:
                    status, preview = await call("/api/v1/config/import_preview", {"document": portable})
                    assert status == 200, preview
                    assert await target.fetchval("SELECT count(*) FROM schedules") == 0
                    assert await target.fetchval("SELECT count(*) FROM managers WHERE id=$1", OWNER) == 0
                    status, applied = await call("/api/v1/config/import_apply", {"import_id": preview["import_id"]})
                    assert status == 200, applied
                    assert applied["plan"]["applied"]
                    assert not any(counts.get("skipped") for counts in applied["plan"]["counts"].values()), applied
                    bot = await target.fetchrow("SELECT manager_id,enabled FROM channel_configs WHERE channel_id='channel-telegram-2'")
                    assert tuple(bot) == (OWNER, False)
                    assert await target.fetchval("SELECT count(*) FROM channel_secrets") == 0
                    schedule_id = await target.fetchval("SELECT id FROM schedules")
                    _, schedule = await call("get", {"schedule_id": schedule_id})
                    assert schedule["schedule"]["manager_id"] == OWNER
                    assert schedule["schedule"]["template_key"] == "report"
                    # Work directories are local: repoint the imported schedule before executing it.
                    status, repointed = await call("update", {"schedule_id": schedule_id,
                                                             "fields": {"work_dir": str(target_root)}})
                    assert status == 200, repointed
                    status, fired = await call("run", {"schedule_id": schedule_id})
                    assert status == 200 and fired["run"]["status"] == "running", fired
                    task_id = fired["run"]["task_id"]
                    status, detail = await call("/api/v1/tasks/detail", {"task_id": task_id})
                    assert status == 200, detail
                    assert detail["manager_id"] == OWNER and detail["manager_name"] == "Reports"
                    assert detail["requirement"].startswith("Inspect https://example.org/report.\n\nSummarize the result.")
                    snapshot = detail["template"]
                    assert snapshot["key"] == "report" and snapshot["planner_instructions"] == "Check public sources."
                    assert snapshot["inputs"][0]["value"] == "https://example.org/report"
                    assert snapshot["roles"][0]["role_prompt"] == "Check sources."
                    assert detail["profile_overrides"]["reviewer"]["name"] == profile_name
                    assert (await tasks.get_task(task_id, pool=target))["template"] == snapshot
                    _, capabilities = await call("/api/v1/manager_installs/list", {"manager_id": OWNER})
                    assert [(row["name"], row["inherited"]) for row in capabilities["installs"]] == [("report-helper", False)]
                    loaded = await library.load_installed(target_root, "manager", manager_id=OWNER)
                    assert loaded.error is None and [item.name for item in loaded.items] == ["report-helper"]
                    assert not (await library.load_installed(target_root, "manager", manager_id="manager")).items

                    # A seeded default-owned task proves the owner predicate excludes real rows.
                    await tasks.create_task(task_id="task-default-control", requirement="Control", pool=target,
                                            project_id=detail["project_id"], manager_id="manager")
                    _, page = await call("/api/v1/tasks/page", {"manager_id": OWNER})
                    assert [row["task_id"] for row in page["tasks"]] == [task_id]
                    assert page["tasks"][0]["template"] == snapshot and page["counts"] == {"init": 1}
                    _, unfiltered = await call("/api/v1/tasks/page", {})
                    assert {row["task_id"] for row in unfiltered["tasks"]} == {task_id, "task-default-control"}

    run_db(run, monkeypatch, timeout=45)
