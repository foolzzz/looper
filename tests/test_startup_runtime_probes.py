"""Startup discovers runtimes concurrently and writes their results in registry order."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from types import SimpleNamespace

import pytest

from looper.agents import profile_registry as registry, runtimes
from looper.agents.runtimes import Capability, Launcher, Probe
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
        self.rows[runtime_id] = dict(fields)
        return True


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
    monkeypatch.setattr(runtimes, "ADAPTERS", {adapter.type: adapter for adapter in adapters})
    monkeypatch.setattr(shutil, "which", lambda executable: f"/stub/{executable}")
    monkeypatch.setattr(db, "get_runtime", store.get)
    monkeypatch.setattr(db, "insert_builtin_runtime", store.insert)


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


def test_runtime_probe_timeout_keeps_failure_reason_and_other_results(monkeypatch):
    adapters = [_adapter("slow", delay=1.2), _adapter("fast", delay=0.05),
                _adapter("failed", delay=0.05, error="version unavailable")]
    store = _RuntimeStore()
    _install(monkeypatch, adapters, store)
    monkeypatch.setattr(registry, "RUNTIME_PROBE_TIMEOUT_SECONDS", 0.5)

    async def run():
        started = time.monotonic()
        names = await registry.register_found_runtimes()
        return names, time.monotonic() - started

    names, elapsed = asyncio.run(run())
    assert elapsed < 1, f"timed-out probe held registration for {elapsed:.2f} s"
    assert names == ["slow", "fast", "failed"]
    assert store.writes == ["rt-slow", "rt-fast", "rt-failed"]
    assert store.rows["rt-slow"]["version"] is None
    assert store.rows["rt-slow"]["probe_error"] == "the probe did not answer within 0.5 s"
    assert store.rows["rt-fast"]["version"] == "fast 1.0"
    assert store.rows["rt-fast"]["probe_error"] is None
    assert store.rows["rt-failed"]["probe_error"] == "version unavailable"


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
