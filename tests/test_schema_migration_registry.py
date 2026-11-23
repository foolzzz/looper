"""Migration discovery preserves the released order without a shared append-only list."""
from dataclasses import asdict
import json
from pathlib import Path
import sys

import pytest

from looper.db import migrations


_BASELINE = json.loads((Path(__file__).parent / "fixtures" / "schema_migration_order.json").read_text())


def test_discovered_migrations_preserve_the_original_execution_and_probe_order():
    discovered = migrations.discover()
    names = [migration.name for migration in discovered]
    # This is a frozen baseline, not the registry: later migrations need no edits here.
    existing = set(_BASELINE["apply"])
    assert [name for name in names if name in existing] == _BASELINE["apply"]
    assert [migration.name for migration in migrations.pending_migrations(discovered)
            if migration.name in existing] == _BASELINE["pending"]
    assert len(names) == len(set(names))


def test_discovered_online_steps_preserve_every_original_byte_and_order():
    steps = [asdict(step) for step in migrations.online_steps(migrations.discover())]
    # Optional unique indexes do not change the released non-unique steps.
    for step in steps:
        if not step["unique_indexes"]:
            step.pop("unique_indexes")
    existing = {step["marker"] for step in _BASELINE["online"]}
    # JSON normalizes tuples to lists while retaining the SQL strings exactly.
    assert json.loads(json.dumps([step for step in steps if step["marker"] in existing])) == _BASELINE["online"]


def _write_migration(directory, module_name, name, order, pending_order):
    (directory / f"{module_name}.py").write_text(
        f"name = {name!r}\norder = {order}\npending_order = {pending_order}\n"
        "async def pending(conn):\n    return False\n"
        "async def apply(conn, *, sql, state):\n    pass\n"
    )


def test_new_files_are_discovered_and_equal_orders_are_deterministic(tmp_path, monkeypatch):
    # Filename order disagrees with declared order and names: discovery must use metadata.
    _write_migration(tmp_path, "registry_test_z", "first", 1, 3)
    _write_migration(tmp_path, "registry_test_a", "third", 2, 1)
    _write_migration(tmp_path, "registry_test_m", "second", 2, 2)
    _write_migration(tmp_path, "registry_test_no_probe", "no_probe", 3, None)
    (tmp_path / "_registry_test_ignored.py").write_text("raise AssertionError('private helper imported')\n")
    monkeypatch.setattr(migrations, "__path__", [str(tmp_path)])
    try:
        discovered = migrations.discover()
        assert [migration.name for migration in discovered] == ["first", "second", "third", "no_probe"]
        assert [migration.name for migration in migrations.pending_migrations(discovered)] == [
            "third", "second", "first"]
        assert migrations.online_steps(discovered) == ()
    finally:
        for name in ("registry_test_z", "registry_test_a", "registry_test_m", "registry_test_no_probe"):
            sys.modules.pop(f"{migrations.__name__}.{name}", None)


def test_duplicate_migration_names_are_rejected(tmp_path, monkeypatch):
    _write_migration(tmp_path, "registry_test_duplicate_a", "duplicate", 1, 1)
    _write_migration(tmp_path, "registry_test_duplicate_b", "duplicate", 2, 2)
    monkeypatch.setattr(migrations, "__path__", [str(tmp_path)])
    try:
        with pytest.raises(ValueError, match="(?i)duplicate"):
            migrations.discover()
    finally:
        for name in ("registry_test_duplicate_a", "registry_test_duplicate_b"):
            sys.modules.pop(f"{migrations.__name__}.{name}", None)


def test_new_fresh_ddl_is_appended_without_changing_the_baseline_bytes():
    from types import SimpleNamespace

    base = "CREATE TABLE original (id integer);\n"
    assert migrations.schema_sql(base, [SimpleNamespace(name="original")]) == base
    assert migrations.schema_sql(base, [SimpleNamespace(name="original"),
                                       SimpleNamespace(name="added", fresh_ddl="CREATE TABLE added (id integer);")]) == (
        base + "\nCREATE TABLE added (id integer);"
    )


def test_current_baseline_additions_have_upgrade_and_admission_migrations():
    discovered = migrations.discover()
    expected = {"mcp_tool_levels", "failure_typing", "memory_candidates", "channel_outbox",
                "memory_draft_role", "memory_draft_queue"}
    assert expected <= {migration.name for migration in discovered}
    assert expected <= {migration.name for migration in migrations.pending_migrations(discovered)}
    steps = migrations.online_steps(discovered)
    failure = next(step for step in steps if step.migration == "failure_typing")
    assert failure.marker == "failure_typing_online"
    assert failure.validate == (("llm_records", "llm_records_failure_check"),)
    assert failure.indexes == (("idx_llm_records_failures",
                                "ON llm_records (created_at) WHERE failure_class IS NOT NULL"),)
