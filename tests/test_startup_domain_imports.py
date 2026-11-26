"""The dashboard domain modules are imported before the event loop starts serving.

Their first import (more than a hundred modules, some pulling in heavy
dependencies such as cryptography) used to happen inside the ``http`` startup
step on the event-loop thread, where the loop watchdog logged multi-second
"event loop was blocked" warnings. The checks below run in a fresh process so
``sys.modules`` reflects what real startup does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from looper import ops_config

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runs in its own process: importing the test module must not pre-load the domains under test.
_PRELOAD_CHECK = r'''
import asyncio
import sys
from pathlib import Path

import looper.agents as agents_pkg
from looper.agents import api_v1, serve

domain_dir = Path(agents_pkg.__file__).parent / "v1_domains"
expected_modules = ["looper.agents.v1_domains"] + sorted(
    f"looper.agents.v1_domains.{path.stem}"
    for path in domain_dir.glob("*.py")
    if not path.stem.startswith("_")
)

def loaded_domain_modules():
    return sorted(
        name for name in sys.modules
        if name == "looper.agents.v1_domains" or name.startswith("looper.agents.v1_domains.")
    )

# Positive control: in a fresh process the serve entrypoint alone never imports a domain module.
assert loaded_domain_modules() == [], loaded_domain_modules()

captured = {}

def fake_run(coro):
    # The moment a real startup would hand control to the event loop.
    captured["at_loop_start"] = loaded_domain_modules()
    coro.close()  # the server coroutine never runs, so no database or socket is needed

asyncio.run = fake_run
serve.serve_agent(runtime_root=Path(sys.argv[1]), work_dir=Path(sys.argv[1]),
                  config=Path(sys.argv[2]))

# Every discovered domain module is already imported before the loop starts.
assert captured["at_loop_start"] == expected_modules, (
    "domains not preloaded before the event loop:", captured["at_loop_start"])

# The http startup step resolves routes with api_v1.find(...) before accepting
# requests; it must not perform a first-time domain import anymore.
before = set(sys.modules)
assert api_v1.find("GET", f"{api_v1.PREFIX}/openapi.json") is not None
first_imports_during_route_resolution = sorted(
    name for name in sys.modules if name not in before and name.startswith("looper.agents.v1_domains"))
assert first_imports_during_route_resolution == [], first_imports_during_route_resolution

# The preload uses the same discovery result the registry serves from.
from looper.agents.v1_domains import MODULES, _DOMAINS

assert tuple(domain.__name__ for domain in api_v1.domains()) == tuple(
    domain.__name__ for domain in _DOMAINS)
assert [domain.__name__.rsplit(".", 1)[-1] for domain in api_v1.domains()] == list(MODULES)
'''


def test_serve_preloads_domain_modules_before_the_event_loop(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = tmp_path / ops_config.FILE
    ops = ops_config.OpsConfig(
        runtime_root=runtime, path=config,
        database_url="postgresql://looper:pw@127.0.0.1:9/none",
    )
    ops_config.write_private(config, ops_config.render(ops))

    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", _PRELOAD_CHECK, str(runtime), str(config)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
