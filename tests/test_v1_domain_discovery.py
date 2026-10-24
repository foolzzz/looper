"""Domain discovery and the operation order preserved from main."""

import importlib
import pkgutil
import sys

import pytest

from looper.agents import api_v1, v1_domains


_MAIN_OPERATION_IDS = """
overview tasks.list tasks.get tasks.room task_create task_control.start task_control.stop task_control.restart
task_control.start_over task_control.cancel task_instruct task_update concurrency projects.list projects.get
projects.create projects.update projects.delete agent_profiles.list agent_profiles.create
agent_profiles.update agent_profiles.delete providers.list providers.register providers.update models.list
models.create models.update models.delete catalog.list catalog.install catalog.update catalog.uninstall
config_export costs settings.view settings.set settings.allowed_profiles settings.scheduler settings.waits
settings.knowledge_vault schedules.list schedules.get schedules.create schedules.update schedules.enable
schedules.disable schedules.run schedules.delete schedules.reset_planner_session task_pipeline gates.list
gates.messages gates.reply gates.end chat.send chat.messages chat.conversations confirm openapi whoami
auth.set_password auth.clear_password system.check config.export config.import_preview config.import_cancel
config.import_apply system.status system.overview tasks.requirement_update tasks.configure tasks.move
tasks.agents tasks.continue catalog_items.list catalog_items.get catalog_items.update catalog_items.delete
installs.create installs.update installs.delete installs.project catalog_sources.import_machine
catalog_sources.import_mcp catalog_sources.upload catalog_sources.git_preview catalog_sources.git_import
catalog_sources.create_skill catalog_sources.create_mcp project_skills.add project_skills.get
project_skills.update project_skills.delete waits.resume waits.cancel tasks.role_update tasks.room_view
tasks.activity tasks.activity_detail tasks.conversation tasks.activity_stream
channels.status channels.people gate_settings.get gate_settings.update managers.list managers.create
managers.update managers.delete manager_templates.put manager_templates.delete
schedule_admin.list schedule_admin.project_list schedule_admin.get schedule_admin.preview
schedule_admin.runs schedule_admin.notifications
clients.list clients.create clients.revoke
project_roles.stats role_templates.list role_templates.put role_templates.delete
runs.for_task runs.list runs.get runs.events runs.usage
""".split()


def test_discovery_finds_every_current_domain():
    names = {info.name for info in pkgutil.iter_modules(v1_domains.__path__) if not info.name.startswith("_")}
    assert set(v1_domains.MODULES) == names
    domains = api_v1.domains()
    assert tuple(domain.__name__.rsplit(".", 1)[-1] for domain in domains) == v1_domains.MODULES
    assert all(domain is importlib.import_module(f"{v1_domains.__name__}.{name}")
               for name, domain in zip(v1_domains.MODULES, domains))


def test_openapi_operations_keep_main_order():
    assert [op["operationId"] for item in api_v1.document()["paths"].values()
            for op in item.values()] == [*_MAIN_OPERATION_IDS, "tasks.create"]


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
