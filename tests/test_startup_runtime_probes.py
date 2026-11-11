"""Startup discovers runtimes concurrently and writes their results in registry order."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fake_claude import fake_claude
from looper.agents import profile_registry as registry, runtimes
from looper.agents.runtimes import ApiProtocol, AuthKind, Capability, Launcher, Probe, VendorType, coco_login
from looper.db import profile_registry as db


class _RuntimeStore:
    def __init__(self):
        self.rows = {}
        self.writes = []

    async def get(self, runtime_id):
        return self.rows.get(runtime_id)

    async def insert(self, runtime_id, fields):
        self.writes.append(runtime_id)
        if runtime_id in self.rows or any(row["name"] == fields["name"] for row in self.rows.values()):
            return False
        self.rows[runtime_id] = {"id": runtime_id, "builtin": True, "probed_at": None,
                                 "created_at": None, "updated_at": None, **fields}
        return True

    async def update(self, runtime_id, fields):
        self.rows[runtime_id].update(fields)

    async def list(self):
        return list(self.rows.values())


def _adapter(runtime_type, *, delay, error=None, finished=None):
    capabilities = frozenset({Capability.RUN})

    def probe(executable, launcher):
        assert executable == runtime_type
        assert launcher is Launcher.NATIVE
        time.sleep(delay)
        if finished is not None:
            finished.append(runtime_type)
        return Probe(None if error else f"{runtime_type} 1.0", capabilities, error)

    return SimpleNamespace(type=runtime_type, default_executable=runtime_type, label=runtime_type,
                           in_process=False, launchers=frozenset({Launcher.NATIVE}),
                           capabilities=capabilities, probe_for=probe)


def _install(monkeypatch, adapters, store):
    cursor = runtimes.adapter("cursor-agent")
    by_type = {adapter.type: adapter for adapter in adapters}
    monkeypatch.setattr(runtimes, "adapter", {"cursor-agent": cursor, **by_type}.__getitem__)
    monkeypatch.setattr(runtimes, "ADAPTERS", by_type)
    monkeypatch.setattr(db, "refresh_builtin_capabilities", AsyncMock())
    monkeypatch.setattr(shutil, "which", lambda executable: f"/stub/{executable}")
    monkeypatch.setattr(db, "get_runtime", store.get)
    monkeypatch.setattr(db, "insert_builtin_runtime", store.insert)
    monkeypatch.setattr(db, "update_runtime", store.update)
    monkeypatch.setattr(db, "list_runtimes", store.list)


def test_runtime_probes_overlap_and_match_sequential_registration(monkeypatch, caplog):
    finished = []
    adapters = [_adapter(name, delay=1, error="version unavailable" if name == "beta" else None,
                         finished=finished) for name in ("alpha", "beta", "gamma")]
    concurrent = _RuntimeStore()
    _install(monkeypatch, adapters, concurrent)

    async def insert_after_probes(runtime_id, fields):
        assert len(finished) == len(adapters), "write results only after every probe returned"
        return await concurrent.insert(runtime_id, fields)

    monkeypatch.setattr(db, "insert_builtin_runtime", insert_after_probes)

    async def run():
        started = time.monotonic()
        names = await registry.register_found_runtimes()
        return names, time.monotonic() - started

    with caplog.at_level(logging.INFO, logger=registry.__name__):
        names, elapsed = asyncio.run(run())
    assert elapsed < 2, f"three one-second probes took {elapsed:.2f} s"
    assert names == ["alpha", "beta", "gamma"]
    assert concurrent.writes == ["rt-alpha", "rt-beta", "rt-gamma"]
    for name in names:
        assert any(message.startswith(f"probed runtime {name} in ") and message.endswith(" s")
                   for message in caplog.messages)
    assert any(message.startswith("probed runtimes in ") and message.endswith(" s total")
               for message in caplog.messages)

    sequential = _RuntimeStore()
    _install(monkeypatch, adapters, sequential)

    async def run_sequential():
        return [adapter.type for adapter in adapters if await registry._detect(adapter)]

    assert asyncio.run(run_sequential()) == names
    assert sequential.rows == concurrent.rows
    assert sequential.writes == concurrent.writes


def test_runtime_probes_queued_on_one_thread_keep_their_finished_results(monkeypatch):
    finished = []
    adapters = [_adapter("slow", delay=0.3, finished=finished),
                _adapter("fast", delay=0, finished=finished),
                _adapter("failed", delay=0, error="version unavailable", finished=finished)]
    store = _RuntimeStore()
    _install(monkeypatch, adapters, store)
    # Scale the former outer deadline down; adapter subprocess budgets own the timeout now.
    monkeypatch.setattr(registry, "RUNTIME_PROBE_TIMEOUT_SECONDS", 0.05, raising=False)

    async def run():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        return await registry.register_found_runtimes()

    names = asyncio.run(run())
    assert names == ["slow", "fast", "failed"]
    assert store.writes == ["rt-slow", "rt-fast", "rt-failed"]
    assert finished == names  # Even the probes queued past the former deadline ran to completion.
    assert store.rows["rt-slow"]["version"] == "slow 1.0"
    assert store.rows["rt-slow"]["probe_error"] is None
    assert store.rows["rt-fast"]["version"] == "fast 1.0"
    assert store.rows["rt-fast"]["probe_error"] is None
    assert store.rows["rt-failed"]["probe_error"] == "version unavailable"


@pytest.mark.parametrize("entry", ["startup", "redetect"])
@pytest.mark.parametrize("runtime_type,launcher", [
    ("omp", Launcher.NATIVE), ("pi", Launcher.NATIVE), ("dsh", Launcher.NATIVE),
    ("codex", Launcher.NATIVE), ("codex", Launcher.TTADK),
])
def test_slow_probe_persists_version_gated_capabilities(monkeypatch, entry, runtime_type, launcher):
    adapter = runtimes.adapter(runtime_type)
    if runtime_type == "codex":
        version = "codex-cli 0.162.0" if launcher is Launcher.NATIVE else "ttadk 1.0 / codex-cli 0.162.0"
        expected = Probe(version, adapter.capabilities | {Capability.STEER})
    else:
        expected = Probe(None, adapter.capabilities_for(None), "version unavailable")
    assert expected.capabilities != adapter.capabilities

    def slow_probe(executable, actual_launcher):
        assert executable == ("ttadk" if launcher is Launcher.TTADK else adapter.default_executable)
        assert actual_launcher is launcher
        time.sleep(0.15)
        return expected

    monkeypatch.setattr(adapter, "launchers", frozenset({launcher}))
    monkeypatch.setattr(adapter, "probe_for", slow_probe)
    monkeypatch.setattr(registry, "RUNTIME_PROBE_TIMEOUT_SECONDS", 0.05, raising=False)
    store = _RuntimeStore()
    _install(monkeypatch, [adapter], store)
    runtime_id = registry.builtin_runtime_id(adapter, launcher)
    name = adapter.ttadk_runtime_name if launcher is Launcher.TTADK else runtime_type

    async def run():
        if entry == "startup":
            assert await registry.register_found_runtimes() == [name]
        else:
            await store.insert(runtime_id, {"name": name, "type": runtime_type, "executable": "",
                                           "launcher": launcher.value, "version": "previous",
                                           "capabilities": [], "probe_error": "previous error"})
            answer = await registry.probe_runtime(runtime_id)
            assert answer["runtime"]["version"] == expected.version
            assert answer["runtime"]["capabilities"] == sorted(c.value for c in expected.capabilities)
            assert answer["runtime"]["probe_error"] == expected.error
        saved = store.rows[runtime_id]
        assert saved["version"] == expected.version
        assert saved["capabilities"] == sorted(c.value for c in expected.capabilities)
        assert saved["probe_error"] == expected.error
        # A later startup skips this row, so its first persisted result must be the finished probe.
        assert await registry.register_found_runtimes() == []

    asyncio.run(run())


@pytest.mark.parametrize("unavailable", ["already-registered", "not-on-path", "name-taken"])
def test_runtime_discovery_preserves_existing_rows(monkeypatch, unavailable):
    adapter = _adapter("alpha", delay=0)
    store = _RuntimeStore()
    _install(monkeypatch, [adapter], store)
    if unavailable == "not-on-path":
        monkeypatch.setattr(shutil, "which", lambda executable: None)
    else:
        existing_id = "rt-alpha" if unavailable == "already-registered" else "custom-alpha"
        store.rows[existing_id] = {"name": "alpha", "version": "retained", "probe_error": "retained"}
    before = {key: dict(value) for key, value in store.rows.items()}

    assert asyncio.run(registry.register_found_runtimes()) == []
    assert store.rows == before


@pytest.mark.parametrize("entry,state", [
    ("startup", "logged_in"), ("registered-startup", "logged_in"), ("redetect", "logged_in"),
    ("startup", "not_logged_in"), ("startup", "unknown"), ("startup", "failed-catalog"),
])
def test_coco_registration_warms_the_model_catalog_off_the_event_loop(tmp_path, monkeypatch, entry, state):
    adapter = runtimes.adapter("coco")
    store = _RuntimeStore()
    _install(monkeypatch, [adapter], store)
    target = tmp_path / "operator-auth.json"
    target.write_text("stand-in login")
    monkeypatch.setattr(coco_login, "login_file", lambda: target)
    monkeypatch.setattr(coco_login, "recorded_problem", AsyncMock(return_value=None))
    calls = tmp_path / "calls"
    executable = fake_claude(tmp_path / "coco", (
        "import sys\nfrom pathlib import Path\n"
        f"with Path({str(calls)!r}).open('a') as stream: stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['--version']: print('coco 0.214.1')\n"
        "elif sys.argv[1:] == ['login', 'status']:\n"
        f"    print({'Not logged in' if state == 'not_logged_in' else 'Unexpected' if state == 'unknown' else 'Logged in using Trae'!r})\n"
        f"    raise SystemExit({1 if state == 'not_logged_in' else 2 if state == 'unknown' else 0})\n"
        "elif sys.argv[1:] == ['models', '--json']:\n"
        "    print('[{\"name\": \"GPT-5.5\"}]')\n"
        f"    raise SystemExit({1 if state == 'failed-catalog' else 0})\n"
        "else: raise AssertionError(sys.argv)\n"
    ))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(shutil, "which", lambda name: executable if name == "coco" else None)
    run_process = subprocess.run

    def off_loop(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run_process(*args, **kwargs)
        pytest.fail("runtime and catalog probes must never start processes on the event loop")

    monkeypatch.setattr(subprocess, "run", off_loop)
    provider = registry.ProviderSpec("prov-coco", "coco-login", VendorType.COCO_LOGIN, AuthKind.OAUTH, "coco")
    endpoint = registry.Endpoint(provider.id, ApiProtocol.OPENAI_RESPONSES, "")
    models = {name: registry.ResolvedModel(
        registry.ModelSpec(name, provider.id, name, endpoint.protocol), provider, endpoint)
        for name in ("GPT-9", "GPT-5.5")}
    coco_login.forget_probe()
    coco_login.forget_models()

    async def run():
        if entry in {"registered-startup", "redetect"}:
            await store.insert("rt-coco", {"name": "coco", "type": "coco", "executable": executable,
                "launcher": "native", "version": "previous", "capabilities": [], "probe_error": None})
        if entry == "redetect":
            await registry.probe_runtime("rt-coco")
        else:
            assert await registry.register_found_runtimes() == (["coco"] if entry == "startup" else [])
        key = executable if entry in {"registered-startup", "redetect"} else "coco"
        runtime = registry.RuntimeSpec("rt-coco", "coco", "coco", key, adapter.capabilities)
        before = calls.read_text()
        # Reads remain structural even when a warmed catalog no longer lists the model.
        models["GPT-9"].check_runtime(runtime)
        if state == "logged_in":
            assert coco_login.cached_models(key) == {"gpt-5.5": "GPT-5.5"}
            with pytest.raises(registry.RegistryError, match="GPT-9.*GPT-5.5") as refused:
                await models["GPT-9"].check_available_runtime(runtime)
            assert refused.value.field == "model"
            assert await adapter.login_problem(models["GPT-9"].binding_provider(), executable=key) == str(refused.value)
            await models["GPT-5.5"].check_available_runtime(runtime)
            assert await adapter.login_problem(models["GPT-5.5"].binding_provider(), executable=key) is None
        else:
            assert coco_login.cached_models(key) is None
        assert calls.read_text() == before
        commands = before.splitlines()
        assert "login status" in commands
        assert ("models --json" in commands) == (state not in {"not_logged_in", "unknown"})

    try:
        asyncio.run(run())
    finally:
        coco_login.forget_probe()
        coco_login.forget_models()
