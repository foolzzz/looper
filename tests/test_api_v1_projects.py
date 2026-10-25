"""Project JSON projections and generated standard-library CLI requests."""

from types import SimpleNamespace

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.db.projects import EXECUTION_COLUMNS


@pytest.mark.parametrize("action,options,body", [
    ("list", [], {}),
    ("create", ["--name", "Example", "--task-budget-usd", '"2.5"'],
     {"name": "Example", "task_budget_usd": "2.5"}),
    ("update", ["--project-id", "proj-example", "--fields", '{"background":"Changed"}'],
     {"project_id": "proj-example", "fields": {"background": "Changed"}}),
    ("delete", ["--project-id", "proj-example"], {"project_id": "proj-example"}),
])
def test_generated_cli_posts_project_admin_requests(action, options, body, capsys):
    args = loopercli.build_parser(api_v1.document()).parse_args(["project-admin", action, *options])
    calls = []
    api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {"status": "ok", "result": {}})))
    assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session="projects")) == 0
    assert calls == [("POST", f"/api/v1/project_admin/{action}", body)]


def test_project_schemas_cover_canonical_execution_columns_and_ordinary_operations():
    schemas = api_v1.document()["components"]["schemas"]
    assert set(schemas["ProjectExecution"]["properties"]) == set(EXECUTION_COLUMNS)
    assert set(schemas["Project"]["properties"]) == set(EXECUTION_COLUMNS) | {
        "project_id", "name", "description", "work_dir", "background", "created_at", "updated_at",
        "task_count", "active_count", "schedule_count"}
    for action in ("list", "create", "update", "delete"):
        operation = api_v1.find("POST", f"/api/v1/project_admin/{action}")
        assert operation is not None and operation.tool is None and not operation.hand
        assert operation.request["additionalProperties"] is False


@pytest.mark.parametrize("action,body", [
    ("list", {"extra": True}), ("create", {"name": None}),
    ("update", {"project_id": 1, "fields": {"name": "Example"}}),
    ("delete", {"project_id": None}),
])
def test_project_requests_reject_unknown_fields_and_wrong_identifier_types(action, body):
    from looper.agents.ops import OpError

    operation = api_v1.find("POST", f"/api/v1/project_admin/{action}")
    with pytest.raises(OpError):
        api_v1.validate_arguments(operation.request, body)
