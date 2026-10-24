"""Domain discovery and OpenAPI registration invariants."""

import importlib
import pkgutil
import sys
from collections import Counter

import pytest

from looper.agents import api_v1, v1_domains


def test_discovery_finds_exactly_the_public_domain_modules():
    names = {info.name for info in pkgutil.iter_modules(v1_domains.__path__) if not info.name.startswith("_")}
    domains = api_v1.domains()
    assert len(domains) == len(names)
    assert set(domains) == {importlib.import_module(f"{v1_domains.__name__}.{name}") for name in names}


def test_task_workspace_operations_keep_their_order_in_openapi():
    expected = ["tasks.create", "tasks.delete", "workspaces.check", "workspaces.discard"]
    assert [op["operationId"] for item in api_v1.document()["paths"].values()
            for op in item.values() if op["operationId"] in expected] == expected


def test_discovered_operations_appear_in_openapi_exactly_once():
    operations = api_v1.operations()
    assert len({op.operation_id for op in operations}) == len(operations)
    assert len({(op.method, op.path) for op in operations}) == len(operations)

    paths = api_v1.document()["paths"]
    operation_ids = Counter(op["operationId"] for item in paths.values() for op in item.values())
    assert all(count == 1 for count in operation_ids.values())
    assert {
        "schedules.create", "schedules.update", "schedules.enable", "schedules.disable",
        "schedules.run", "schedules.delete", "schedules.reset_planner_session",
        "schedule_admin.create", "schedule_admin.update", "schedule_admin.delete",
        "schedule_admin.reset_planner_session", "schedule_admin.run",
    } <= operation_ids.keys()
    for domain in api_v1.domains():
        for operation in domain.OPERATIONS:
            assert operation_ids[operation.operation_id] == 1
            path = operation.path.removeprefix(api_v1.PREFIX)
            assert paths[path][operation.method.lower()]["operationId"] == operation.operation_id


@pytest.fixture
def package_path(monkeypatch, tmp_path):
    monkeypatch.setattr(v1_domains, "__path__", [str(tmp_path)])
    yield tmp_path
    for path in tmp_path.glob("*.py"):
        sys.modules.pop(f"{v1_domains.__name__}.{path.stem}", None)
        vars(v1_domains).pop(path.stem, None)


def _domain(path, name, *, operation_id=None, route=None, order=None, stream=False):
    operation_id = operation_id or name
    route = route or f"/api/v1/{name}"
    source = f"""from ..api_v1 import Operation
OPERATIONS = (Operation({operation_id!r}, "POST", {route!r}, "Test", "Test.", {{}}, {{}},
                        stream={stream!r}, risk="read"),)
RESULTS = {{}}
async def run(*args):
    return {{}}
async def stream(*args):
    return None
"""
    if order is not None:
        source += f"ORDER = {order!r}\n"
    target = path / f"{name}.py"
    target.write_text(source)
    return target


def test_new_modules_register_in_deterministic_order_and_private_modules_are_skipped(package_path):
    _domain(package_path, "discovery_z", order=10)
    _domain(package_path, "discovery_b", order=20)
    _domain(package_path, "discovery_a", order=20)
    _domain(package_path, "discovery_default")
    (package_path / "_discovery_private.py").write_text("raise AssertionError('must not import')")
    domains = v1_domains._discover()
    assert [domain.OPERATIONS[0].operation_id for domain in domains] == [
        "discovery_z", "discovery_a", "discovery_b", "discovery_default"]
    assert all(callable(domain.run) and callable(domain.stream) for domain in domains)


@pytest.mark.parametrize("export", ["OPERATIONS", "RESULTS", "run", "stream"])
def test_missing_export_fails_when_domains_load(package_path, export):
    path = _domain(package_path, "discovery_missing", stream=export == "stream")
    path.write_text(path.read_text() + f"del {export}\n")
    with pytest.raises(ValueError, match=rf"discovery_missing.*missing required export: {export}"):
        v1_domains._discover()


@pytest.mark.parametrize("stream", [False, True])
def test_only_callbacks_used_by_operations_are_required(package_path, stream):
    path = _domain(package_path, "discovery_callback", stream=stream)
    unused = "run" if stream else "stream"
    path.write_text(path.read_text() + f"del {unused}\n")
    (domain,) = v1_domains._discover()
    assert callable(getattr(domain, "stream" if stream else "run"))


def test_invalid_order_has_a_clear_error(package_path):
    _domain(package_path, "discovery_order", order="first")
    with pytest.raises(ValueError, match="discovery_order.*ORDER must be an integer"):
        v1_domains._discover()


@pytest.mark.parametrize("duplicate", ["operationId", "path"])
def test_duplicate_declarations_fail_when_domains_load(package_path, duplicate):
    _domain(package_path, "discovery_first")
    _domain(package_path, "discovery_second",
            operation_id="discovery_first" if duplicate == "operationId" else None,
            route="/api/v1/discovery_first" if duplicate == "path" else None)
    with pytest.raises(ValueError, match=rf"duplicate {duplicate}.*discovery_first.*discovery_second"):
        v1_domains._discover()
