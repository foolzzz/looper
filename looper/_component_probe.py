"""Statically read target seat-tool declarations; never execute candidate code.

Only the data syntax and declaration shapes below are supported. A target that
changes the catalog's executable semantics needs a new reader, not a fallback
to the running version's schemas or an attempt to execute its declarations.
"""
from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass


# These are syntax contracts, not code to compile or execute. Checking the whole
# class also rejects executable decorators, defaults, bases and extra class code.
TOOL = '''
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: Mapping[str, Any]
    risk: Risk
    run: Callable[[Call], Awaitable[dict[str, Any]]]
    escalate: Callable[[Call], Awaitable[str | None]] | None = None
    task_id: Callable[[Mapping[str, Any]], str | None] = lambda arguments: None
    read_actions: frozenset[str] = frozenset()

    def risk_of(self, arguments: Mapping[str, Any]) -> Risk:
        return Risk.READ if arguments.get("action") in self.read_actions else self.risk

    def listing(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": dict(self.schema)}
'''
TOOLS_FOR = '''
def tools_for(kind: MemberKind) -> tuple[Tool, ...]:
    if kind is MemberKind.WORK:
        return TOOLS
    return tuple(tool for tool in TOOLS if tool.name != WATCH)
'''


def _body(nodes):
    return [node for node in nodes if not (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]


class _WithoutDocs(ast.NodeTransformer):
    def generic_visit(self, node):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = _body(node.body)
        return super().generic_visit(node)


def _shape(node):
    return ast.dump(_WithoutDocs().visit(node), include_attributes=False)


def _require_shape(node, template):
    if _shape(node) != _shape(ast.parse(template).body[0]):
        raise ValueError(f"unsupported target declaration: {node.name}")


@dataclass(frozen=True)
class _Member:
    enum: str
    value: str


@dataclass(frozen=True)
class _Enum:
    members: dict[str, _Member]


def _enum(node):
    if (node.decorator_list or node.keywords
            or [ast.dump(base) for base in node.bases] != [ast.dump(ast.Name(id=name, ctx=ast.Load()))
                                                         for name in ("str", "Enum")]):
        raise ValueError(f"unsupported target enum: {node.name}")
    members = {}
    for item in _body(node.body):
        if (not isinstance(item, ast.Assign) or len(item.targets) != 1
                or not isinstance(item.targets[0], ast.Name)
                or not isinstance(item.value, ast.Constant) or not isinstance(item.value.value, str)):
            raise ValueError(f"unsupported target enum member in {node.name}")
        name = item.targets[0].id
        if (name.startswith("_") or name == "mro" or name in members
                or item.value.value in {m.value for m in members.values()}):
            raise ValueError(f"unsupported target enum member in {node.name}")
        members[name] = _Member(node.name, item.value.value)
    return _Enum(members)


def _value(node, namespace):
    """Interpret a closed data grammar, with no Python call or attribute dispatch."""
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        return node.value
    if isinstance(node, ast.Name) and node.id in namespace:
        return namespace[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_value(item, namespace) for item in node.elts]
        return tuple(items) if isinstance(node, ast.Tuple) else items
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in zip(node.keys, node.values):
            if key is None:
                result.update(_value(value, namespace))
            else:
                result[_value(key, namespace)] = _value(value, namespace)
        return result
    if isinstance(node, ast.Attribute):
        value = _value(node.value, namespace)
        if isinstance(value, _Enum) and node.attr in value.members:
            return value.members[node.attr]
        if isinstance(value, _Member) and node.attr == "value":
            return value.value
    if isinstance(node, ast.Subscript):
        value, key = _value(node.value, namespace), _value(node.slice, namespace)
        if isinstance(value, dict) and isinstance(key, str):
            return value[key]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _value(node.left, namespace), _value(node.right, namespace)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    if isinstance(node, ast.ListComp) and len(node.generators) == 1:
        gen = node.generators[0]
        if isinstance(gen.target, ast.Name) and not gen.ifs and not gen.is_async:
            values = _value(gen.iter, namespace)
            if isinstance(values, _Enum):
                values = tuple(values.members.values())
            if isinstance(values, tuple):
                return [_value(node.elt, {**namespace, gen.target.id: item}) for item in values]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Tool":
        fields = {kw.arg: kw.value for kw in node.keywords}
        if (not node.args and len(fields) == len(node.keywords)
                and set(fields) == {"name", "description", "schema", "risk", "run"}
                and isinstance(fields["run"], ast.Name) and fields["run"].id == "_never"):
            risk = _value(fields["risk"], namespace)
            name, description, schema = (_value(fields[key], namespace) for key in ("name", "description", "schema"))
            if (isinstance(risk, _Member) and risk.enum == "Risk" and isinstance(name, str)
                    and isinstance(description, str) and isinstance(schema, dict)):
                return {"name": name, "description": description, "inputSchema": schema}
    raise ValueError(f"unsupported target seat-tool expression: {type(node).__name__}")


def _bind(target, value, namespace):
    if isinstance(target, ast.Name):
        namespace[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (tuple, list)) and len(target.elts) == len(value):
        for item, entry in zip(target.elts, value):
            _bind(item, entry, namespace)
    else:
        raise ValueError("unsupported target seat-tool assignment")


def declarations(sources, path, names, namespace):
    found = set()
    for node in ast.parse(sources[path]).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defined = {node.name}
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
        else:
            continue
        selected = defined & names
        if not selected:
            continue
        if found & selected:
            raise ValueError(f"duplicate target declarations in {path}")
        found.update(selected)
        if isinstance(node, ast.ClassDef):
            if node.name == "Tool":
                _require_shape(node, TOOL)
            else:
                namespace[node.name] = _enum(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "tools_for":
                _require_shape(node, TOOLS_FOR)
            else:
                area = "vault" if path.endswith("vault.py") else "room"
                _require_shape(node, f'async def _never(call: Any) -> dict[str, Any]:\n'
                                    f'    raise AssertionError("{area} tools run through run_tool")')
        else:
            value = _value(node.value, namespace)
            for target in targets:
                _bind(target, value, namespace)
    if found != names:
        raise ValueError(f"unsupported target seat-tool declarations in {path}: missing {sorted(names - found)}")


def main():
    sources = json.load(sys.stdin)
    namespace = {}
    declarations(sources, "looper/agents/ops/__init__.py", {"Risk", "Tool"}, namespace)
    declarations(sources, "looper/agents/room.py", {"MemberKind", "MessageKind", "POSTABLE"}, namespace)
    declarations(sources, "looper/agents/ops/room.py",
                 {"ROSTER", "POST", "INBOX", "READ", "WATCH", "_NAMES", "_never", "TOOLS", "tools_for"}, namespace)
    if not {"WORK", "REVIEW"} <= namespace["MemberKind"].members.keys():
        raise ValueError("target is missing Worker/Reviewer member kinds")
    # tools_for's exact syntax was checked above; implement that supported rule
    # over parsed data instead of ever calling the target's function or methods.
    room = {"worker": namespace["TOOLS"],
            "reviewer": tuple(tool for tool in namespace["TOOLS"] if tool["name"] != namespace["WATCH"])}
    declarations(sources, "looper/agents/ops/vault.py", {"SEARCH", "READ", "WRITE", "_never", "TOOLS"}, namespace)
    print(json.dumps({role: [*tools, *namespace["TOOLS"]] for role, tools in room.items()},
                     sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
