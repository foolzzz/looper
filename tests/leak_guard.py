"""Keep tests from leaving processes and launchd jobs behind when a run is aborted.

Finalizers cover failures and Ctrl-C. A SIGKILL or a timeout kill runs none, so
every process a test starts is also recorded here under its owner's pid, and the
next pytest session reaps records whose owner is gone.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

from looper.agents import proc_tree

LAUNCHD_PREFIX = "com.looper.test-upgrade-"
_RECORDS = Path(tempfile.gettempdir()) / f"looper-test-leaks-{os.getuid()}"


class PollingProcess:
    """A subprocess whose exit is polled, not awaited through asyncio's child watcher.

    On macOS with Python 3.14 the watcher thread treats a SIGSTOPped child as exited, and the
    event loop then blocks forever in ``waitpid``, so a test that stops a process hangs the suite.
    """

    def __init__(self, argv: list[str]):
        self._popen = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
        self.pid = self._popen.pid
        track(self.pid)

    @property
    def returncode(self) -> int | None:
        return self._popen.poll()

    def send_signal(self, sig: int) -> None:
        if self.returncode is None:
            self._popen.send_signal(sig)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    async def wait(self) -> int:
        while (code := self._popen.poll()) is None:
            await asyncio.sleep(0.02)
        return code


def _write(name: str, record: dict) -> None:
    _RECORDS.mkdir(mode=0o700, exist_ok=True)
    owner = os.getpid()
    (_RECORDS / name).write_text(json.dumps(
        {"owner": owner, "owner_started": proc_tree._start_time(owner), **record}))


def _command(pid: int) -> str | None:
    shown = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=10, check=False)
    return shown.stdout.strip() or None


def track(pid: int) -> None:
    """Record a process this session started, so a later session can reap it."""
    _write(f"{os.getpid()}-{pid}.json", {"pid": pid, "started": proc_tree._start_time(pid),
                                         "command": _command(pid)})


def _is_recorded_process(record: dict) -> bool:
    """True only when the pid still runs the very process that was recorded and leads its own group.

    A pid recycled by an unrelated process (possibly the operator's live service) differs in start
    time or command line, and is never signalled.
    """
    pid = record.get("pid")
    if not (isinstance(pid, int) and pid > 1 and record.get("started") and record.get("command")):
        return False
    if proc_tree._start_time(pid) != record["started"] or _command(pid) != record["command"]:
        print(f"leak_guard: pid {pid} is no longer the recorded process; left alone", file=sys.stderr)
        return False
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


def track_launchd(label: str) -> None:
    _write(f"{os.getpid()}-{label}.json", {"label": label})


def untrack(key: int | str) -> None:
    (_RECORDS / f"{os.getpid()}-{key}.json").unlink(missing_ok=True)


def _kill_group(pid: int) -> None:
    # The keeper leads its own session, so its group holds the keeper and its child.
    for sig in (signal.SIGCONT, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return


def reap_group(pid: int) -> None:
    """SIGKILL the process group led by ``pid``, then forget it."""
    _kill_group(pid)
    untrack(pid)


def _gui() -> str:
    return f"gui/{os.getuid()}"


def bootout(label: str) -> None:
    subprocess.run(["launchctl", "bootout", f"{_gui()}/{label}"], capture_output=True, timeout=20, check=False)


def _plist_of(label: str) -> Path | None:
    shown = subprocess.run(["launchctl", "print", f"{_gui()}/{label}"], capture_output=True, text=True,
                           timeout=20, check=False)
    found = re.search(r"^\s*path = (.+)$", shown.stdout, re.MULTILINE)
    return Path(found.group(1)) if found else None


def sweep() -> list[str]:
    """Reap what dead test sessions left behind; returns what it removed."""
    removed: list[str] = []
    for path in sorted(_RECORDS.glob("*.json")) if _RECORDS.is_dir() else []:
        try:
            record = json.loads(path.read_text())
            owner_started = proc_tree._start_time(record["owner"])
            if owner_started is not None and owner_started == record.get("owner_started"):
                continue
        except (OSError, ValueError, KeyError):
            path.unlink(missing_ok=True)
            continue
        if _is_recorded_process(record):
            _kill_group(record["pid"])
            removed.append(f"pid {record['pid']}")
        path.unlink(missing_ok=True)
    if sys.platform == "darwin":
        removed += sweep_launchd()
    return removed


def _under_temp(path: Path) -> bool:
    roots = {os.path.realpath(root) for root in ("/tmp", tempfile.gettempdir())}
    return any(os.path.realpath(path).startswith(root + os.sep) for root in roots)


def sweep_launchd() -> list[str]:
    """Unload ``com.looper.test-upgrade-*`` jobs whose plist is under a temp dir and no longer exists."""
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=20, check=False)
    removed = []
    for label in re.findall(rf"\t({re.escape(LAUNCHD_PREFIX)}[0-9a-f]+)$", listed.stdout, re.MULTILINE):
        plist = _plist_of(label)
        if plist is not None and not plist.exists() and _under_temp(plist):
            bootout(label)
            removed.append(label)
    return removed
