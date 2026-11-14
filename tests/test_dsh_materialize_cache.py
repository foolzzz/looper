"""Failed DSH catalog validation backs off without hiding later recovery."""

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
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
def test_catalog_failure_is_cached_for_sixty_seconds_then_recovers(library, monkeypatch, failure):
    now, calls = [100.0], []
    monkeypatch.setattr(dsh, "_clock", lambda: now[0], raising=False)

    def validate(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(dsh.subprocess, "run", validate)
    for elapsed in (0, 59.9):
        now[0] = 100.0 + elapsed
        with pytest.raises(RuntimeUnsupported, match="cannot validate DSH catalog"):
            dsh.materialize(library)
    assert len(calls) == 1
    now[0] = 160.0
    catalog = dsh.materialize(library)
    assert not catalog.loaded
    now[0] = 300.0
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
    assert len(calls) == 2


def test_concurrent_catalog_failures_share_one_validation(library, monkeypatch):
    calls = []

    def validate(*args, **kwargs):
        calls.append(args)
        time.sleep(0.05)
        raise OSError("unavailable")

    def launch():
        with pytest.raises(RuntimeUnsupported):
            dsh.materialize(library)

    monkeypatch.setattr(dsh.subprocess, "run", validate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: launch(), range(2)))
    assert len(calls) == 1
