"""Default-off bytes use independent expectations recorded from a fixed source."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.memory_lesson_injection_surfaces import surfaces, digest

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/memory_lesson_injection_off.json').read_text())


@pytest.mark.parametrize('key', sorted(FIXTURE['surfaces']))
def test_each_disabled_surface_matches_fixed_revision(key):
    assert FIXTURE['revision'] == 'ad62143e7fc02afa840cd6771a730836d72fed89'
    assert digest(surfaces()[key]) == FIXTURE['surfaces'][key], key


@pytest.mark.parametrize('stored,project', [(None, 'on'), ('{}', 'on'), ('broken', 'on'),
                                         ('{"enabled":true}', None), ('{"enabled":true}', 'off')])
def test_disabled_runtime_does_not_read_or_initialize_snapshots(monkeypatch, stored, project):
    from looper.agents import memory_lesson_injection as lessons
    from looper.db import projects, system_settings, memory_lesson_injection as store

    async def settings(*args, **kwargs):
        return {} if stored is None else {'memory_lessons': stored}
    async def get_project(*args, **kwargs):
        return {'id': 'synthetic-project', 'memory_lessons': project}
    async def forbidden(*args, **kwargs):
        pytest.fail('disabled lesson runtime touched snapshots')
    monkeypatch.setattr(system_settings, 'get_settings', settings)
    monkeypatch.setattr(projects, 'get_project', get_project)
    monkeypatch.setattr(store, 'ensure_snapshot', forbidden)
    monkeypatch.setattr(store, 'render_snapshot', forbidden)
    async def check():
        assert not (await lessons.ensure_snapshot('synthetic-task', 'synthetic-project')).text
        for role in ('planner', 'worker', 'reviewer', 'manager'):
            assert not (await lessons.render_for_run('synthetic-task', 'synthetic-project', role)).text
    asyncio.run(check())
