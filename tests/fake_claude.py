"""Stand-in ``claude`` executables that start fast.

macOS checks every newly written executable the first time it runs, which
costs 0.3 s on an idle machine and more than 1 s on a loaded one. Each
stand-in made here is a fresh symlink to one driver script that already ran
once in this process; the driver runs the stand-in's body from ``<link>.py``.
"""

from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_DRIVER: list[Path] = []


def _driver() -> Path:
    if not _DRIVER:
        home = Path(tempfile.mkdtemp(prefix="loopx-fake-claude-"))
        atexit.register(shutil.rmtree, home, True)
        driver = home / "driver"
        driver.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "body = sys.argv[0] + '.py'\n"
            "exec(compile(open(body, encoding='utf-8').read(), body, 'exec'))\n",
            encoding="utf-8",
        )
        driver.chmod(0o755)
        (home / "driver.py").write_text("", encoding="utf-8")
        subprocess.run([str(driver)], check=True)  # the one run that pays the first-exec check
        _DRIVER.append(driver)
    return _DRIVER[0]


def fake_claude(path: Path, body: str) -> str:
    """Make ``path`` an executable that runs the Python source ``body``; return the path."""
    Path(f"{path}.py").write_text(body, encoding="utf-8")
    path.unlink(missing_ok=True)
    path.symlink_to(_driver())
    return str(path)
