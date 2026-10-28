"""Migrated registry -> isolated serve -> HTTP/Manager/Dashboard readback.

Run with LOOPER_TEST_DATABASE_URL (CREATEDB on a disposable server). Optional
real API-key combinations are supplied as LOOPER_REGISTRY_SMOKE_COMBINATIONS:
[{"runtime":"claude-code", "model":"exact-name", "protocol":"anthropic-messages",
  "base_url":"https://...", "key_env":"SMOKE_KEY"}]. No personal login is
discovered or copied. Missing combinations are explicitly skipped, never passed.
Private evidence stays in pytest's temporary directory (use --basetemp=.local/...).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import asyncpg
import pytest

from evals.prompt_regression import dialogs
from evals.prompt_regression.runner import fresh_database, free_port
from looper import ops_config
from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind
from looper.db import connection
from tests.bridge_schema_support import bridge_source as bridge_source, initialize_bridge, witness
from tests.test_llm_models_dashboard import DashboardServer, _wait_ready

ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = {"claude-code": "claude", "codex": "codex", "omp": "omp", "grok": "grok"}


def combinations(environ):
    selected = json.loads(environ.get("LOOPER_REGISTRY_SMOKE_COMBINATIONS", "[]"))
    if not isinstance(selected, list):
        raise ValueError("combinations must be a JSON array")
    for item in selected:
        if not isinstance(item, dict) or item.get("runtime") not in RUNTIMES:
            raise ValueError("unknown runtime combination")
        if not all(isinstance(item.get(k), str) and item[k].strip()
                   for k in ("model", "protocol", "base_url", "key_env")):
            raise ValueError("combination requires model, protocol, base_url and key_env")
    return selected


def inventory(environ):
    selected = combinations(environ)
    return [{"runtime": runtime, "installed": shutil.which(cli) is not None,
             "credential": bool(environ.get(item["key_env"])),
             "missing": (["cli"] if shutil.which(cli) is None else [])
                        + ([] if environ.get(item["key_env"]) else ["explicit API key"])
                        + (["API-key binding unsupported"] if AuthKind.API_KEY not in ADAPTERS[runtime].auth_kinds else []),
             "status": "not-run", "model": item.get("model"), "protocol": item.get("protocol")}
            for runtime, cli in RUNTIMES.items()
            for item in ([i for i in selected if i["runtime"] == runtime] or [{"key_env": ""}])]


def query(url, sql, *args):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
    return asyncio.run(asyncio.wait_for(run(), 20))


def migrate(url, source, real=None):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=15)
        try:
            await initialize_bridge(conn, source, url=url)
            if real is not None:
                await conn.execute("INSERT INTO llm_providers (id,name,vendor_type,auth) "
                    "VALUES ('prov-smoke-real','Runtime smoke','custom','api_key')")
                await conn.execute("INSERT INTO llm_provider_endpoints (provider_id,protocol,base_url) "
                    "VALUES ('prov-smoke-real',$1,$2)", real["protocol"], real["base_url"])
                await conn.execute("INSERT INTO llm_models (id,provider_id,name,protocol) "
                    "VALUES ('model-smoke-real','prov-smoke-real',$1,$2)", real["model"], real["protocol"])
                await conn.execute("INSERT INTO llm_provider_secrets (provider_id,api_key) "
                    "VALUES ('prov-smoke-real',$1)", os.environ[real["key_env"]])
                runtime_id = await conn.fetchval("SELECT id FROM agent_runtimes WHERE type=$1 AND builtin", real["runtime"])
                await conn.execute("INSERT INTO agent_profiles (id,name,runtime_id,model_id,agent_teams) "
                    "VALUES ('agent-smoke-real','Runtime smoke',$1,'model-smoke-real',false)", runtime_id)
            before = await witness(conn)
            await connection.ensure_schema(conn)
            assert await conn.fetchval("SELECT version FROM registry_schema_version") == 4
            after = await witness(conn)
            for table in ("task_plans", "system_settings", "library_items", "library_files", "library_installs"):
                assert after[table] == before[table], table
            return {"profiles_after": await conn.fetchval("SELECT count(*) FROM agent_profiles"),
                    "historical_records": len(after["llm_records"])}
        finally:
            await conn.close()
    return asyncio.run(asyncio.wait_for(run(), 60))


@contextmanager
def server(case, url, *, real=None):
    home, root, work, bin_dir = (case / n for n in ("home", "runtime", "work", "bin"))
    for directory in (home, root, work, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)
    scenarios = case / "scenarios.json"
    if real is None:
        if not scenarios.exists():
            scenarios.write_text('{"manager": {}}')
        # Keep the existing scripted MCP driver, but report realistic cumulative
        # four-kind usage so restart/resume can detect double counting.
        script = bin_dir / "claude"
        script.write_text(f"#!{sys.executable}\n" +
            "import json, os, sys\nfrom pathlib import Path\n"
            "from evals.prompt_regression import fake_claude as cli\n"
            f"state = Path({str(case / 'cumulative.json')!r})\n"
            f"capture = Path({str(case / 'launch-capture.jsonl')!r})\n"
            "with capture.open('a') as out:\n"
            "    out.write(json.dumps({'argv': sys.argv[1:], 'base_url': os.environ.get('ANTHROPIC_BASE_URL')}) + '\\n')\n"
            "emit = cli._emit\n"
            "def measured(event):\n"
            "    if event.get('type') == 'result':\n"
            "        n = (int(state.read_text()) if state.exists() else 0) + 1\n"
            "        state.write_text(str(n))\n"
            "        event['modelUsage'] = {'fake': {'inputTokens': n*10, 'outputTokens': n*5, "
            "'cacheReadInputTokens': n*2, 'cacheCreationInputTokens': n}}\n"
            "        event['total_cost_usd'] = n * 0.01\n"
            "    emit(event)\n"
            "cli._emit = measured\n"
            f"sys.exit(cli.main([{str(scenarios)!r}, *sys.argv[1:]]))\n")
        script.chmod(0o755)
    else:
        executable = shutil.which(RUNTIMES[real["runtime"]])
        assert executable
        target = bin_dir / RUNTIMES[real["runtime"]]
        if not target.exists():
            target.symlink_to(executable)
        if node := shutil.which("node"):
            if not (bin_dir / "node").exists():
                (bin_dir / "node").symlink_to(node)
    port = free_port()
    assert port != 8767
    ops = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE,
                               database_url=url, port=port, operator_notes_root=str(home / "notes"))
    ops_config.write_private(ops.path, ops_config.render(ops))
    env = {k: v for k, v in os.environ.items()
           if k in ("LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") or k.startswith("PYTEST_LOOPER_")}
    env.update(HOME=str(home), PATH=f"{bin_dir}:/usr/bin:/bin",
               PYTHONPATH=os.pathsep.join((str(ROOT / "tests/runtime_root_guard"), str(ROOT))),
               CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
               GROK_HOME=str(home / ".grok"), XDG_CONFIG_HOME=str(home / ".config"))
    log_path = case / "serve.log"
    # Prove the service subprocess imports this worktree before starting it.
    code = ("import sys, pathlib, looper; "
            "print(sys.executable, looper.__file__, flush=True); "
            f"assert pathlib.Path(looper.__file__).resolve() == pathlib.Path({str(ROOT / 'looper/__init__.py')!r}); "
            "from looper.cli import main; main()")
    with log_path.open("a") as log:
        process = subprocess.Popen([sys.executable, "-c", code, "serve", "--host", "127.0.0.1",
                                    "--port", str(port), "--runtime-root", str(root), "--config", str(ops.path)],
                                   cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        from evals.prompt_regression.runner import Api
        running = DashboardServer(Api(f"http://127.0.0.1:{port}", timeout=3), url, home, root, env, process, log_path)
        try:
            _wait_ready(running)
            assert not any(r["enabled"] for r in query(url, "SELECT enabled FROM channel_configs"))
            yield running
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


@pytest.fixture
def migrated(tmp_path, bridge_source, request):
    import looper
    assert Path(looper.__file__).resolve() == ROOT / "looper/__init__.py"
    assert Path(sys.executable).parent == ROOT / ".venv/bin"
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("not-run: disposable LOOPER_TEST_DATABASE_URL required")
    with fresh_database(admin) as (url, _):
        report = migrate(url, bridge_source) if getattr(request, "param", "bridge") == "bridge" else {"kind": "fresh"}
        (tmp_path / "migration-report.json").write_text(json.dumps(report, indent=2))
        yield url


def test_inventory_distinguishes_credentials_and_missing_cli(monkeypatch, tmp_path):
    # These are missing-evidence reports, not fabricated comparison results.
    (tmp_path / "installed-combinations.json").write_text(json.dumps(inventory(os.environ), indent=2))
    for tier in ("haiku", "opus"):
        (tmp_path / f"dialogs-real-{tier}-comparison.json").write_text(json.dumps({
            "tier": tier, "status": "not-run", "comparison": None,
            "baseline": f"evals/prompt_regression/baselines/dialogs-real-{tier}.json",
            "reason": "Real dialogs require a separate authorized credential and two repetitions per case.",
            "release_ready": False}, indent=2))
    monkeypatch.setattr(shutil, "which", lambda name: "/test/claude" if name == "claude" else None)
    env = {"LOOPER_REGISTRY_SMOKE_COMBINATIONS": json.dumps([{
        "runtime": "claude-code", "model": "exact[1m]", "protocol": "anthropic-messages",
        "base_url": "https://example.invalid", "key_env": "TEST_KEY"}]), "TEST_KEY": "never-record-me"}
    rows = inventory(env)
    assert rows[0]["installed"] and rows[0]["credential"] and not rows[0]["missing"]
    assert rows[1]["missing"] == ["cli", "explicit API key"]
    assert all(r["status"] == "not-run" for r in rows)
    assert "never-record-me" not in json.dumps(rows)
    with pytest.raises(ValueError):
        combinations({"LOOPER_REGISTRY_SMOKE_COMBINATIONS": '[{"runtime":"typo"}]'})


@pytest.mark.parametrize("migrated", ["fresh", "bridge"], indirect=True)
def test_migrated_service_manager_creates_dashboard_reads(migrated, tmp_path):
    with server(tmp_path, migrated) as running:
        api = running.api
        cli = subprocess.run([sys.executable, "-m", "looper.loopercli", "--json", "config", "export", "-",
                              "--url", api.base_url, "--operator-token-file", str(running.runtime_root / "operator-token")],
                             cwd=ROOT, env=running.env, capture_output=True, text=True, timeout=15)
        assert cli.returncode == 0, cli.stderr
        assert "looper-config/v4" in cli.stdout
        registry = api.post("/api/v1/registry/view", {})["result"]
        assert registry["profiles"]
        profile = next(p for p in registry["profiles"] if p["profile_id"] == "agent-claude-code-opus")
        assert profile["model"]["name"] == "opus"
        assert profile["model"]["protocol"] == "anthropic-messages"
        api.post("/api/v1/agent_settings/update_role", {"role": "manager", "values": {"profile_id": profile["profile_id"]}})
        project = api.post("/api/v1/project_admin/create", {"name": "runtime smoke", "work_dir": str(running.home.parent / "work")})["result"]["project"]
        dialog = dialogs.select(["model_custom"])[0]
        result = dialogs.run_dialog(api, dialog, database_url=migrated, project_id=project["project_id"],
                                    scenarios=tmp_path / "scenarios.json", mode="fake", timeout=30,
                                    runtime_root=running.runtime_root)
        (tmp_path / "manager-dialog-scripted.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        assert result["outcome"] == "pass", result
        # A second HTTP turn exercises resident session accounting.
        dialogs._ask(api, "smoke-continuation", "read back registry", timeout=30)
        rows = query(migrated, "SELECT * FROM llm_records WHERE agent_id = 'manager' AND task_id IS NULL ORDER BY id")
        assert len(rows) == 2 and all(r["status"] == "completed" for r in rows)
        assert rows[0]["session_id"] == rows[1]["session_id"]
        for row in rows:
            metadata = json.loads(row["metadata"])
            assert metadata["llm_model"]["name"] == "opus"
            assert metadata["llm_model"]["protocol"] == "anthropic-messages"
            assert metadata["system_prompt_mode"] == "replace"
            assert metadata["pricing"]["cost_source"] == row["cost_source"] == "runtime"
            assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"],
                    row["cache_creation_tokens"]) == (10, 5, 2, 1)
            assert row["model"] == "fake" and row["cost_usd"] == 0.01
            assert "sk-synthetic" not in json.dumps(metadata)
            prompt = metadata["system_prompt"]
            digest = hashlib.sha256(prompt.encode()).hexdigest()
            assert digest == metadata["system_prompt_sha256"]
            assert (running.runtime_root / ".local/prompts" / f"{digest}.md").read_text() == prompt
            current_role = query(migrated, "SELECT system_prompt FROM role_configs WHERE role='manager'")[0]["system_prompt"]
            assert current_role in prompt
            assert "## Looper's tools (required)" in prompt
            assert "opus" in metadata["command"]
        saved = query(migrated, "SELECT * FROM agent_sessions WHERE agent_id = 'manager'")[0]
        assert saved["session_id"] == rows[-1]["session_id"]
        assert json.loads(saved["session_totals"])["input_tokens"] == 20
        (tmp_path / "launch-records.json").write_text(json.dumps(rows, default=str, indent=2))
    with server(tmp_path, migrated) as restarted:
        dialogs._ask(restarted.api, "smoke-continuation", "read back after restart", timeout=30)
        row = query(migrated, "SELECT * FROM llm_records WHERE agent_id='manager' AND task_id IS NULL ORDER BY id DESC LIMIT 1")[0]
        assert row["session_id"] == saved["session_id"]
        assert row["input_tokens"] == 10 and row["output_tokens"] == 5
        totals = json.loads(query(migrated, "SELECT session_totals FROM agent_sessions WHERE agent_id='manager'")[0]["session_totals"])
        assert totals["input_tokens"] == 30 and totals["output_tokens"] == 15


def test_bridge_current_profile_is_preserved_before_launch(tmp_path, bridge_source):
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("not-run: disposable LOOPER_TEST_DATABASE_URL required")
    with fresh_database(admin) as (url, _):
        report = migrate(url, bridge_source)
        assert report["historical_records"] == 1
        with server(tmp_path, url) as running:
            running.api.post("/api/v1/agent_settings/update_role", {"role": "manager", "values": {"profile_id": "agent-claude-code-opus"}})
            dialogs._ask(running.api, "smoke-combination", "Reply OK without tools", timeout=30)
            record = query(url, "SELECT * FROM llm_records WHERE agent_id='manager' AND task_id IS NULL")[0]
            metadata = json.loads(record["metadata"])
            assert metadata["llm_model"]["name"] == "opus"
            assert metadata["llm_model"]["protocol"] == "anthropic-messages"
            assert record["status"] == "completed"
            capture = json.loads((tmp_path / "launch-capture.jsonl").read_text().splitlines()[-1])
            assert capture["argv"][capture["argv"].index("--model") + 1] == "opus"


@pytest.mark.installed
@pytest.mark.parametrize("migrated", ["fresh", "bridge"], indirect=True)
def test_migrated_dashboard_screenshot(migrated, tmp_path, monkeypatch, staged_dashboard_dir):
    playwright = pytest.importorskip("playwright.sync_api", reason="not-run: Playwright missing")
    with playwright.sync_playwright() as driver:
        executable = Path(driver.chromium.executable_path)
    if not executable.exists():
        pytest.skip("not-run: Chromium missing")
    with server(tmp_path, migrated) as running:
        assert running.env.get("PYTEST_LOOPER_STATIC_DIR") == str(staged_dashboard_dir)
        # Browser profile and HOME are disposable too.
        monkeypatch.setenv("HOME", str(running.home))
        with playwright.sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=str(executable), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                response = page.goto(running.api.base_url + "/#settings/profiles", wait_until="domcontentloaded", timeout=15000)
                assert response is not None and response.status == 200
                assert response.text() == (staged_dashboard_dir / "index.html").read_text()
                page.get_by_text("claude-code/opus", exact=True).first.wait_for(timeout=15000)
                page.screenshot(path=str(tmp_path / "migrated-dashboard.png"), full_page=True)
            finally:
                browser.close()


@pytest.mark.installed
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_selected_real_runtime_request_and_continuation(runtime, tmp_path, bridge_source):
    rows = inventory(os.environ)
    (tmp_path / "runtime-inventory.json").write_text(json.dumps(rows, indent=2))
    chosen = [i for i in combinations(os.environ) if i["runtime"] == runtime]
    if not chosen:
        pytest.skip(f"not-run: {runtime}: no explicit combination/credential; personal logins forbidden")
    completed = 0
    for index, item in enumerate(chosen):
        status = [r for r in rows if r["runtime"] == runtime][index]
        if status["missing"]:
            continue
        if ApiProtocol(item["protocol"]) not in ADAPTERS[runtime].protocols:
            pytest.fail("selected protocol is unsupported by this runtime")
        admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
        if not admin:
            pytest.skip("not-run: disposable LOOPER_TEST_DATABASE_URL required")
        case = tmp_path / str(index)
        with fresh_database(admin) as (url, _):
            report = migrate(url, bridge_source, real=item)
            case.mkdir(parents=True)
            (case / "migration-report.json").write_text(json.dumps(report, indent=2))
            try:
                _real_continuation(case, url, item, status, rows, tmp_path)
                completed += 1
            except Exception:
                status["status"] = "fail"
                raise
            finally:
                (tmp_path / "runtime-inventory.json").write_text(json.dumps(rows, indent=2))
    if not completed:
        pytest.skip(f"not-run: {runtime}: selected combinations have missing CLI, credential or binding")


def _real_continuation(case, url, item, status, rows, evidence):
    with server(case, url, real=item) as running:
        api = running.api
        profile = next(p for p in api.post("/api/v1/registry/view", {})["result"]["profiles"] if p["profile_id"] == "agent-smoke-real")
        assert profile["model"]["name"] == item["model"]
        assert profile["model"]["protocol"] == item["protocol"]
        api.post("/api/v1/agent_settings/update_role", {"role": "manager", "values": {"profile_id": profile["profile_id"]}})
        marker = hashlib.sha256(str(case).encode()).hexdigest()[:12]
        first = dialogs._ask(api, "smoke-real", f"Remember {marker}. Reply only OK. Do not use tools.", timeout=90)
        previous = query(url, "SELECT session_id,session_totals FROM agent_sessions WHERE agent_id='manager'")[0]
        assert previous["session_id"]
    with server(case, url, real=item) as running:
        second = dialogs._ask(running.api, "smoke-real", "What marker did I just give you? Reply only the marker.", timeout=90)
        assert first and marker in second
        records = query(url, "SELECT * FROM llm_records WHERE agent_id='manager' AND task_id IS NULL ORDER BY id DESC LIMIT 2")
        assert len(records) == 2 and records[0]["session_id"] == records[1]["session_id"] == previous["session_id"]
        assert json.loads(records[0]["metadata"])["pricing"] == json.loads(records[1]["metadata"])["pricing"]
        for record in records:
            metadata = json.loads(record["metadata"])
            assert record["status"] == "completed" and record["input_tokens"] > 0
            assert metadata["llm_model"]["name"] == item["model"]
            assert metadata["llm_model"]["protocol"] == item["protocol"]
            assert metadata["system_prompt_mode"] == "replace"
            assert metadata["pricing"]["cost_source"] == record["cost_source"]
            assert hashlib.sha256(metadata["system_prompt"].encode()).hexdigest() == metadata["system_prompt_sha256"]
            assert os.environ[item["key_env"]] not in json.dumps(metadata)
        totals = json.loads(query(url, "SELECT session_totals FROM agent_sessions WHERE agent_id='manager'")[0]["session_totals"])
        assert totals.get("input_tokens", 0) >= sum(r["input_tokens"] for r in records)
        status["status"] = "pass"
        (evidence / "runtime-inventory.json").write_text(json.dumps(rows, indent=2))
        (case / "records.json").write_text(json.dumps(records, default=str, indent=2))
