"""Acquisition's authenticated transport, atomic import and confirmation contracts."""

import json

import pytest
from library_fixtures import fake_claude_home

from looper.agents import catalog_sources
from looper.db import library
from tests.db_support import DSN, run_db
from tests.test_api_v1_catalog_installs_db import _api
from tests.test_catalog_sources_db import _b64, _commit, _git, _skill, repo as repo, scratch as scratch

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize(("legacy", "action"), [
    ("/api/catalog/import", "import_machine"), ("/api/catalog/mcp/import", "import_mcp"),
    ("/api/catalog/upload", "upload"), ("/api/catalog/git/preview", "git_preview"),
    ("/api/catalog/git/import", "git_import"), ("/api/catalog/skills", "create_skill"),
    ("/api/catalog/mcp", "create_mcp"),
])
def test_retired_acquisition_routes_with_authenticated_v1_control(tmp_path, monkeypatch, repo, scratch,
                                                                 legacy, action):
    config = fake_claude_home(tmp_path / "synthetic-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {"example": {"command": "example"}}}))
    bodies = {
        "import_machine": {}, "import_mcp": {},
        "upload": {"filename": "SKILL.md", "content_base64": _b64(_skill("uploaded"))},
        "git_preview": {"url": repo.as_uri()},
        "git_import": {"url": repo.as_uri(), "commit": _git(repo, "rev-parse", "HEAD"),
                       "items": [{"path": "skills/alpha"}]},
        "create_skill": {"name": "manual", "description": "Instructions", "content": "Reply."},
        "create_mcp": {"name": "manual", "transport": "stdio", "command": "example"},
    }
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            assert (await call(legacy, bodies[action]))[0] == 404
            status, answer = await call("catalog_sources." + action, bodies[action])
            assert status == 200 and answer["result"], answer
            if action == "git_preview":
                assert answer["result"]["items"] and await library.list_items() == []
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["import_machine", "import_mcp", "create_mcp"])
def test_credential_acquisition_refuses_clients_before_validation_and_audit(tmp_path, monkeypatch, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, answer = await call("catalog_sources." + action, client=True, raw="not-json")
            assert status == 403 and answer["error"]["code"] == "forbidden"
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
            assert await library.list_items() == []
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize(("action", "report", "count"), [
    ("import_machine", "last_import", 3), ("import_mcp", "last_mcp_import", 1),
])
def test_machine_imports_are_idempotent_and_mask_credentials(tmp_path, monkeypatch, caplog, action, report, count):
    config = fake_claude_home(tmp_path / "synthetic-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    secret = "synthetic-acquisition-key"
    (config / ".claude.json").write_text(json.dumps({"mcpServers": {
        "example": {"command": "example", "env": {"API_KEY": secret}}}}))
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            status, first = await call("catalog_sources." + action)
            assert status == 200 and len(first["result"][report]["added"]) == count
            status, second = await call("catalog_sources." + action)
            assert status == 200 and second["result"][report]["added"] == []
            assert second["result"][report]["unchanged"] == count
            assert secret not in json.dumps(second) + caplog.text
            if action == "import_mcp":
                assert second["result"]["items"][0]["mcp"]["env"] == {"API_KEY": "***"}
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, timeout=30)


def test_created_mcp_masks_secret_headers_and_never_audits_them(tmp_path, monkeypatch, caplog):
    async def run(pool):
        secret = "synthetic-acquisition-key"
        async with _api(tmp_path, pool) as call:
            status, created = await call("catalog_sources.create_mcp", {
                "name": "manual", "transport": "http", "url": "https://example.test/mcp",
                "headers": {"Authorization": secret}})
            assert status == 200 and created["result"]["item"]["mcp"]["headers"] == {"Authorization": "***"}
            assert secret not in json.dumps(created) + caplog.text
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0
    run_db(run, monkeypatch, timeout=30)


async def _confirmed_import(call, body):
    status, pending = await call("catalog_sources.git_import", body, client=True)
    assert status == 202 and pending["impact"]
    return await call("confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)


async def _import_alpha(call, repo):
    _, preview = await call("catalog_sources.git_preview", {"url": repo.as_uri()}, client=True)
    body = {"url": repo.as_uri(), "commit": preview["result"]["commit"], "items": [{"path": "skills/alpha"}]}
    status, imported = await _confirmed_import(call, body)
    assert status == 200 and imported["result"]["added"] == ["skill:alpha"]
    return body, imported["result"]["items"][0]


def test_selected_git_import_preserves_content_and_unchanged_counters(tmp_path, monkeypatch, repo, scratch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            body, item = await _import_alpha(call, repo)
            assert [row["name"] for row in await library.list_items()] == ["alpha"]
            _, detail = await call("catalog_items.get", {"item_id": item["item_id"]})
            assert detail["result"]["content"] == "Reply ALPHA-OK.\n"
            status, again = await _confirmed_import(call, body)
            assert status == 200 and again["result"]["unchanged"] == ["skill:alpha"]
            assert again["result"]["added"] == again["result"]["updated"] == []
    run_db(run, monkeypatch, timeout=30)


def test_modified_git_replacement_includes_edits_made_while_confirmation_is_pending(tmp_path, monkeypatch, repo, scratch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            body, item = await _import_alpha(call, repo)
            await catalog_sources.update_item(item["item_id"], {"content": "Keep local changes."})
            body["commit"] = _commit(repo, {"skills/alpha/SKILL.md": _skill("alpha", "Updated upstream.")})
            status, pending = await call("catalog_sources.git_import", body, client=True)
            assert status == 202 and pending["impact"]
            _, detail = await call("catalog_items.get", {"item_id": item["item_id"]})
            assert detail["result"]["content"] == "Keep local changes.\n"
            await catalog_sources.update_item(item["item_id"], {"content": "Edited while pending."})
            status, done = await call("confirm", {"confirmation_id": pending["confirmation_id"]}, client=True)
            assert status == 200 and done["result"]["updated"] == ["skill:alpha"]
            assert done["result"]["items"][0]["modified"] is False
            _, detail = await call("catalog_items.get", {"item_id": item["item_id"]})
            assert detail["result"]["content"] == "Updated upstream.\n"
    run_db(run, monkeypatch, timeout=30)


def test_commit_drift_leaves_existing_and_selected_items_unchanged(tmp_path, monkeypatch, repo, scratch):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            body, _ = await _import_alpha(call, repo)
            before = await library.list_items()
            _commit(repo, {"README.md": "Moved after preview.\n"})
            body["items"].append({"path": "skills/beta"})
            status, answer = await call("catalog_sources.git_import", body, client=True)
            assert status == 409 and answer["error"]["code"] == "conflict"
            assert await library.list_items() == before
    run_db(run, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["upload", "create_skill"])
def test_duplicate_client_acquisition_preserves_original_content(tmp_path, monkeypatch, action):
    async def run(pool):
        async with _api(tmp_path, pool) as call:
            _, first = await call("catalog_sources.create_skill", {
                "name": "example", "description": "Original", "content": "Keep this."}, client=True)
            item_id = first["result"]["item"]["item_id"]
            body = ({"filename": "SKILL.md", "content_base64": _b64(_skill("example", "Replace this."))}
                    if action == "upload" else {"name": "example", "description": "Replacement", "content": "Replace this."})
            status, answer = await call("catalog_sources." + action, body, client=True)
            assert status == 409 and answer["error"]["code"] == "conflict"
            _, detail = await call("catalog_items.get", {"item_id": item_id}, client=True)
            assert detail["result"]["content"] == "Keep this.\n"
            assert detail["result"]["item"]["description"] == "Original"
            assert len(await library.list_items()) == 1
    run_db(run, monkeypatch, timeout=30)
