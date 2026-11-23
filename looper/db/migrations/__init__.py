"""Named schema migrations, discovered without a shared registration list."""

from __future__ import annotations

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
