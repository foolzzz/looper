"""The keeper stays isolated from the control plane and its import dependencies."""

import ast
import json
import subprocess
import sys
from pathlib import Path

from looper.agents import keeper


ROOT = Path(__file__).resolve().parents[1]
KEEPER = ROOT / "looper" / "agents" / "keeper"


def test_the_keeper_loads_only_allowed_looper_modules() -> None:
    result = subprocess.run(
        keeper.check_argv(sys.executable, ROOT),
        cwd=ROOT, capture_output=True, text=True, timeout=15, check=True,
    )
    modules = set(json.loads(result.stdout))
    assert {"looper.agents.keeper", "looper.agents.keeper.__main__",
            "looper.agents.keeper.process", "looper.agents.keeper.server",
            "looper.agents.keeper.protocol", "looper.agents.keeper.client"} <= modules
    allowed = {"looper", "looper.agents", "looper.agents.keeper",
               "looper.agents.proc_tree", "looper.child_env"}
    assert {name for name in modules if not name.startswith("looper.agents.keeper.")} <= allowed


def test_keeper_modules_import_only_at_module_level() -> None:
    sources = sorted(KEEPER.glob("*.py"))
    assert sources
    sources += [ROOT / "looper" / "agents" / "proc_tree.py", ROOT / "looper" / "child_env.py"]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                imports = [child.lineno for child in ast.walk(node)
                           if isinstance(child, (ast.Import, ast.ImportFrom))]
                assert not imports, f"{source.relative_to(ROOT)} imports inside a function at {imports}"


def test_the_control_plane_does_not_load_the_keeper_yet() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import json, sys; sys.path.insert(0, sys.argv[1]); "
         "import looper.agents.serve; import looper.agents.tiers.worker; "
         "print(json.dumps(sorted(m for m in sys.modules if m == 'looper' or m.startswith('looper.'))))",
         str(ROOT)],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=True,
    )
    modules = set(json.loads(result.stdout))
    assert {"looper.agents.serve", "looper.agents.tiers.worker"} <= modules
    assert not any(name == "looper.agents.keeper" or name.startswith("looper.agents.keeper.")
                   for name in modules)
