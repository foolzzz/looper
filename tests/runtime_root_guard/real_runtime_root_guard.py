"""Refuse test access to the real user LoopX roots.

Tests must keep LoopX state under temporary directories. The owner's live
roots may be in use while the suite runs: the runtime root (``~/.loopx``, which
also holds the home registry and machine-scoped leases such as the Lark event
consumer locks under ``lark-consumers/``) by a running dispatcher or chat
server. The pre-``~/.loopx`` runtime root ``~/.codex/loopx`` stays protected so
that code still writing there fails here instead of on the owner's machine.
This guard never inspects any of these directories. It installs a
Python audit hook that checks the path arguments of the audited filesystem
events listed in ``_PATH_ARGUMENTS`` (``open``, ``os.mkdir``, ``os.remove``,
``os.rename``, ``os.listdir``, ``os.scandir``, ``shutil.rmtree`` and similar),
and refuses those under a protected root with ``PermissionError`` before the
operation runs. Each protected root is pinned in its normalized and its
``os.path.realpath`` form. Accessed paths are only made absolute and normalized
lexically; they are never resolved.

Covered: those events in the pytest process, and in Python subprocesses that
inherit ``PYTHONPATH`` and the session's report path. The ``sitecustomize``
shim next to this module installs the hook in those subprocesses.

Not covered:

- other LoopX state locations, such as a project's own ``.loopx`` directory or
  a runtime root passed by path, unless it is the ambient
  ``LOOPX_RUNTIME_ROOT``;
- metadata checks that raise no audit event: ``os.stat``, ``os.lstat``,
  ``os.access`` and ``os.readlink``, hence ``Path.exists()`` and
  ``Path.stat()``;
- paths relative to a ``dir_fd``;
- access through a symlink or another spelling of a path that does not
  normalize to a pinned form;
- subprocesses that drop ``PYTHONPATH`` or the report path, and non-Python
  subprocesses;
- reporting a refusal that the code under test catches and retries forever:
  nothing is written, but the test hangs instead of reaching the teardown
  that names it.

Every refusal is appended to the session's report file together with the
``PYTEST_CURRENT_TEST`` value it happened under, which names the test to charge
it to. This module is imported before pytest in child processes, so it must
depend on the standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

PROTECTED_ROOTS_ENV = "PYTEST_LOOPX_PROTECTED_RUNTIME_ROOTS"
REPORT_PATH_ENV = "PYTEST_LOOPX_RUNTIME_ROOT_GUARD_REPORT"

# Audited event -> positions of the path arguments to check. Reads count too: a
# test that reads the owner's live state is not hermetic either. os.symlink is
# checked at the link location only; its target string touches nothing.
_PATH_ARGUMENTS: dict[str, tuple[int, ...]] = {
    "open": (0,),
    "os.chdir": (0,),
    "os.chflags": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.link": (0, 1),
    "os.listdir": (0,),
    "os.lchflags": (0,),
    "os.lchmod": (0,),
    "os.mkdir": (0,),
    "os.removexattr": (0,),
    "os.remove": (0,),
    "os.rename": (0, 1),
    "os.rmdir": (0,),
    "os.scandir": (0,),
    "os.setxattr": (0,),
    "os.symlink": (1,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.walk": (0,),
    "glob.glob": (0,),
    "glob.glob/2": (0, 2),
    "pathlib.Path.glob": (0,),
    "pathlib.Path.rglob": (0,),
    "shutil.copyfile": (0, 1),
    "shutil.copymode": (0, 1),
    "shutil.copystat": (0, 1),
    "shutil.copytree": (0, 1),
    "shutil.move": (0, 1),
    "shutil.rmtree": (0,),
    "sqlite3.connect": (0,),
    "tempfile.mkdtemp": (0,),
    "tempfile.mkstemp": (0,),
}
# Positions of the ``dir_fd`` arguments of os.* events; the events report -1
# when a path is not relative to a directory descriptor. Relative paths with a
# directory descriptor are not checked.
_DIR_FD_ARGUMENTS: dict[str, tuple[int, ...]] = {
    "os.chmod": (2,),
    "os.chown": (3,),
    "os.link": (2, 3),
    "os.mkdir": (2,),
    "os.remove": (1,),
    "os.rename": (2, 3),
    "os.rmdir": (1,),
    "os.symlink": (2,),
    "os.utime": (3,),
}

_protected: tuple[str, ...] = ()
_report_path: str | None = None
_report_cursor = 0
_installed = False
_reporting = False


def legacy_home_roots(home: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return the retired LoopX runtime roots under ``home``: ``~/.codex/loopx``.

    The current roots come from ``looper.paths``; this module must not import it.
    """

    return (os.path.join(os.path.abspath(os.fspath(home)), ".codex", "loopx"),)


def configure(protected_roots: list[str], report_path: str) -> None:
    """Protect ``protected_roots`` here and in child processes that inherit the env.

    Each root is pinned in its normalized and its real form, so the physical
    spelling of a symlinked root is refused as well.
    """

    global _protected, _report_path, _report_cursor
    pinned: set[str] = set()
    for root in protected_roots:
        if root:
            pinned.add(os.path.normpath(os.path.abspath(root)))
            pinned.add(os.path.realpath(root))
    _protected = tuple(sorted(pinned))
    _report_path = report_path
    try:
        _report_cursor = os.stat(report_path).st_size
    except OSError:
        _report_cursor = 0
    os.environ[PROTECTED_ROOTS_ENV] = os.pathsep.join(_protected)
    os.environ[REPORT_PATH_ENV] = report_path


def install() -> None:
    """Install the audit hook once per process; audit hooks cannot be removed."""

    global _installed
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True


def install_from_environment() -> None:
    """Child-process entry point used by the ``sitecustomize`` shim.

    The hook is installed only when the environment names both protected roots
    and the session's report file.
    """

    roots = [root for root in os.environ.get(PROTECTED_ROOTS_ENV, "").split(os.pathsep) if root]
    report_path = os.environ.get(REPORT_PATH_ENV)
    if roots and report_path:
        configure(roots, report_path)
        install()


def protected_roots() -> tuple[str, ...]:
    return _protected


def record_test_id(record: dict[str, object]) -> str | None:
    """Return the node id a refusal was recorded under, without the phase suffix."""

    test = record.get("test")
    if not isinstance(test, str) or not test:
        return None
    return test.rsplit(" (", 1)[0]


def take_violations(
    nodeid: str | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split refusals recorded since the last call into ``nodeid``'s and the rest.

    A refusal is charged to the test named by the ``PYTEST_CURRENT_TEST`` value
    it was recorded under. A subprocess that outlives its test therefore cannot
    fail an unrelated test; its refusals, and those recorded under no test, land
    in the second list.
    """

    global _report_cursor
    if not _report_path:
        return [], []
    try:
        with open(_report_path, "rb") as report:
            report.seek(_report_cursor)
            data = report.read()
    except FileNotFoundError:
        return [], []
    complete = data[: data.rfind(b"\n") + 1]
    _report_cursor += len(complete)
    own: list[dict[str, object]] = []
    others: list[dict[str, object]] = []
    for line in complete.decode("utf-8", "replace").splitlines():
        with contextlib.suppress(ValueError):
            record = json.loads(line)
            (own if nodeid is not None and record_test_id(record) == nodeid else others).append(record)
    return own, others


_FIX_HINT = (
    "Keep LoopX state under tmp_path: give the registry a temporary "
    "'common_runtime_root', pass --runtime-root / runtime_root, or set HOME to "
    "a temporary directory (monkeypatch.setenv for code that resolves "
    "Path.home(), env= for CLI subprocesses)."
)


def _origin(record: dict[str, object]) -> str:
    if record.get("pid") == os.getpid():
        return "this test process"
    return f"child pid {record.get('pid')}: {' '.join(map(str, record.get('argv') or []))}"


def describe(nodeid: str, violations: list[dict[str, object]]) -> str:
    lines = [f"{nodeid} used a real LoopX root instead of a temporary one:"]
    for violation in violations[:10]:
        lines.append(f"  - {violation.get('event')} {violation.get('path')} ({_origin(violation)})")
    if len(violations) > 10:
        lines.append(f"  - ... {len(violations) - 10} more")
    lines.append(_FIX_HINT)
    return "\n".join(lines)


def describe_unattributed(records: list[dict[str, object]]) -> str:
    lines = [
        f"{len(records)} refused access(es) to a real LoopX root could not "
        "be charged to the test that was running when they were read:"
    ]
    for record in records[:20]:
        test = record_test_id(record) or "no test"
        lines.append(
            f"  - {record.get('event')} {record.get('path')} "
            f"(recorded under {test}; {_origin(record)})"
        )
    if len(records) > 20:
        lines.append(f"  - ... {len(records) - 20} more")
    lines.append(_FIX_HINT)
    return "\n".join(lines)


@contextlib.contextmanager
def protecting(root: Path, report_path: Path) -> Iterator[None]:
    """Additionally protect ``root`` and record refusals in ``report_path`` only.

    Guard self-tests use a temporary stand-in for the real root so that a broken
    guard can never write the owner's runtime root.
    """

    global _report_cursor
    saved_roots, saved_report, saved_cursor = _protected, _report_path, _report_cursor
    configure([*saved_roots, str(root)], str(report_path))
    try:
        yield
    finally:
        configure(list(saved_roots), saved_report or "")
        _report_cursor = saved_cursor


def _path_text(value: object) -> str | None:
    if isinstance(value, (str, bytes)) or hasattr(value, "__fspath__"):
        try:
            return os.fsdecode(os.fspath(value))
        except TypeError:
            return None
    return None


def _protected_root_of(path: str) -> str | None:
    path = os.path.normpath(path)
    for root in _protected:
        if path == root or path.startswith(root + os.sep):
            return root
    return None


def _audit(event: str, args: tuple[object, ...]) -> None:
    positions = _PATH_ARGUMENTS.get(event)
    if positions is None or not _protected or _reporting:
        return
    relative_to_fd = any(
        position < len(args) and args[position] not in (None, -1)
        for position in _DIR_FD_ARGUMENTS.get(event, ())
    )
    for position in positions:
        path = _path_text(args[position]) if position < len(args) else None
        if path is None:
            continue
        if not os.path.isabs(path):
            if relative_to_fd:
                continue
            try:
                path = os.path.join(os.getcwd(), path)
            except OSError:
                continue
        root = _protected_root_of(path)
        if root is not None:
            _refuse(event, path, root)


def _refuse(event: str, path: str, root: str) -> None:
    global _reporting
    _reporting = True
    try:
        record = {
            "event": event,
            "path": path,
            "root": root,
            "pid": os.getpid(),
            "argv": sys.argv[:4],
            "test": os.environ.get("PYTEST_CURRENT_TEST"),
        }
        if _report_path:
            with contextlib.suppress(OSError), open(_report_path, "a", encoding="utf-8") as report:
                report.write(json.dumps(record, default=str) + "\n")
    finally:
        _reporting = False
    raise PermissionError(
        errno.EACCES,
        f"test guard refused {event} under the real LoopX root {root}",
        path,
    )
