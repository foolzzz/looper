"""Product imports must be available to a wheel installed without test extras."""

from __future__ import annotations

import ast
from importlib import metadata
from pathlib import Path
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
# SDK-specific HTTP transports load only when these API codecs are used.
# Keep optional lazy exceptions scoped to their modules; eager imports still fail.
OPTIONAL_LAZY_IMPORTS = {
    ("looper/agents/runtimes/api/messages.py", "httpx2"),
    ("looper/agents/runtimes/api/responses.py", "httpx2"),
}


def runtime_distributions(requirements: list[str]) -> set[str]:
    pending = [(Requirement(value), frozenset()) for value in requirements]
    visited = set()
    distributions = set()
    while pending:
        requirement, parent_extras = pending.pop()
        if requirement.marker and not any(
            requirement.marker.evaluate({"extra": extra}) for extra in {"", *parent_extras}
        ):
            continue
        name = canonicalize_name(requirement.name)
        key = (name, frozenset(requirement.extras))
        if key in visited:
            continue
        visited.add(key)
        distributions.add(name)
        # Follow only requirements enabled by runtime dependencies, never the
        # extras installed for pytest itself. Missing metadata must fail too.
        pending.extend((Requirement(value), key[1]) for value in metadata.requires(name) or ())
    return distributions


class Imports(ast.NodeVisitor):
    def __init__(self, tree: ast.Module):
        self.imports: list[tuple[str, int, bool]] = []
        self.function_depth = 0
        self.type_guards = {"TYPE_CHECKING"}
        self.typing_names = {"typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                self.type_guards.update(a.asname or a.name for a in node.names if a.name == "TYPE_CHECKING")
            elif isinstance(node, ast.Import):
                self.typing_names.update(a.asname or a.name for a in node.names if a.name == "typing")

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if ((isinstance(test, ast.Name) and test.id in self.type_guards)
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
                and isinstance(test.value, ast.Name) and test.value.id in self.typing_names)):
            for statement in node.orelse:
                self.visit(statement)
        else:
            self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend((a.name.split(".")[0], node.lineno, bool(self.function_depth)) for a in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not node.level and node.module:
            self.imports.append((node.module.split(".")[0], node.lineno, bool(self.function_depth)))


def test_product_imports_are_runtime_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    distributions = runtime_distributions(project["dependencies"])
    providers = metadata.packages_distributions()
    missing = []
    for path in sorted((ROOT / "looper").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = Imports(tree)
        imports.visit(tree)
        relative = path.relative_to(ROOT).as_posix()
        for package, line, lazy in imports.imports:
            if package in sys.stdlib_module_names or package == "looper":
                continue
            if lazy and (relative, package) in OPTIONAL_LAZY_IMPORTS:
                continue
            if not any(canonicalize_name(name) in distributions for name in providers.get(package, ())):
                missing.append(f"{relative}:{line}: {package}")
    assert not missing, "Imports missing from runtime dependencies:\n" + "\n".join(missing)
