"""Can a task Manager's direct session write anywhere but its task directory and its private temporary directory?

The caller (a probe test, or an eval's stage 0) starts the real CLI as a direct session whose environment carries the
launch's ``CLAUDE_CODE_TMPDIR``, has a scripted model run each ``rows`` command as one Bash call, and passes the tool
results to ``judge``. The probe writes only into directories it makes itself, under random names, and ``close`` removes
exactly those. The verdict rests on the marks and on what is on disk afterwards.
"""
from __future__ import annotations

import os
import secrets
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from looper.agents import task_manager_readback, task_manager_sandbox

ALLOWED, DENIED = "LOOPER_WRITE_ALLOWED", "LOOPER_WRITE_DENIED"
_ORIGINAL = "echo original\n"


@dataclass(frozen=True)
class Row:
    name: str
    command: str
    allowed: bool  # what the boundary must answer


def _mark(test: str) -> str:
    return f"if {test} 2>/dev/null; then echo {ALLOWED}; else echo {DENIED}; fi"


class WriteProbe:
    def __init__(self, *, temp_root: str, environment: Mapping[str, str]) -> None:
        """``temp_root`` is the launch's ``PrivateTmpdir.write_root``; ``environment`` is the CLI's."""
        token = f"looper-write-probe-{secrets.token_hex(6)}"
        shared = Path(task_manager_readback.shared_temp_root(os.getuid()))
        self.made: list[Path] = []
        self.unverified: list[str] = []
        self.refused: list[Path] = []  # files that must not exist afterwards
        self.script: Path | None = None
        rows = [
            Row("temp_directory_is_private",
                _mark(f'[ "$(cd "$TMPDIR" && pwd -P)" = {shlex.quote(temp_root.rstrip("/"))} ]'), True),
            Row("own_temp_directory", _mark(f'touch "$TMPDIR/{token}"'), True),
            Row("task_directory", _mark(f"touch ./{token} && rm ./{token}"), True),
        ]
        targets = [("shared_temp_root", shared / token)]
        home = environment.get("HOME") or str(Path.home())
        for index, root in enumerate(task_manager_sandbox.shared_write_roots(environment)):
            # A home directory is never changed to make a target; the fixed temporary roots may be made.
            if root.startswith(f"{home}/") and not os.path.isdir(root):
                self.unverified.append(f"{os.path.relpath(root, home)}: the directory does not exist")
                continue
            targets.append((f"fixed_root_{index}", Path(root) / token))
        for name, directory in targets:
            if os.path.realpath(directory) in {os.path.realpath(made) for made in self.made}:
                continue
            directory.mkdir(parents=True)
            self.made.append(directory)
            self.refused.append(directory / "written")
            rows.append(Row(name, _mark(f"touch {shlex.quote(str(directory / 'written'))}"), False))
        # A script someone runs outside any sandbox, placed in the shared root before the session starts.
        self.script = self.made[0] / "placed-script.sh"
        self.script.write_text(_ORIGINAL)
        rows.append(Row("placed_script", _mark(f"echo 'echo changed' >> {shlex.quote(str(self.script))}"), False))
        sibling = shared / f"{token}-sibling"
        self.refused.append(sibling)
        self.made.append(sibling)  # removed if the session managed to make it
        rows.append(Row("new_sibling_directory", _mark(f"mkdir {shlex.quote(str(sibling))}"), False))
        self.rows: tuple[Row, ...] = tuple(rows)

    def judge(self, results: Mapping[str, str]) -> dict[str, Any]:
        """``results`` maps a row's name to its tool result. Only ``PASS`` lets a session be trusted."""
        failed = [row.name for row in self.rows
                  if (ALLOWED if row.allowed else DENIED) not in results.get(row.name, "")]
        failed += [f"written: {path}" for path in self.refused if path.exists()]
        if self.script is not None and self.script.read_text() != _ORIGINAL:
            failed.append("placed_script changed")
        return {"status": "FAIL" if failed else "PASS", "failed": failed, "unverified": list(self.unverified)}

    def close(self) -> None:
        made, self.made = self.made, []
        for directory in made:
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)
