"""DSH catalog validation shares in-flight work and permits immediate recovery."""

import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
from looper.agents.runtimes import RuntimeUnsupported, dsh


@pytest.fixture
def library(tmp_path):
    directory = tmp_path / "plugin"
    skill = directory / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\nExample")
    return AgentLibrary(items=(LoadedItem(ItemKind.PLUGIN, "plugin", "version", "plugin"),),
                        plugin_dirs=(str(directory),))


@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("node", 20),
                                    SimpleNamespace(returncode=1, stdout=""),
                                    SimpleNamespace(returncode=0, stdout="invalid JSON")])
def test_catalog_failure_recovers_on_immediate_retry(library, monkeypatch, failure):
    calls = []

    def validate(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(dsh.subprocess, "run", validate)
    with pytest.raises(RuntimeUnsupported, match="cannot validate DSH catalog"):
        dsh.materialize(library)
    assert len(calls) == 1
    catalog = dsh.materialize(AgentLibrary(items=library.items, plugin_dirs=library.plugin_dirs))
    assert not catalog.loaded
    assert dsh.materialize(library) is catalog
    assert len(calls) == 2


def test_catalog_failure_does_not_block_a_different_executable(library, monkeypatch):
    calls = []

    def validate(*args, **kwargs):
        calls.append(args)
        raise OSError("unavailable")

    monkeypatch.setattr(dsh.subprocess, "run", validate)
    for executable in ("missing-dsh", "other-dsh", "missing-dsh"):
        with pytest.raises(RuntimeUnsupported):
            dsh.materialize(library, executable)
    assert len(calls) == 3


@pytest.mark.parametrize("fails", [False, True])
def test_concurrent_catalog_calls_share_one_validation(library, monkeypatch, fails):
    calls, entered, joined, release = [], Event(), Event(), Event()

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    def validate(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            if fails:
                raise OSError("unavailable")
        return SimpleNamespace(returncode=0, stdout="[]")

    def launch():
        if fails:
            with pytest.raises(RuntimeUnsupported, match="cannot validate DSH catalog"):
                dsh.materialize(library)
        else:
            return dsh.materialize(library)

    monkeypatch.setattr(dsh.subprocess, "run", validate)
    monkeypatch.setattr(dsh, "Future", ObservedFuture)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(launch)
        try:
            assert entered.wait(5)
            second = executor.submit(launch)
            assert joined.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) is second.result(timeout=5)
    assert len(calls) == 1
    catalog = dsh.materialize(library)
    assert dsh.materialize(library) is catalog
    assert len(calls) == (2 if fails else 1)
