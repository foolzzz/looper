"""The on-disk and boot contract of a process holding one agent run."""

import json
import os
from enum import StrEnum
from pathlib import Path

RUNS_DIR = Path(".local") / "runs"
MANIFEST = "manifest.json"
STATE = "keeper.json"
STDOUT = "stdout.log"
STDERR = "stderr.log"
STDIN = "stdin.log"
DETACH = "detach.json"
GRANT = "grant.json"
SOCKET = "keeper.sock"
LOG = "keeper.log"
STATE_FORMAT = 1
PROTOCOL_VERSIONS: tuple[str, ...] = ()
DEFAULT_ORPHAN_TIMEOUT_S = 1800
DEFAULT_STDOUT_LIMIT_BYTES = 512 * 1024 ** 2
STDERR_LIMIT_BYTES = 64 * 1024 ** 2
CHUNK = 65536
SOCKET_PATH_MAX = 103


class KeeperState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"


class EndReason(StrEnum):
    EXITED = "exited"
    ENDED = "ended"
    ORPHANED = "orphaned"
    LOG_LIMIT = "log_limit"
    START_FAILED = "start_failed"
    KEEPER_ERROR = "keeper_error"


def _write_all(fd, data):
    remaining = memoryview(data)
    while remaining:
        count = os.write(fd, remaining)
        if count <= 0:
            raise OSError("write made no progress")
        remaining = remaining[count:]


def write_json(path, data):
    directory, name = os.path.split(path)
    temporary = os.path.join(directory, f".{name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, json.dumps(data, ensure_ascii=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from looper.agents.keeper.__main__ import main; sys.exit(main(sys.argv[2:]))"


def boot_argv(executable, package_root, run_dir, runtime_root, cwd, argv) -> list[str]:
    return [executable, "-I", "-c", BOOT, str(package_root), "--dir", str(run_dir),
            "--runtime-root", str(runtime_root), "--cwd", cwd, "--", *argv]


def check_argv(executable, package_root) -> list[str]:
    return [executable, "-I", "-c", BOOT, str(package_root), "--check"]
