"""pytest integration of the real-runtime-root guard.

Importing this module starts the guard for the pytest process. tests/conftest.py
imports it before anything else and re-exports its fixture and hooks; a
separate pytest process can load it with ``-p real_runtime_root_guard_plugin``
when this directory is on ``sys.path``.

A refusal fails the test it was recorded under, at that test's teardown.
Refusals recorded under another test or under no test are reported once for
the whole session, which then exits with a failure status. Under xdist each
worker hands those refusals to the controller.

The session environment also puts the source tree under test on PYTHONPATH,
ahead of any earlier entries, so a Python subprocess that inherits it imports
``looper`` from that tree rather than from wherever the environment's editable
install points. Python searches the script directory (or, for ``-c`` and
``-m``, the cwd) before PYTHONPATH, so a subprocess started from inside
another checkout still imports that checkout's ``looper``. A directory whose
path contains ``os.pathsep`` cannot be a PYTHONPATH entry and is left out.
"""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

import pytest

import real_runtime_root_guard as guard

GUARD_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = GUARD_DIR.parents[1]
_WORKER_OUTPUT_KEY = "looper_runtime_root_unattributed"
_unattributed: list[dict[str, object]] = []


def session_roots() -> list[str]:
    """Return the real Looper roots this session protects.

    They are computed with the resolver of the tree under test, before any test
    can monkeypatch HOME: the default runtime root of the starting HOME and of
    the account's home directory, the retired ``~/.loopx`` and ``~/.codex/loopx`` of both, and an
    ambient LOOPER_RUNTIME_ROOT.
    """

    if str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    from looper.paths import configured_runtime_root, home_runtime_root

    homes = [Path.home()]
    try:
        import pwd

        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError):
        pass
    roots = [str(home_runtime_root(home)) for home in homes]
    roots += [root for home in homes for root in guard.legacy_home_roots(home)]
    ambient = configured_runtime_root()
    if ambient is not None:
        roots.append(str(ambient))
    return roots


def _start() -> None:
    """Protect the real Looper roots for this process and its Python children.

    A nested pytest session inherits its parent's roots instead of protecting
    the temporary HOME it may run under.
    """

    inherited = os.environ.get(guard.PROTECTED_ROOTS_ENV)
    roots = inherited.split(os.pathsep) if inherited else session_roots()
    handle, report_path = tempfile.mkstemp(prefix="looper-runtime-root-guard-", suffix=".jsonl")
    os.close(handle)
    atexit.register(Path(report_path).unlink, missing_ok=True)
    guard.configure(roots, report_path)
    guard.install()
    # The guard's sitecustomize first, then the tree under test ahead of any
    # editable install of another checkout.
    os.environ["PYTHONPATH"] = python_path(GUARD_DIR, SOURCE_ROOT, existing=os.environ.get("PYTHONPATH"))


def python_path(*directories: Path, existing: str | None = None) -> str:
    """Directories first, then ``existing``; a directory containing os.pathsep is left out."""

    parts = [str(directory) for directory in directories if directory and os.pathsep not in str(directory)]
    return os.pathsep.join([*parts, *([existing] if existing else [])])


_start()


@pytest.fixture(autouse=True)
def _refuse_real_looper_runtime_root(request: pytest.FixtureRequest):
    yield
    own, others = guard.take_violations(request.node.nodeid)
    _unattributed.extend(others)
    if own:
        pytest.fail(guard.describe(request.node.nodeid, own), pytrace=False)


def pytest_sessionfinish(session: pytest.Session) -> None:
    _unattributed.extend(guard.take_violations(None)[1])
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKER_OUTPUT_KEY] = list(_unattributed)
        return
    if _unattributed and session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error) -> None:
    _unattributed.extend(getattr(node, "workeroutput", {}).get(_WORKER_OUTPUT_KEY, []))


def pytest_terminal_summary(terminalreporter) -> None:
    if _unattributed and not hasattr(terminalreporter.config, "workeroutput"):
        terminalreporter.section("real Looper roots", red=True, bold=True)
        terminalreporter.line(guard.describe_unattributed(_unattributed))
