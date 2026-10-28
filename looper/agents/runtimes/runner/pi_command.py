"""Packaged Pi runner launch and handshake, without runtime registration."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .. import RuntimeUnsupported
from .bridge import RunnerBridge

PERMITTED = ("turn", "retry", "compaction")
ENV = {"PI_OFFLINE": "1", "PI_TELEMETRY": "0", "PI_SKIP_VERSION_CHECK": "1"}


def entry() -> Path:
    path = Path(str(files(__package__).joinpath("pi_runner/src/main.ts")))
    if not path.is_absolute() or not path.is_file():
        raise RuntimeUnsupported("the Pi runner is missing from this installation; reinstall Looper")
    return path


def command(node: str, pi: str) -> list[str | Path]:
    if not Path(node).is_absolute():
        raise RuntimeUnsupported("the Pi runner requires an absolute Node executable")
    return [node, entry(), "--pi", str(Path(pi).resolve())]


async def connect(process, kind: str) -> RunnerBridge:
    bridge = RunnerBridge(process.stdout, process.stdin, kind, control_timeout=10)
    try:
        await bridge.initialize(permitted=PERMITTED)
    except BaseException:
        await bridge.close()
        raise
    return bridge
