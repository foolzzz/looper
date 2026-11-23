"""Fail-closed launchers for acceptance checks without network access.

The macOS profile has been verified on macOS 26.6.2. Linux's ``unshare -rn``
path remains unverified on a target host; every invocation must pass the same
enforcement preflight. Neither launcher restricts file access by the service user.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import errno
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

from . import proc_tree

MACOS_PROFILE = "(version 1)(allow default)(deny network*)(deny job-creation)"
PREFLIGHT_TIMEOUT_SECONDS = 2

# No project imports or startup hooks: the probe must test the launcher, not the
# check's environment. Gate the external connect on local enforcement first so a
# passthrough launcher cannot turn the preflight into an unsandboxed request.
_PROBE = '''
import errno, json, os, socket, sys
result = {}
if sys.platform == "darwin":
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except OSError as exc:
            result["bind_errno"] = exc.errno
        else:
            result["bind_errno"] = 0
    if result["bind_errno"] != errno.EPERM:
        print(json.dumps(result))
        sys.exit(1)
else:
    result["namespace"] = os.readlink("/proc/self/ns/net")
    result["interfaces"] = [name for _, name in socket.if_nameindex()]
    if result["namespace"] == sys.argv[1] or result["interfaces"] != ["lo"]:
        print(json.dumps(result))
        sys.exit(1)
try:
    with socket.create_connection(("1.1.1.1", 53), 1):
        result["connect_errno"] = 0
except OSError as exc:
    result["connect_errno"] = exc.errno
print(json.dumps(result))
'''


async def _run_probe(argv: tuple[str, ...], work_dir: Path,
                     env: Mapping[str, str]) -> tuple[int | None, bytes] | None:
    process = None
    tree = None
    try:
        async with asyncio.timeout(PREFLIGHT_TIMEOUT_SECONDS):
            process = await proc_tree.spawn(
                *argv, cwd=work_dir, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            tree = proc_tree.track(process, label="acceptance sandbox preflight")
            assert process.stdout is not None
            # Fixed probes print less than 256 bytes. Bound even a broken launcher.
            output = await process.stdout.read(4097)
            if len(output) > 4096:
                return None
            await process.wait()
            return process.returncode, output
    except (OSError, TimeoutError):
        return None
    finally:
        if tree is not None:
            await tree.end()
        elif process is not None:
            await proc_tree.end(process, label="acceptance sandbox preflight")


async def local_prefix(work_dir: Path, env: Mapping[str, str]) -> tuple[str, ...] | None:
    """Return a verified launcher prefix, or None; never execute a check as fallback."""
    namespace = ""
    if sys.platform == "darwin":
        executable = shutil.which("sandbox-exec", path=env.get("PATH"))
        prefix = (executable, "-p", MACOS_PROFILE) if executable else None
    elif sys.platform == "linux":
        executable = shutil.which("unshare", path=env.get("PATH"))
        prefix = (executable, "-rn", "--") if executable else None
        try:
            namespace = os.readlink("/proc/self/ns/net")
        except OSError:
            return None
    else:
        return None
    if prefix is None:
        return None
    shell = (*prefix, "/bin/sh", "-c")
    if await _run_probe((*shell, "true"), work_dir, env) != (0, b""):
        return None
    command = shlex.join((sys.executable, "-I", "-S", "-c", _PROBE, namespace))
    reply = await _run_probe((*shell, command), work_dir, env)
    if reply is None or reply[0] != 0:
        return None
    try:
        result = json.loads(reply[1])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(result, dict):
        return None
    if sys.platform == "darwin":
        enforced = result.get("bind_errno") == result.get("connect_errno") == errno.EPERM
    else:
        enforced = (isinstance(result.get("namespace"), str) and result["namespace"] != namespace
                    and result.get("interfaces") == ["lo"]
                    and result.get("connect_errno") in (errno.EPERM, errno.ENETUNREACH))
    return prefix if enforced else None
