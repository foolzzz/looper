"""Disposable real-server/browser smoke for Dashboard model configuration.

Install with ``uv sync --extra test`` and
``uv run --extra test python -m playwright install chromium``.
Set LOOPER_TEST_DATABASE_URL to a disposable PostgreSQL server with CREATEDB.
The URL is used only to create/drop a fresh database, never as the app database.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from evals.prompt_regression.runner import Api, EvalError, fresh_database, free_port
from looper import ops_config
from looper.agents import operator_auth

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_NAME = "Isolated browser smoke project"


@pytest.fixture
def database_admin_url():
    url = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("UNTESTED: set LOOPER_TEST_DATABASE_URL to a disposable PostgreSQL server with CREATEDB")
    return url


@dataclass
class DashboardServer:
    api: Api
    database_url: str
    home: Path
    runtime_root: Path
    env: dict[str, str]
    process: subprocess.Popen
    log_path: Path


def _database_state(url):
    async def read():
        import asyncpg

        conn = await asyncpg.connect(url, timeout=5)
        try:
            return {"database": await conn.fetchval("SELECT current_database()"),
                    "channels": [tuple(row) for row in await conn.fetch(
                        "SELECT channel_id, enabled FROM channel_configs ORDER BY channel_id")]}
        finally:
            await conn.close()

    return asyncio.run(asyncio.wait_for(read(), timeout=10))


def _seed(url, work_dir):
    async def seed():
        import asyncpg

        from looper.agents.serve import seed_channel_configs
        from looper.db.connection import ensure_schema
        from looper.db.projects import create_project

        conn = await asyncpg.connect(url, timeout=5)
        try:
            await ensure_schema(conn)
            await seed_channel_configs(conn)
            await create_project(name=SEED_NAME, work_dir=str(work_dir), pool=conn)
        finally:
            await conn.close()

    asyncio.run(asyncio.wait_for(seed(), timeout=30))


def _wait_ready(server):
    deadline = time.monotonic() + 45
    while server.process.poll() is None:
        try:
            server.api.token = operator_auth.read_token(server.runtime_root)
            if server.api.token:
                server.api.get("/api/status")
                return
        except (OSError, EvalError):
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    raise AssertionError(f"isolated server did not start:\n{server.log_path.read_text()}")


@contextmanager
def _running_dashboard(tmp_path, admin_url):
    home, root, work, bin_dir = (tmp_path / name for name in ("home", "runtime", "work", "bin"))
    for directory in (home, root, work, bin_dir):
        directory.mkdir()
    # An allowlist prevents ambient credentials, channel tokens, ops settings and
    # executable runtimes from entering the server; keep the suite's root guard.
    env = {key: value for key, value in os.environ.items()
           if key in ("LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR") or key.startswith("PYTEST_LOOPER_")}
    env.update(HOME=str(home), PATH=str(bin_dir),
               PYTHONPATH=os.pathsep.join((str(REPO_ROOT / "tests/runtime_root_guard"), str(REPO_ROOT))),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
               GROK_HOME=str(home / ".grok"), XDG_CONFIG_HOME=str(home / ".config"))
    with fresh_database(admin_url) as (url, _):
        _seed(url, work)
        port = free_port()
        ops = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE,
                                   database_url=url, port=port, operator_notes_root=str(home / "notes"))
        ops_config.write_private(ops.path, ops_config.render(ops))
        log_path = tmp_path / "serve.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "looper.cli", "serve", "--host", "127.0.0.1", "--port", str(port),
                 "--runtime-root", str(root), "--config", str(ops.path)],
                cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            server = DashboardServer(Api(f"http://127.0.0.1:{port}", timeout=2), url, home, root, env, process, log_path)
            try:
                _wait_ready(server)
                yield server
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


@pytest.fixture
def dashboard_server(tmp_path, database_admin_url):
    with _running_dashboard(tmp_path, database_admin_url) as server:
        yield server


@pytest.fixture
def chromium_browser(dashboard_server, tmp_path, monkeypatch):
    # Keep the server outside Playwright's sync event loop: fresh_database uses
    # asyncio.run() both to create the database and to drop it on teardown.
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="UNTESTED: Playwright missing; run `uv sync --extra test`", exc_type=ImportError)
    # Playwright finds its installed browser before HOME is isolated. Its driver
    # and Chromium then run with a throwaway HOME and profile.
    with playwright.sync_playwright() as driver:
        executable = Path(driver.chromium.executable_path)
    if not executable.is_file():
        pytest.skip("UNTESTED: Chromium missing; run `uv run --extra test python -m playwright install chromium`")
    monkeypatch.setenv("HOME", str(tmp_path / "browser-home"))
    (tmp_path / "browser-home").mkdir()
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, executable_path=str(executable), timeout=15000)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def dashboard_page(chromium_browser, dashboard_server):
    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        page = context.new_page()
        page.set_default_timeout(10000)
        page.set_default_navigation_timeout(15000)
        yield page
    finally:
        context.close()


def test_dashboard_server_is_isolated(dashboard_server, tmp_path):
    server = dashboard_server
    assert urlsplit(server.api.base_url).port != 8767
    assert server.database_url != os.environ["LOOPER_TEST_DATABASE_URL"]
    assert server.home.is_relative_to(tmp_path)
    assert server.runtime_root.is_relative_to(tmp_path)
    assert server.home != server.runtime_root
    for key in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME", "XDG_CONFIG_HOME"):
        assert Path(server.env[key]).is_relative_to(tmp_path)
    assert not any(key.startswith(("LOOPER_", "LOOPX_")) for key in server.env)
    assert server.env["PATH"] == str(tmp_path / "bin")
    projects = server.api.get("/api/projects")["projects"]
    assert [project["name"] for project in projects] == [SEED_NAME]
    state = _database_state(server.database_url)
    assert state["database"].startswith("looper_eval_")
    assert state["channels"] == [
        ("channel-lark", False), ("channel-lark_bot", False), ("channel-telegram", False),
    ]


def test_browser_opens_own_dashboard_and_reads_seed(dashboard_page, dashboard_server):
    from playwright.sync_api import expect

    # Readiness is the rendered database seed; background dashboard requests
    # must not make the smoke wait for the whole page's network to become idle.
    response = dashboard_page.goto(dashboard_server.api.base_url, wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    assert urlsplit(dashboard_page.url).netloc == urlsplit(dashboard_server.api.base_url).netloc
    expect(dashboard_page.get_by_role("region", name="Projects").get_by_text(SEED_NAME, exact=True)).to_be_visible()


@pytest.mark.parametrize("failure_at", ["startup", "test"])
def test_failed_smoke_cleans_up_server_and_database(tmp_path, database_admin_url, monkeypatch, failure_at):
    started = []
    original = _wait_ready

    def wait_ready(server):
        started.append(server)
        if failure_at == "startup":
            raise RuntimeError("injected smoke failure")
        original(server)

    monkeypatch.setattr(sys.modules[__name__], "_wait_ready", wait_ready)
    with pytest.raises(RuntimeError, match="injected smoke failure"):
        with _running_dashboard(tmp_path, database_admin_url):
            raise RuntimeError("injected smoke failure")
    [server] = started
    assert server.process.poll() is not None
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", urlsplit(server.api.base_url).port)) != 0

    async def database_exists():
        import asyncpg

        conn = await asyncpg.connect(database_admin_url, timeout=5)
        try:
            return await conn.fetchval("SELECT EXISTS (SELECT FROM pg_database WHERE datname = $1)",
                                       urlsplit(server.database_url).path.lstrip("/"))
        finally:
            await conn.close()

    assert not asyncio.run(database_exists())


ACCOUNT = "Smoke account"
MODEL_NAME = "smoke-shared-model"
SECRET = "synthetic-dashboard-key-not-a-credential"


def _registry_state(server):
    """Read persisted identities, including while Playwright owns this thread's loop."""
    async def read():
        import asyncpg

        conn = await asyncpg.connect(server.database_url, timeout=5)
        try:
            return {
                "providers": [dict(r) for r in await conn.fetch(
                    "SELECT p.id, p.name, p.vendor_type, EXISTS (SELECT FROM llm_provider_secrets s "
                    "WHERE s.provider_id = p.id) AS key_set, EXISTS (SELECT FROM llm_provider_secrets s "
                    "WHERE s.provider_id = p.id AND s.api_key = $1) AS synthetic_key "
                    "FROM llm_providers p ORDER BY p.id", SECRET)],
                "endpoints": [dict(r) for r in await conn.fetch(
                    "SELECT provider_id, protocol, base_url FROM llm_provider_endpoints ORDER BY provider_id, protocol")],
                "models": [dict(r) for r in await conn.fetch(
                    "SELECT id, provider_id, name, protocol, context_window, input_usd_per_mtok "
                    "FROM llm_models ORDER BY id")],
                "profiles": [dict(r) for r in await conn.fetch(
                    "SELECT id, name, runtime_id, model_id, effort, custom_prompt FROM agent_profiles ORDER BY id")],
                "layers": [dict(r) for r in await conn.fetch(
                    "SELECT scope, role, profile_id FROM agent_setting_layers ORDER BY scope, role")],
                "channels": [dict(r) for r in await conn.fetch(
                    "SELECT channel_id, enabled FROM channel_configs ORDER BY channel_id")],
            }
        finally:
            await conn.close()

    def run():
        return asyncio.run(asyncio.wait_for(read(), timeout=10))

    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run).result(timeout=15)


def test_incompatible_protocol_cannot_create_profile(dashboard_server):
    """Negative control: the same name on a different protocol must not pair."""
    api = dashboard_server.api
    provider = api.post("/api/providers", {"name": ACCOUNT, "vendor_type": "custom", "endpoints": [
        {"protocol": protocol, "base_url": "https://example.invalid/v1"}
        for protocol in ("anthropic-messages", "openai-chat")]})["provider"]
    models = {protocol: api.post("/api/models", {
        "provider_id": provider["provider_id"], "name": MODEL_NAME, "protocol": protocol,
    })["model"] for protocol in ("anthropic-messages", "openai-chat")}
    before = _registry_state(dashboard_server)
    assert before["profiles"]  # Positive control for the DB readback.
    with pytest.raises(EvalError, match=r"answered 400:.*protocol"):
        api.post("/api/agent-profiles", {"name": "Rejected profile", "runtime_id": "rt-claude-code",
                                        "model_id": models["openai-chat"]["model_id"]})
    assert _registry_state(dashboard_server) == before
    profile = api.post("/api/agent-profiles", {"name": "Compatible profile", "runtime_id": "rt-claude-code",
                                              "model_id": models["anthropic-messages"]["model_id"]})["profile"]
    assert profile["model_id"] == models["anthropic-messages"]["model_id"]
    assert next(p for p in _registry_state(dashboard_server)["profiles"]
                if p["id"] == profile["profile_id"])["model_id"] == profile["model_id"]


def _settings(page, server, sub):
    page.goto(f"{server.api.base_url}/#settings/{sub}", wait_until="domcontentloaded")


def _row(page, name):
    return page.get_by_role("listitem").filter(has=page.get_by_text(name, exact=True))


def _finish_form(form, submit):
    from playwright.sync_api import expect

    with form.page.expect_response(lambda r: "/api/" in r.url and r.request.method in ("POST", "PUT")) as response:
        form.get_by_role("button", name=submit, exact=True).click()
    assert response.value.status in (200, 201), response.value.text()
    # Warning acknowledgements are part of the save flow, not evidence of failure.
    ok = form.get_by_role("button", name="OK", exact=True)
    if response.value.json().get("warnings"):
        expect(ok).to_be_visible()
        ok.click()
    expect(form).not_to_be_visible()


@pytest.mark.parametrize("width", [1280, 768])
def test_complete_dashboard_configuration_flow(dashboard_page, dashboard_server, tmp_path, width):
    from playwright.sync_api import expect

    page, server = dashboard_page, dashboard_server
    page.set_viewport_size({"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _settings(page, server, "providers")
    add = page.get_by_role("button", name="Add provider", exact=True)
    # Keyboard entry into the form, followed by a real Tab focus transition.
    add.focus()
    add.press("Enter")
    form = page.get_by_role("form", name="Add a provider")
    name = form.get_by_label("Name *", exact=True)
    expect(name).to_be_focused()
    name.fill(ACCOUNT)
    name.press("Tab")
    expect(form.get_by_label("Vendor", exact=True)).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for protocol in ("anthropic-messages", "openai-chat"):
        form.get_by_role("checkbox", name=protocol, exact=True).check()
        form.get_by_label(f"{protocol} URL", exact=True).fill(f"https://example.invalid/{protocol}")
    # An invalid endpoint is surfaced by the real HTTP handler; no account is saved.
    form.get_by_label("openai-chat URL", exact=True).fill("not-a-url")
    with page.expect_response(lambda r: r.url.endswith("/api/providers") and r.request.method == "POST") as failed:
        form.get_by_role("button", name="Add provider", exact=True).click()
    assert failed.value.status == 400
    expect(form.get_by_role("alert")).to_be_visible()
    assert not any(p["name"] == ACCOUNT for p in _registry_state(server)["providers"])
    form.get_by_label("openai-chat URL", exact=True).fill("https://example.invalid/openai-chat")
    _finish_form(form, "Add provider")
    account = _row(page, ACCOUNT)
    expect(account).to_contain_text("API key: not set")
    provider = next(p for p in server.api.get("/api/providers")["providers"] if p["name"] == ACCOUNT)
    pid = provider["provider_id"]
    assert provider["api_key"] is None and provider["model_count"] == 0
    assert not next(p for p in _registry_state(server)["providers"] if p["id"] == pid)["key_set"]
    assert {(e["protocol"], e["base_url"]) for e in _registry_state(server)["endpoints"]
            if e["provider_id"] == pid} == {
                (protocol, f"https://example.invalid/{protocol}") for protocol in ("anthropic-messages", "openai-chat")}
    account.get_by_role("button", name=f"Models for provider {ACCOUNT}", exact=True).click()
    expect(account).to_contain_text("No models registered")
    assert server.api.get("/api/models", provider_id=pid)["models"] == []

    for protocol in ("anthropic-messages", "openai-chat"):
        account.get_by_role("button", name="Add model", exact=True).click()
        model_form = page.get_by_role("form", name=f"Add model to {ACCOUNT}")
        expect(model_form.get_by_label("Exact model name *", exact=True)).to_be_disabled()
        model_form.get_by_label("Protocol *", exact=True).select_option(protocol)
        model_form.get_by_label("Exact model name *", exact=True).fill(MODEL_NAME)
        model_form.get_by_label("Context window", exact=True).fill("64000")
        for label in ("Input price", "Output price", "Cache read price", "Cache write price"):
            model_form.get_by_label(label, exact=True).fill("0")
        model_form.get_by_label("Effort support", exact=True).select_option("supported")
        model_form.get_by_label("Supported efforts", exact=True).fill("low, high")
        _finish_form(model_form, "Add model")
    models = server.api.get("/api/models", provider_id=pid)["models"]
    assert len(models) == 2 and {m["name"] for m in models} == {MODEL_NAME}
    by_protocol = {m["protocol"]: m for m in models}
    mid = by_protocol["anthropic-messages"]["model_id"]
    assert mid != by_protocol["openai-chat"]["model_id"]
    assert {m["protocol"] for m in _registry_state(server)["models"] if m["provider_id"] == pid} == set(by_protocol)

    _settings(page, server, "profiles/new")
    profile_form = page.get_by_role("form", name="New Agent Profile", exact=True)
    profile_form.get_by_label("Name *", exact=True).fill("Smoke profile")
    profile_form.get_by_label("Runtime", exact=True).select_option("rt-claude-code")
    profile_form.get_by_role("button", name="Add Agent Profile", exact=True).click()
    expect(profile_form.get_by_role("alert")).to_contain_text("compatible model")
    # Missing CLIs are honestly unavailable, rather than supplied by fake executables.
    unavailable = profile_form.get_by_label("Runtime", exact=True).locator('option[value="rt-codex"]')
    expect(unavailable).to_be_disabled()
    profile_form.get_by_label("Provider", exact=True).select_option(pid)
    choices = profile_form.get_by_label("Model *", exact=True)
    expect(choices.locator(f'option[value="{mid}"]')).to_have_count(1)
    expect(choices.locator(f'option[value="{by_protocol["openai-chat"]["model_id"]}"]')).to_have_count(0)
    choices.select_option(mid)
    profile_form.get_by_label("Effort", exact=True).select_option("high")
    profile_form.get_by_label("Custom prompt", exact=True).fill("Public smoke preview marker")
    _finish_form(profile_form, "Add Agent Profile")
    profile = next(p for p in server.api.get("/api/registry")["profiles"] if p["name"] == "Smoke profile")
    profile_id = profile["profile_id"]
    assert profile["model_id"] == mid and profile["provider"]["provider_id"] == pid
    assert next(p for p in server.api.get("/api/providers")["providers"] if p["provider_id"] == pid)["api_key"] is None
    saved = next(p for p in _registry_state(server)["profiles"] if p["id"] == profile_id)
    assert (saved["model_id"], saved["effort"], saved["custom_prompt"]) == (mid, "high", "Public smoke preview marker")
    profile_row = _row(page, "Smoke profile")
    summary = profile_row.get_by_text("Preview system prompt", exact=True)
    summary.focus()
    summary.press("Enter")
    profile_row.get_by_label("Role", exact=True).select_option("worker")
    with page.expect_response(lambda r: "/prompt-preview?" in r.url) as preview:
        profile_row.get_by_role("button", name="Load", exact=True).click()
    assert preview.value.status == 200
    blocks = preview.value.json()["blocks"]
    assert any("Public smoke preview marker" in (b["text"] or "") for b in blocks)
    expect(profile_row.get_by_role("list", name="System prompt blocks")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    _settings(page, server, "roles/worker")
    role = page.locator("#roles-worker-panel")
    selection = role.get_by_label("Agent Profile", exact=True)
    before = server.api.get("/api/agent-settings")["roles"]["worker"]["values"]["profile_id"]
    selection.select_option("__new_profile__")
    nested = role.get_by_role("group", name="New Agent Profile", exact=True)
    nested.get_by_label("Name *", exact=True).fill("Embedded smoke profile")
    nested.get_by_label("Provider", exact=True).select_option(pid)
    nested.get_by_label("Model *", exact=True).select_option(mid)
    _finish_form(nested, "Add Agent Profile")
    expect(selection).to_have_value(before or "")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Registering an embedded profile must not silently persist a role selection.
    assert server.api.get("/api/agent-settings")["roles"]["worker"]["values"]["profile_id"] == before
    selection.select_option(profile_id)
    role.get_by_role("button", name="Save overrides", exact=True).click()
    expect(role.get_by_text("Saved; applies to the next run", exact=True)).to_be_visible()
    assert server.api.get("/api/agent-settings")["roles"]["worker"]["values"]["profile_id"] == profile_id
    assert any(layer["scope"] == "role" and layer["role"] == "worker" and layer["profile_id"] == profile_id
               for layer in _registry_state(server)["layers"])

    _settings(page, server, "profiles")
    expect(_row(page, "Smoke profile").get_by_role("button", name=re.compile("Delete Agent Profile"))).to_be_disabled()
    _settings(page, server, "providers")
    account = _row(page, ACCOUNT)
    page.on("dialog", lambda dialog: dialog.accept())
    account.get_by_role("button", name=f"Delete provider {ACCOUNT}", exact=True).click()
    expect(account.get_by_role("alert")).to_be_visible()
    account.get_by_role("button", name=f"Models for provider {ACCOUNT}", exact=True).click()
    model_row = account.get_by_role("listitem").filter(has=page.get_by_role(
        "button", name=f"Delete model {MODEL_NAME} (anthropic-messages)", exact=True))
    model_row.get_by_role("button", name=f"Delete model {MODEL_NAME} (anthropic-messages)", exact=True).click()
    expect(model_row.get_by_role("alert")).to_be_visible()
    assert next(m for m in server.api.get("/api/models", provider_id=pid)["models"] if m["model_id"] == mid)
    account.get_by_role("button", name=f"Edit provider {ACCOUNT}", exact=True).click()
    edit = page.get_by_role("form", name=f"Edit provider {ACCOUNT}", exact=True)
    edit.get_by_label("API key", exact=True).fill(SECRET)
    _finish_form(edit, "Save")
    assert SECRET not in page.content()
    assert next(p for p in server.api.get("/api/providers")["providers"] if p["provider_id"] == pid)["api_key"] == "***"

    _settings(page, server, "transfer")
    with page.expect_download() as download:
        page.get_by_role("button", name="Export configuration", exact=True).click()
    exported = tmp_path / "smoke-config.json"
    download.value.save_as(exported)
    content = exported.read_text()
    data = json.loads(content)
    assert data["secrets"] == "omitted" and SECRET not in content
    assert len([m for m in data["models"] if m["provider"] == ACCOUNT]) == 2
    assert next(p for p in data["profiles"] if p["name"] == "Smoke profile")["model"] == {
        "provider": ACCOUNT, "name": MODEL_NAME, "protocol": "anthropic-messages"}
    # A real exported file with one edit proves preview/apply, rather than a no-op import.
    next(m for m in data["models"] if m["provider"] == ACCOUNT and m["protocol"] == "anthropic-messages")["context_window"] = 96000
    exported.write_text(json.dumps(data))
    before_import = _registry_state(server)
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Choose file", exact=True).click()
    with page.expect_response(lambda r: "/api/config/imports" in r.url and r.request.method == "POST") as upload:
        chooser.value.set_files(exported)
    assert upload.value.status in (200, 201)
    expect(page.get_by_text(re.compile("nothing written yet"))).to_be_visible()
    assert _registry_state(server) == before_import
    with page.expect_response(lambda r: r.url.endswith("/apply")) as applied:
        page.get_by_role("button", name=re.compile(r"^Apply \d+ changes?$")).click()
    assert applied.value.status == 200
    expect(page.get_by_role("status").filter(has_text="Applied smoke-config.json")).to_be_visible()
    after_import = _registry_state(server)
    assert next(m for m in after_import["models"] if m["id"] == mid)["context_window"] == 96000
    assert after_import["profiles"] == before_import["profiles"]
    assert after_import["layers"] == before_import["layers"]
    assert next(p for p in after_import["providers"] if p["id"] == pid)["key_set"]
    assert next(p for p in after_import["providers"] if p["id"] == pid)["synthetic_key"]
    assert after_import["endpoints"] == before_import["endpoints"]
    assert after_import["channels"] and all(not c["enabled"] for c in after_import["channels"])
    assert SECRET not in page.content()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors
    # One opt-in local artifact, containing only synthetic public UI data.
    screenshot = os.environ.get("LOOPER_DASHBOARD_SMOKE_SCREENSHOT")
    if screenshot and width == 1280:
        destination = Path(screenshot).resolve()
        assert destination.is_relative_to(REPO_ROOT / ".local")
        page.screenshot(path=str(destination))


def test_vendor_suggestions_are_explicit_and_invalid_inputs_do_not_write(dashboard_page, dashboard_server):
    from playwright.sync_api import expect

    page, server = dashboard_page, dashboard_server
    _settings(page, server, "providers/new")
    form = page.get_by_role("form", name="Add a provider", exact=True)
    form.get_by_label("Vendor", exact=True).select_option("deepseek")
    form.get_by_label("Name *", exact=True).fill("Smoke vendor account")
    vendor = next(v for v in server.api.get("/api/registry")["vendor_types"] if v["vendor_type"] == "deepseek")
    for endpoint in vendor["endpoints"]:
        expect(form.get_by_label(f'{endpoint["protocol"]} URL', exact=True)).to_have_value(endpoint["base_url"])
    _finish_form(form, "Add provider")
    provider = next(p for p in server.api.get("/api/providers")["providers"] if p["name"] == "Smoke vendor account")
    assert provider["model_count"] == 0
    account = _row(page, "Smoke vendor account")
    account.get_by_role("button", name="Models for provider Smoke vendor account", exact=True).click()
    account.get_by_role("button", name="Add model", exact=True).click()
    form = page.get_by_role("form", name="Add model to Smoke vendor account", exact=True)
    suggestion = vendor["suggested_models"][0]
    form.get_by_label("Protocol *", exact=True).select_option(suggestion["protocol"])
    suggestions = form.get_by_label("Vendor suggestion", exact=True)
    assert suggestions.locator("option").all_text_contents()[1:] == [
        s["name"] for s in vendor["suggested_models"] if s["protocol"] == suggestion["protocol"]]
    suggestions.select_option(suggestion["name"])
    expect(form.get_by_label("Exact model name *", exact=True)).to_have_value(suggestion["name"])
    assert server.api.get("/api/models", provider_id=provider["provider_id"])["models"] == []
    before = _registry_state(server)
    form.get_by_label("Context window", exact=True).fill("-1")
    with page.expect_response(lambda r: r.url.endswith("/api/models") and r.request.method == "POST") as invalid:
        form.get_by_role("button", name="Add model", exact=True).click()
    assert invalid.value.status == 400
    expect(form.get_by_role("alert")).to_be_visible()
    assert _registry_state(server) == before
    form.get_by_label("Context window", exact=True).fill("32000")
    _finish_form(form, "Add model")
    [model] = server.api.get("/api/models", provider_id=provider["provider_id"])["models"]
    assert (model["name"], model["protocol"], model["context_window"]) == (suggestion["name"], suggestion["protocol"], 32000)
    assert next(m for m in _registry_state(server)["models"] if m["id"] == model["model_id"])["context_window"] == 32000


def test_invalid_import_displays_server_error_without_writing(dashboard_page, dashboard_server):
    from playwright.sync_api import expect

    page, server = dashboard_page, dashboard_server
    _settings(page, server, "transfer")
    before = _registry_state(server)
    assert before["models"] and before["profiles"]
    with page.expect_file_chooser() as chooser:
        page.get_by_role("button", name="Choose file", exact=True).click()
    with page.expect_response(lambda r: "/api/config/imports" in r.url and r.request.method == "POST") as invalid:
        chooser.value.set_files({"name": "invalid.json", "mimeType": "application/json", "buffer": b"{broken"})
    assert invalid.value.status == 400
    expect(page.get_by_role("alert").filter(has_text="invalid.json")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Apply \d+ changes?$"))).to_have_count(0)
    assert _registry_state(server) == before
