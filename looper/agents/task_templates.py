"""Task templates of a Manager: their typed inputs, the check of a task's values against them, and the requirement
rendered from both. Patterns are user text run by ``re``, which backtracks, so they only ever run in a throwaway
subprocess, never on the shared event loop."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from looper.agents.projects import TASK_PROFILE_ROLES
from looper.agents.system_prompt import TASK_REQUIREMENT_LIMIT
from looper.agents.task_roles import RoleError, role_key

PATTERN_TIMEOUT_SEC = 2.0
PATTERN_PROBE = "a" * 64 + "!"
KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
INPUT_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
MAX_INPUTS = 10
LABEL_MAX = 60
HINT_MAX = 200
PATTERN_MAX = 300
VALUE_MAX = 2000
INSTRUCTIONS_MAX = 8000
NAME_MAX = 60
DESCRIPTION_MAX = 300

PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_INPUT_FIELDS = ("key", "label", "kind", "required", "pattern", "hint")

# Reads [[pattern, value], ...] from stdin; answers one JSON line per pair: true, false, or the compile error.
_MATCHER = (
    "import json, re, sys\n"
    "for pattern, value in json.load(sys.stdin):\n"
    "    try:\n"
    "        out = re.fullmatch(pattern, value) is not None\n"
    "    except re.error as exc:\n"
    "        out = str(exc)\n"
    "    sys.stdout.write(json.dumps(out) + '\\n')\n"
    "    sys.stdout.flush()\n"
)


class InputKind(str, Enum):
    TEXT = "text"
    URL = "url"


class TemplateError(ValueError):
    """A template or a task's inputs Looper refuses; the message says what to change."""


class PatternTimeout(TemplateError):
    """A pattern did not finish in time; ``index`` is the first check without an answer."""

    def __init__(self, index: int):
        super().__init__(f"the pattern of check {index} took too long")
        self.index = index


@dataclass(frozen=True)
class TemplateInput:
    key: str
    label: str
    kind: InputKind = InputKind.TEXT
    required: bool = True
    pattern: str = ""
    hint: str = ""


@dataclass(frozen=True)
class TaskTemplate:
    manager_id: str
    key: str
    name: str
    description: str = ""
    project_id: str | None = None
    work_dir: str = ""
    workspace_mode: str | None = None
    requirement: str = ""
    planner_instructions: str = ""
    profiles: Mapping[str, str] = field(default_factory=dict)
    role_templates: tuple[str, ...] = ()
    inputs: tuple[TemplateInput, ...] = ()
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskTemplate":
        raw_inputs = row.get("inputs") or []
        if isinstance(raw_inputs, (str, bytes)):
            raw_inputs = json.loads(raw_inputs)
        inputs = tuple(
            TemplateInput(
                key=item["key"], label=item["label"],
                kind=InputKind(item.get("kind", "text")),
                required=bool(item.get("required", True)),
                pattern=item.get("pattern") or "", hint=item.get("hint") or "",
            ) for item in raw_inputs)
        profiles = {r: row[f"{r}_profile_id"] for r in TASK_PROFILE_ROLES if row.get(f"{r}_profile_id")}
        return cls(
            manager_id=row["manager_id"], key=row["key"], name=row["name"],
            description=row.get("description") or "", project_id=row.get("project_id"),
            work_dir=row.get("work_dir") or "", workspace_mode=row.get("workspace_mode"),
            requirement=row.get("requirement") or "",
            planner_instructions=row.get("planner_instructions") or "",
            profiles=profiles, role_templates=tuple(row.get("role_templates") or ()),
            inputs=inputs, updated_at=row.get("updated_at"))

    def snapshot(self, values: Mapping[str, str], roles: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "inputs": [{"key": i.key, "label": i.label, "value": values[i.key]}
                       for i in self.inputs if i.key in values],
            "planner_instructions": self.planner_instructions,
            "roles": [dict(r) for r in roles],
        }


def parse_inputs(raw: Any) -> tuple[TemplateInput, ...]:
    if not isinstance(raw, list):
        raise TemplateError("inputs must be a list of {key, label, kind, required, pattern, hint}")
    if len(raw) > MAX_INPUTS:
        raise TemplateError(f"inputs has {len(raw)} items; a template takes at most {MAX_INPUTS}")
    out: list[TemplateInput] = []
    seen: set[str] = set()
    for n, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TemplateError(f"inputs[{n}] must be an object {{key, label, kind, required, pattern, hint}}")
        for name in item:
            if name not in _INPUT_FIELDS:
                raise TemplateError(f"inputs[{n}]: unknown field {name!r}; the fields are: {', '.join(_INPUT_FIELDS)}")
        key = item.get("key")
        if not isinstance(key, str) or not re.fullmatch(INPUT_KEY_PATTERN, key):
            raise TemplateError(f"inputs[{n}].key must match {INPUT_KEY_PATTERN}; got {str(key)[:40]!r}")
        if key in seen:
            raise TemplateError(f"inputs[{n}].key {key!r} is used twice: give each input its own key")
        seen.add(key)
        label = item.get("label")
        label = label.strip() if isinstance(label, str) else ""
        if not 1 <= len(label) <= LABEL_MAX:
            raise TemplateError(f"input {key}: label must be 1 to {LABEL_MAX} characters")
        try:
            kind = InputKind(item.get("kind", "text"))
        except ValueError:
            raise TemplateError(f"input {key}: kind must be one of: {', '.join(k.value for k in InputKind)}") from None
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise TemplateError(f"input {key}: required must be true or false")
        pattern = item.get("pattern", "")
        if not isinstance(pattern, str) or len(pattern) > PATTERN_MAX:
            raise TemplateError(f"input {key}: pattern must be a string of at most {PATTERN_MAX} characters")
        hint = item.get("hint", "")
        if not isinstance(hint, str) or len(hint) > HINT_MAX:
            raise TemplateError(f"input {key}: hint must be a string of at most {HINT_MAX} characters")
        out.append(TemplateInput(key, label, kind, required, pattern, hint))
    return tuple(out)


def placeholders(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def check_requirement(text: str, inputs: Sequence[TemplateInput]) -> None:
    keys = [i.key for i in inputs]
    for k in placeholders(text):
        if k not in keys:
            raise TemplateError(f"requirement: {{{{{k}}}}} names no input; the inputs are: "
                                f"{', '.join(keys) or 'none'}")
    rest = PLACEHOLDER.sub("", text)
    if "{{" in rest or "}}" in rest:
        raise TemplateError("requirement: '{{' without a matching '}}', or a placeholder that is not {{input_key}}")
    if len(text) > TASK_REQUIREMENT_LIMIT:
        raise TemplateError(f"requirement has {len(text)} characters; at most {TASK_REQUIREMENT_LIMIT}")


def check_template_fields(*, name: str, description: str = "", planner_instructions: str = "",
                          workspace_mode: str | None = None, role_templates: Sequence[str] = (),
                          project_id: str | None = None) -> tuple[str, str]:
    """Return the cleaned ``(name, description)``; raise ``TemplateError`` on the first problem."""
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= NAME_MAX or "\n" in name or "\r" in name:
        raise TemplateError(f"name must be one line of 1 to {NAME_MAX} characters")
    description = " ".join((description or "").split())
    if len(description) > DESCRIPTION_MAX:
        raise TemplateError(f"description must be at most {DESCRIPTION_MAX} characters")
    if len(planner_instructions or "") > INSTRUCTIONS_MAX:
        raise TemplateError(f"planner_instructions must be at most {INSTRUCTIONS_MAX} characters")
    if workspace_mode not in (None, "worktree", "in_place"):
        raise TemplateError("workspace_mode must be 'worktree', 'in_place' or left out")
    if role_templates and not project_id:
        raise TemplateError("role_templates need the template's project: set project_id")
    seen: set[str] = set()
    for key in role_templates:
        try:
            role_key(key)
        except RoleError as exc:
            raise TemplateError(f"role_templates: {exc}") from None
        if key in seen:
            raise TemplateError(f"role_templates lists {key} twice: list each role template once")
        seen.add(key)
    return name, description


async def match_patterns(checks: Sequence[tuple[str, str]], *,
                         timeout: float = PATTERN_TIMEOUT_SEC) -> list[bool | str]:
    """``re.fullmatch`` of each ``(pattern, value)`` in a throwaway subprocess: True, False, or the compile error."""
    if not checks:
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", _MATCHER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError:
        raise TemplateError("Looper could not check the input's pattern now; tell the user and try again later") from None
    payload = json.dumps([[p, v] for p, v in checks]).encode()
    answers: list[bool | str] = []

    async def run() -> None:
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
        while len(answers) < len(checks):
            line = await proc.stdout.readline()
            if not line:
                return
            answers.append(json.loads(line))

    try:
        try:
            await asyncio.wait_for(run(), timeout)
        except asyncio.TimeoutError:
            raise PatternTimeout(len(answers)) from None
        except OSError:
            raise TemplateError(
                "Looper could not check the input's pattern now; tell the user and try again later") from None
        await proc.wait()
    finally:
        # Also when this coroutine is cancelled: a backtracking child must not outlive the check.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if len(answers) < len(checks):
        raise TemplateError("Looper could not check the input's pattern now; tell the user and try again later")
    return answers


async def check_patterns(inputs: Sequence[TemplateInput]) -> None:
    """At save time: every pattern compiles and does not blow up on the probe."""
    for item in inputs:
        if not item.pattern:
            continue
        try:
            (answer,) = await match_patterns([(item.pattern, PATTERN_PROBE)])
        except PatternTimeout:
            raise TemplateError(f"input {item.key}: pattern is too slow (nested repetition such as (a+)+): "
                                "simplify it") from None
        if isinstance(answer, str):
            raise TemplateError(f"input {item.key}: pattern does not compile: {answer}")


async def check_values(template: TaskTemplate, raw: Any) -> dict[str, str]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
        raise TemplateError(f"the template {template.name} takes inputs as an object of strings")
    by_key = {i.key: i for i in template.inputs}
    for k in raw:
        if k not in by_key:
            raise TemplateError(f"the template {template.name} has no input {k!r}; its inputs are: "
                                f"{', '.join(by_key) or 'none'}")
    values: dict[str, str] = {}
    for item in template.inputs:
        value = raw.get(item.key, "").strip()
        if not value:
            if item.required:
                hint = f" ({item.hint})" if item.hint else ""
                raise TemplateError(f"the template {template.name} needs {item.label}{hint}: "
                                    "ask the user for it; never make one up")
            continue
        if len(value) > VALUE_MAX:
            raise TemplateError(f"{item.label} has {len(value)} characters; at most {VALUE_MAX}")
        if item.kind is InputKind.URL:
            try:
                parts = urlsplit(value)
                ok = parts.scheme in ("http", "https") and bool(parts.netloc)
            except ValueError:
                ok = False
            if not ok:
                raise TemplateError(f"{item.label} must be an http or https URL")
        values[item.key] = value
    checked = [i for i in template.inputs if i.pattern and i.key in values]
    if checked:
        try:
            answers = await match_patterns([(i.pattern, values[i.key]) for i in checked])
        except PatternTimeout as exc:
            item = checked[min(exc.index, len(checked) - 1)]
            raise TemplateError(f"the template {template.name}'s pattern for {item.label} took too long to "
                                "check: tell the user to simplify it on the dashboard") from None
        for item, answer in zip(checked, answers):
            if answer is not True:
                raise TemplateError(f"{item.label} must look like {item.hint or item.pattern}")
    return values


def render_requirement(template: TaskTemplate, values: Mapping[str, str], requirement: str) -> str:
    text = PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template.requirement).strip()
    requirement = requirement.strip()
    out = "\n\n".join(part for part in (text, requirement) if part)
    if len(out) > TASK_REQUIREMENT_LIMIT:
        raise TemplateError(f"the requirement with the template's text is longer than {TASK_REQUIREMENT_LIMIT} "
                            "characters: shorten it")
    return out
