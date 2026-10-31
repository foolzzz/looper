"""Evaluate only the target's seat-tool declarations, without importing its services.

The input is source text, not an installed package. Keeping schema extraction
independent of operational imports also allows inspecting a target whose new
runtime dependencies have not been installed yet. Declaration layout changes
fail closed rather than borrowing any schema from the running version.
"""
from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


def declarations(sources, path, names, namespace):
    nodes = []
    found = set()
    for node in ast.parse(sources[path]).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined = {node.name}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
        else:
            continue
        if defined & names:
            nodes.append(node)
            found.update(defined & names)
    if found != names:
        raise ValueError(f"unsupported target seat-tool declarations in {path}: missing {sorted(names - found)}")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), namespace)


def main():
    sources = json.load(sys.stdin)
    namespace = {"__name__": __name__, "Enum": Enum, "dataclass": dataclass, "Mapping": Mapping, "Any": Any}
    declarations(sources, "looper/agents/ops/__init__.py", {"Risk", "Tool"}, namespace)
    declarations(sources, "looper/agents/room.py", {"MemberKind", "MessageKind", "POSTABLE"}, namespace)
    declarations(sources, "looper/agents/ops/room.py",
                 {"ROSTER", "POST", "INBOX", "READ", "WATCH", "_NAMES", "_never", "TOOLS", "tools_for"}, namespace)
    kind = namespace["MemberKind"]
    room = {role: namespace["tools_for"](member) for role, member in
            (("worker", kind.WORK), ("reviewer", kind.REVIEW))}
    declarations(sources, "looper/agents/ops/vault.py", {"SEARCH", "READ", "WRITE", "_never", "TOOLS"}, namespace)
    print(json.dumps({role: [tool.listing() for tool in (*tools, *namespace["TOOLS"])]
                      for role, tools in room.items()}, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
