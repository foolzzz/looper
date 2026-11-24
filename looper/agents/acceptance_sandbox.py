"""Fail-closed launchers for acceptance checks without network access.

The macOS profile has been verified on macOS 26.6.2. Linux's ``unshare -rn``
path remains unverified on a target host; every invocation must pass the same
enforcement preflight. A new network namespace may also contain the kernel's
down, unlinked tunnel fallbacks (``tunl0``, ``gre0``, and the rest). Those are
not host interfaces. Anything up, addressed, or linked to another device is
not enforcement. When the kernel refuses the unprivileged user namespace,
passwordless ``sudo`` may open the network namespace; the check then continues
as the same host user with no capabilities. Neither launcher restricts file
access by the service user.
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
    result["euid"] = os.geteuid()
    try:
        result["uid_map"] = open("/proc/self/uid_map").read()
    except OSError:
        result["uid_map"] = ""
    names = [name for _, name in socket.if_nameindex()]
    result["interfaces"] = names
    links = {}

    def host_uid():
        euid = result["euid"]
        if not isinstance(euid, int) or isinstance(euid, bool) or euid < 0:
            return None
        for line in result["uid_map"].splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            try:
                inside, outside, count = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            if count > 0 and inside <= euid < inside + count:
                return outside + (euid - inside)
        return None

    try:
        expected_uid = int(sys.argv[2])
    except (IndexError, ValueError):
        expected_uid = None
    # A root helper must already have dropped back to the caller. Host uid 0
    # here would be the real root, not the root of an unprivileged user namespace.
    isolated = (host_uid() == expected_uid and isinstance(result["namespace"], str)
                and result["namespace"] != sys.argv[1] and "lo" in names)

    def addressed(name):
        import fcntl
        import struct
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            fcntl.ioctl(sock, 0x8915, struct.pack("16sH14s", name.encode()[:15], socket.AF_INET, b"\\0" * 14))
        except OSError:
            pass
        else:
            return True
        finally:
            sock.close()
        try:
            text = open("/proc/net/if_inet6").read()
        except OSError:
            return True
        return any(line.split()[-1:] == [name] for line in text.splitlines() if line.split())

    for name in names:
        try:
            flags = int(open("/sys/class/net/" + name + "/flags").read(), 16)
            link = int(open("/sys/class/net/" + name + "/iflink").read())
        except OSError:
            isolated = False
            links[name] = {"up": None, "iflink": None, "addressed": None}
            continue
        has_address = addressed(name)
        links[name] = {"up": bool(flags & 1), "iflink": link, "addressed": has_address}
        # iflink 0 is an unconnected kernel tunnel. lo is the only other device a fresh netns may have.
        if name != "lo" and (flags & 1 or link != 0 or has_address):
            isolated = False
    result["links"] = links
    if not isolated:
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


def _which(name: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH"))


def _env_assignments(env: Mapping[str, str]) -> tuple[str, ...]:
    """Env assignments ``sudo`` would otherwise reset. The caller already removed credentials."""
    assignments = []
    for key, value in env.items():
        if (not key.isidentifier() or "\0" in value or "\n" in value or "\r" in value):
            continue
        assignments.append(f"{key}={value}")
    return tuple(assignments)


def _linux_prefixes(env: Mapping[str, str]) -> tuple[tuple[str, ...], ...]:
    """Launchers to prove, in order. Each one must pass the same preflight."""
    unshare = _which("unshare", env)
    if unshare is None:
        return ()
    prefixes = [(unshare, "-rn", "--")]
    # Root does not need a user namespace to open a network namespace.
    if os.geteuid() == 0:
        prefixes.append((unshare, "-n", "--"))
        return tuple(prefixes)
    sudo, setpriv, env_bin = _which("sudo", env), _which("setpriv", env), _which("env", env)
    if sudo is None or setpriv is None or env_bin is None:
        return tuple(prefixes)
    # setpriv runs before the probe and the check, so a refused uid_map cannot
    # leave the command running as host root.
    prefixes.append((
        sudo, "-n", unshare, "-n", "--",
        setpriv, f"--reuid={os.getuid()}", f"--regid={os.getgid()}", "--init-groups",
        "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", "--",
        env_bin, "-i", *_env_assignments(env),
    ))
    return tuple(prefixes)


async def _verified_prefix(prefix: tuple[str, ...], namespace: str, uid: int, work_dir: Path,
                           env: Mapping[str, str]) -> bool:
    shell = (*prefix, "/bin/sh", "-c")
    if await _run_probe((*shell, "true"), work_dir, env) != (0, b""):
        return False
    command = shlex.join((sys.executable, "-I", "-S", "-c", _PROBE, namespace, str(uid)))
    reply = await _run_probe((*shell, command), work_dir, env)
    if reply is None or reply[0] != 0:
        return False
    try:
        result = json.loads(reply[1])
    except (ValueError, UnicodeError):
        return False
    if not isinstance(result, dict):
        return False
    if sys.platform == "darwin":
        return result.get("bind_errno") == result.get("connect_errno") == errno.EPERM
    return _linux_enforced(result, namespace, uid)


async def local_prefix(work_dir: Path, env: Mapping[str, str]) -> tuple[str, ...] | None:
    """Return a verified launcher prefix, or None; never execute a check as fallback."""
    if sys.platform == "darwin":
        executable = _which("sandbox-exec", env)
        if executable is None:
            return None
        prefix = (executable, "-p", MACOS_PROFILE)
        return prefix if await _verified_prefix(prefix, "", os.getuid(), work_dir, env) else None
    if sys.platform != "linux":
        return None
    try:
        namespace = os.readlink("/proc/self/ns/net")
    except OSError:
        return None
    uid = os.getuid()
    for prefix in _linux_prefixes(env):
        if await _verified_prefix(prefix, namespace, uid, work_dir, env):
            return prefix
    return None


def _host_uid(euid: object, uid_map: object) -> int | None:
    """The caller's uid outside this user namespace, or None when the map does not say."""
    if isinstance(euid, bool) or not isinstance(euid, int) or euid < 0 or not isinstance(uid_map, str):
        return None
    for line in uid_map.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            inside, outside, count = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if count > 0 and inside <= euid < inside + count:
            return outside + (euid - inside)
    return None


def _linux_enforced(result: Mapping[str, object], namespace: str, uid: int) -> bool:
    """A new netns owned by ``uid`` that cannot connect, with only lo and down unlinked tunnels."""
    interfaces = result.get("interfaces")
    links = result.get("links")
    if (_host_uid(result.get("euid"), result.get("uid_map")) != uid
            or not isinstance(result.get("namespace"), str) or result["namespace"] == namespace
            or result.get("connect_errno") not in (errno.EPERM, errno.ENETUNREACH)
            or not isinstance(interfaces, list) or "lo" not in interfaces or not isinstance(links, dict)
            or set(interfaces) != set(links)):
        return False
    for name in interfaces:
        info = links.get(name)
        if not isinstance(name, str) or not isinstance(info, dict):
            return False
        if name == "lo":
            continue
        if info.get("up") is not False or info.get("iflink") != 0 or info.get("addressed") is not False:
            return False
    return True
