"""Named schema migrations, discovered without a shared registration list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import import_module
from pkgutil import iter_modules
from types import ModuleType
from collections.abc import Iterable


@dataclass(frozen=True)
class OnlineStep:
    """A migration's resumable validation and concurrent indexes, after commit.

    A step runs once its migration marker exists and until its own marker does.
    A stopped or outdated index build is dropped and rebuilt; a valid index with
    the expected definition is kept. The runner owns serialization and retries.
    """

    migration: str
    marker: str
    validate: tuple[tuple[str, str], ...] = ()  # (table, constraint)
    indexes: tuple[
        tuple[str, str], ...
    ] = ()  # (name, what follows the name in CREATE INDEX)
    unique_indexes: tuple[tuple[str, str], ...] = ()


def discover() -> tuple[ModuleType, ...]:
    """Use declared order, then name, regardless of directory enumeration order."""
    modules = []
    names = set()
    for entry in iter_modules(__path__):
        if entry.name.startswith("_"):
            continue
        module = import_module(f"{__name__}.{entry.name}")
        if module.name in names:
            raise ValueError(f"duplicate migration name: {module.name}")
        names.add(module.name)
        modules.append(module)
    return tuple(sorted(modules, key=lambda module: (module.order, module.name)))


def pending_migrations(modules: Iterable[ModuleType]) -> tuple[ModuleType, ...]:
    """Preserve the historical admission checks' independent short-circuit order."""
    return tuple(
        sorted(
            (module for module in modules if module.pending_order is not None),
            key=lambda module: (module.pending_order, module.name),
        )
    )


def online_steps(modules: Iterable[ModuleType]) -> tuple[OnlineStep, ...]:
    """Online work has its own order, independent of the transactional DDL."""
    ordered = sorted(
        (module for module in modules if getattr(module, "online", ())),
        key=lambda module: (module.online_order, module.name),
    )
    return tuple(step for module in ordered for step in module.online)


def schema_sql(base_sql: str, modules: Iterable[ModuleType]) -> str:
    """Keep the baseline byte-identical; new migrations own their fresh DDL."""
    return base_sql + "".join(
        "\n" + module.fresh_ddl
        for module in modules
        if getattr(module, "fresh_ddl", "")
    )


_NOT_VALID = re.compile(r"\s+NOT\s+VALID\s*$", re.IGNORECASE)


def _split_literals(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into SQL and single-quoted literals. A doubled quote stays inside the literal."""
    parts: list[tuple[str, str]] = []
    start = i = 0
    while i < len(text):
        if text[i] != "'":
            i += 1
            continue
        if start < i:
            parts.append(("sql", text[start:i]))
        end = i + 1
        while end < len(text):
            if text[end] == "'":
                if end + 1 < len(text) and text[end + 1] == "'":
                    end += 2
                    continue
                end += 1
                break
            end += 1
        parts.append(("lit", text[i:end]))
        start = i = end
    if start < len(text) or not parts:
        parts.append(("sql", text[start:]))
    return parts


def printed_check(definition: str) -> str:
    """``pg_get_constraintdef`` with keywords, whitespace and parentheses normalized.

    Validation is the online step, so a validated check and the ``NOT VALID`` check
    taken under the schema lock are the same predicate. ``::text`` casts are ignored
    for the same reason. String literal contents are kept: ``'unanswered'`` and
    ``'UNANSWERED'`` are different predicates. Callers compare this text, not a list
    of words inside it.
    """
    parts = _split_literals(definition or "")
    if parts[-1][0] == "sql":
        parts[-1] = ("sql", _NOT_VALID.sub("", parts[-1][1]))
    folded = []
    for kind, text in parts:
        if kind == "lit":
            folded.append(text)
        else:
            folded.append("".join(text.lower().split()).replace("::text", ""))
    return "".join(folded)
