"""Settings snapshots, confirmation and retired routes on disposable PostgreSQL."""

import json
import subprocess
from contextlib import asynccontextmanager

import pytest

from looper.agents import api_v1, scheduler_settings as scheduler, vault, waits
from looper.agents.bus import MessageBus
from looper.agents.ops import OpError
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.db import api_clients, projects, role_configs, system_settings
from tests.db_support import DSN, run_db
from tests.test_api_v1_auth import _bearer, _send, _serving
from tests.test_operator_auth import TOKEN

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_STORED_KEYS = {"scheduler_settings": [scheduler.MIN_INTERVAL_KEY, scheduler.RUN_TIMEOUT_KEY, scheduler.RUN_RETENTION_KEY],
                "wait_settings": [waits.MAX_WAIT_KEY, waits.MIN_CHECK_KEY], "knowledge_vault": [vault.SETTING_KEY]}


@pytest.fixture(autouse=True)
def _isolate_settings_cache(monkeypatch):
    # Database cleanup does not restore the process-wide caches changed by these requests.
    monkeypatch.setattr(waits, "_limits", waits.WaitLimits())
    monkeypatch.setattr(waits, "_sources", {})
    monkeypatch.setattr(scheduler, "_min_interval", scheduler.DEFAULT_MIN_INTERVAL_SECONDS)
    monkeypatch.setattr(scheduler, "_min_source", None)


@asynccontextmanager
async def _api(tmp_path, pool):
    _, token = await api_clients.create("Settings client", pool=pool)
    await scheduler.load()
    await waits.load()
    bus = MessageBus(runtime_root=tmp_path)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router, runtime_root=tmp_path))
        async def post(path, data=None, *, identity=TOKEN, session="settings"):
            status, _, body = await _send(server, f"POST /api/v1/{path}",
                {**_bearer(identity), "X-Looper-Session": session}, json.dumps(data or {}))
            if status == 200 and path != "confirm":
                schema = api_v1.find("POST", f"/api/v1/{path}").result
                if "oneOf" in schema:
                    # The request validator closes the root, so validate each closed result variant.
                    matches = 0
                    for variant in schema["oneOf"]:
                        try:
                            api_v1.validate_arguments(variant, body["result"])
                            matches += 1
                        except OpError:
                            pass
                    assert matches == 1
                else:
                    api_v1.validate_arguments(schema, body["result"])
            return status, body
        try:
            yield server, post, token
        finally:
            await bus.shutdown()


def _vault(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "writable").mkdir()
    (root / "rules.md").write_text("Example rules\n")
    return {"path": str(root), "writable": ["writable"], "rules_file": "rules.md"}


@pytest.mark.parametrize("family,fields", [
    ("scheduler_settings", {"min_interval_seconds", "run_timeout_seconds", "run_retention_days"}),
    ("wait_settings", {"max_wait_seconds", "min_check_seconds"}),
    ("knowledge_vault", {"configured", "path", "writable", "rules_file", "git", "problem"}),
])
@pytest.mark.parametrize("client", [False, True])
def test_full_get_views_are_nonempty_and_typed(tmp_path, monkeypatch, family, fields, client):
    async def run(pool):
        await vault.update_settings(_vault(tmp_path))
        async with _api(tmp_path, pool) as (_, post, token):
            status, body = await post(f"{family}/get", identity=token if client else TOKEN)
            assert status == 200 and set(body["result"]) == fields
            if family == "knowledge_vault":
                assert body["result"]["configured"] is True
            else:
                assert all(entry["value"] is not None for name, entry in body["result"].items()
                           if name != "run_timeout_seconds")
    run_db(run, monkeypatch, timeout=20)


NUMERIC_RULES = [
    ("scheduler_settings", "min_interval_seconds", 1, None),
    ("scheduler_settings", "run_timeout_seconds", 1, scheduler.RUN_TIMEOUT_MAX_SECONDS),
    ("scheduler_settings", "run_retention_days", 1, scheduler.RUN_RETENTION_MAX_DAYS),
    ("wait_settings", "max_wait_seconds", *waits.MAX_WAIT_RANGE),
    ("wait_settings", "min_check_seconds", *waits.MIN_CHECK_RANGE),
]


@pytest.mark.parametrize("family,field,low,high", NUMERIC_RULES)
@pytest.mark.parametrize("variant", ["minimum", "maximum", "below", "above", "bool", "null"])
def test_numeric_rules_preserve_bounds_types_and_defaults(tmp_path, monkeypatch, family, field, low, high, variant):
    async def run(pool):
        value = {"minimum": low, "maximum": high or 10**9, "below": low - 1,
                 "above": high + 1 if high else 10**9, "bool": True, "null": None}[variant]
        async with _api(tmp_path, pool) as (_, post, _):
            before = (await post(f"{family}/get"))[1]["result"]
            # Each wait bound is valid when the companion field is set consistently.
            data = {field: value}
            if family == "wait_settings":
                data["min_check_seconds" if field == "max_wait_seconds" else "max_wait_seconds"] = (
                    waits.MIN_CHECK_RANGE[0] if field == "max_wait_seconds" else waits.MAX_WAIT_RANGE[1])
            status, body = await post(f"{family}/update", data)
            valid = variant not in {"below", "bool"} and (variant != "above" or high is None)
            assert status == (200 if valid else 400), body
            after = (await post(f"{family}/get"))[1]["result"]
            if valid:
                assert after[field]["value"] == (before[field].get("built_in", before[field].get("default"))
                                                  if value is None else value)
                assert after[field]["source"] == ("built-in" if value is None else "setting")
            else:
                assert body["error"]["code"] == "invalid" and after == before
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("family", ["scheduler_settings", "wait_settings", "knowledge_vault"])
def test_client_confirmation_is_isolated_and_abandoned_changes_do_not_persist(tmp_path, monkeypatch, family):
    async def run(pool):
        data = {"scheduler_settings": {"min_interval_seconds": 77},
                "wait_settings": {"min_check_seconds": 77}, "knowledge_vault": _vault(tmp_path)}[family]
        _, other = await api_clients.create("Other client", pool=pool)
        async with _api(tmp_path, pool) as (_, post, token):
            before = (await post(f"{family}/get"))[1]["result"]
            keys = _STORED_KEYS[family]
            stored_before = await system_settings.get_settings(keys)
            status, pending = await post(f"{family}/update", data, identity=token)
            assert status == 202 and pending["status"] == "needs_confirmation" and pending["impact"]
            assert (await post(f"{family}/get"))[1]["result"] == before
            assert await system_settings.get_settings(keys) == stored_before
            confirmation = {"confirmation_id": pending["confirmation_id"]}
            for options in ({"identity": other}, {"identity": token, "session": "foreign"}, {}):
                assert (await post("confirm", confirmation, **options))[0] == 404
                assert (await post(f"{family}/get"))[1]["result"] == before
                assert await system_settings.get_settings(keys) == stored_before
            # A replacement request abandons the previous pending choice without applying it.
            status, pending = await post(f"{family}/update", data, identity=token)
            assert status == 202 and (await post("confirm", confirmation, identity=token))[0] == 404
            assert (await post(f"{family}/get"))[1]["result"] == before
            status, body = await post("confirm", {"confirmation_id": pending["confirmation_id"]}, identity=token)
            assert status == 200 and body["result"] != before
            assert await system_settings.get_settings(keys) != stored_before
            await scheduler.load()
            await waits.load()
            assert body["result"] == (await post(f"{family}/get"))[1]["result"]
            assert (await post(f"{family}/update", data))[0] == 200
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("family,data", [
    ("scheduler_settings", {}), ("wait_settings", {}),
    ("scheduler_settings", {"unknown": 1}), ("wait_settings", {"unknown": 1}),
    ("wait_settings", {"max_wait_seconds": 60, "min_check_seconds": 61}),
    ("knowledge_vault", {}), ("knowledge_vault", {"path": None, "git": True}),
])
def test_invalid_updates_preserve_the_full_snapshot(tmp_path, monkeypatch, family, data):
    async def run(pool):
        async with _api(tmp_path, pool) as (_, post, token):
            before = (await post(f"{family}/get"))[1]["result"]
            status, body = await post(f"{family}/update", data, identity=token)
            assert status == 400 and body["error"]["code"] == "invalid"
            assert (await post(f"{family}/get"))[1]["result"] == before
    run_db(run, monkeypatch, timeout=20)


def test_confirmation_revalidates_current_numeric_constraints(tmp_path, monkeypatch):
    async def run(pool):
        async with _api(tmp_path, pool) as (_, post, token):
            status, pending = await post("wait_settings/update", {"max_wait_seconds": 300}, identity=token)
            assert status == 202
            assert (await post("wait_settings/update", {"min_check_seconds": 400}))[0] == 200
            before = await system_settings.get_settings([scheduler.MIN_INTERVAL_KEY, waits.MAX_WAIT_KEY, waits.MIN_CHECK_KEY])
            status, body = await post("confirm", {"confirmation_id": pending["confirmation_id"]}, identity=token)
            assert status == 400 and body["error"]["code"] == "invalid"
            assert await system_settings.get_settings([scheduler.MIN_INTERVAL_KEY, waits.MAX_WAIT_KEY, waits.MIN_CHECK_KEY]) == before
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("variable", ["LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", "LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS"])
def test_scheduler_null_restores_builtin_despite_retired_environment(tmp_path, monkeypatch, variable):
    monkeypatch.delenv("LOOPER_SCHEDULE_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("LOOPX_SCHEDULE_MIN_INTERVAL_SECONDS", raising=False)
    monkeypatch.setenv(variable, "93")
    async def run(pool):
        async with _api(tmp_path, pool) as (_, post, _):
            initial = (await post("scheduler_settings/get"))[1]["result"]
            assert initial["min_interval_seconds"]["source"] == "built-in"
            assert (await post("scheduler_settings/update", {"min_interval_seconds": 45}))[1]["result"]["min_interval_seconds"]["source"] == "setting"
            restored = (await post("scheduler_settings/update", {"min_interval_seconds": None}))[1]["result"]
            assert restored == initial and restored["min_interval_seconds"]["value"] == 300
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("invalid", ["relative", "missing", "writable", "rules", "clear_with_writable"])
def test_vault_invalid_path_writable_and_rules_do_not_write(tmp_path, monkeypatch, invalid):
    async def run(pool):
        data = _vault(tmp_path)
        await vault.update_settings(data)
        data = {**data, **{"relative": {"path": "relative"}, "missing": {"path": str(tmp_path / "missing")},
                "writable": {"writable": ["../outside"]}, "rules": {"rules_file": "missing.md"},
                "clear_with_writable": {"path": None}}[invalid]}
        async with _api(tmp_path, pool) as (_, post, token):
            before = await system_settings.get_settings([vault.SETTING_KEY])
            status, body = await post("knowledge_vault/update", data, identity=token)
            assert status == 400 and body["error"]["code"] == "invalid"
            assert await system_settings.get_settings([vault.SETTING_KEY]) == before
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("clear", [{"path": None}, {"path": None, "writable": [], "rules_file": None},
                                  {"path": None, "writable": [], "rules_file": ""}])
def test_vault_git_problem_clear_and_execution_revalidation_leave_notes_untouched(tmp_path, monkeypatch, clear):
    async def run(pool):
        data = _vault(tmp_path)
        subprocess.run(["git", "init", "-q", data["path"]], check=True, timeout=10)
        async with _api(tmp_path, pool) as (_, post, token):
            status, pending = await post("knowledge_vault/update", data, identity=token)
            assert status == 202
            root = tmp_path / "notes"
            (root / "writable").rmdir()
            status, body = await post("confirm", {"confirmation_id": pending["confirmation_id"]}, identity=token)
            assert status == 400 and body["error"]["code"] == "invalid"
            assert await system_settings.get_settings([vault.SETTING_KEY]) == {}
            (root / "writable").mkdir()
            status, body = await post("knowledge_vault/update", data)
            assert status == 200 and body["result"]["git"] is True
            (root / "writable").rmdir()
            problem = (await post("knowledge_vault/get"))[1]["result"]
            assert problem["configured"] is False and problem["problem"] and problem["path"] == data["path"]
            status, body = await post("knowledge_vault/update", clear)
            assert status == 200 and body["result"] == {"configured": False, "path": None, "writable": [],
                                                       "rules_file": None, "git": False, "problem": None}
            assert (root / "rules.md").read_text() == "Example rules\n"
            assert subprocess.run(["git", "-C", data["path"], "log", "--oneline"], capture_output=True,
                                  timeout=10).returncode != 0
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("project,role", [(False, None), (False, "manager"), (True, None)])
def test_compact_settings_view_validates_actual_global_and_project_variants(tmp_path, monkeypatch, project, role):
    async def run(pool):
        await role_configs.seed_defaults([{"role": name, "system_prompt": "Example"}
                                         for name in ("manager", "planner", "worker", "reviewer")])
        if project:
            await projects.create_project(name="Example", work_dir=str(tmp_path))
        async with _api(tmp_path, pool) as (_, post, _):
            status, body = await post("ops/settings/view", {**({"project": "Example"} if project else {}),
                                                           **({"role": role} if role else {})})
            result = body["result"]
            assert status == 200 and result["roles"]
            expected = {"project", "project_layer", "roles", "allowed_profiles", "allowed_profiles_note"} if project else {
                "scheduler", "waits", "shared_defaults", "roles", "knowledge_vault"}
            if role:
                expected.add("manager_session")
            assert set(result) == expected
            if not project:
                assert "git" not in result["knowledge_vault"]
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("action,key,data", [
    ("scheduler", "scheduler", {"run_timeout_seconds": 20}),
    ("waits", "waits", {"min_check_seconds": 20}),
    ("knowledge_vault", "knowledge_vault", {"path": None}),
])
def test_existing_compact_write_results_remain_actual_results(tmp_path, monkeypatch, action, key, data):
    async def run(pool):
        async with _api(tmp_path, pool) as (_, post, _):
            status, body = await post(f"ops/settings/{action}", {key: data})
            result = body["result"]
            assert status == 200 and result
            assert set(result) == ({"knowledge_vault"} if action == "knowledge_vault" else {"changes"})
            if action != "knowledge_vault":
                assert len(result["changes"]) == 1 and result["changes"][0]["setting"] == next(iter(data))
    run_db(run, monkeypatch, timeout=20)


@pytest.mark.parametrize("legacy,family", [("scheduler-settings", "scheduler_settings"),
    ("wait-settings", "wait_settings"), ("knowledge-vault", "knowledge_vault")])
@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_retired_routes_have_successful_v1_controls(tmp_path, monkeypatch, legacy, family, method):
    async def run(pool):
        data = {"scheduler_settings": {"run_timeout_seconds": 20},
                "wait_settings": {"min_check_seconds": 20}, "knowledge_vault": {"path": None}}[family]
        async with _api(tmp_path, pool) as (server, post, _):
            assert (await post(f"{family}/{'get' if method == 'GET' else 'update'}", {} if method == "GET" else data))[0] == 200
            assert (await _send(server, f"{method} /api/{legacy}", _bearer(TOKEN), json.dumps(data)))[0] == 404
    run_db(run, monkeypatch, timeout=20)
