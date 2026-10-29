"""Packaged DSH runner launch and handshake, without runtime registration."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from .. import RuntimeUnsupported
from .bridge import RunnerBridge

PERMITTED = ("turn", "compaction")


def entry() -> Path:
    path = Path(str(files(__package__).joinpath("dsh_runner/src/main.ts")))
    if not path.is_absolute() or not path.is_file():
        raise RuntimeUnsupported("the DSH runner is missing from this installation; reinstall Looper")
    return path


def state_root(runtime_root: Path) -> Path:
    root = Path(runtime_root).absolute()
    # Create ancestors individually: mkdir(parents=True) gives intermediate
    # directories default permissions. Existing directory symlinks are allowed
    # through runtime_root (e.g. the OS temp directory), never in our suffix.
    path = Path(root.anchor)
    for index, part in enumerate((*root.parts[1:], "runners", "state", "dsh-native")):
        path /= part
        try:
            path.mkdir(mode=0o700, exist_ok=True)
        except FileExistsError:
            raise ValueError("DSH state root must be a real directory") from None
        if not path.is_dir() or (index >= len(root.parts) - 1 and path.is_symlink()):
            raise ValueError("DSH state root must be a real directory")
    return path


def command(node: str, dsh: str, state: Path) -> list[str | Path]:
    if not Path(node).is_absolute():
        raise RuntimeUnsupported("the DSH runner requires an absolute Node executable")
    return [node, entry(), "--dsh", str(Path(dsh).resolve()), "--state-root", str(state)]


async def connect(process, kind: str) -> RunnerBridge:
    bridge = RunnerBridge(process.stdout, process.stdin, kind, control_timeout=30)
    try:
        await bridge.initialize(permitted=PERMITTED)
    except BaseException:
        await bridge.close()
        raise
    bridge.control_timeout = 10
    return bridge
