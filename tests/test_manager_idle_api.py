"""Strict idle timeout validation at the service and existing v1 schemas."""

import asyncio

import pytest

from looper.agents import api_v1, managers_api
from looper.agents.ops import OpError


@pytest.mark.parametrize("value", [True, False, 60.0, "60", None, -1, 59, 86401])
def test_idle_timeout_service_rejects_noninteger_and_out_of_range_values(value):
    with pytest.raises(managers_api.RequestError):
        asyncio.run(managers_api.checked_values({"idle_timeout_seconds": value}, own_id="manager-example",
                                                check_references=False))


@pytest.mark.parametrize("value", [0, 60, 86400])
def test_existing_manager_operations_accept_optional_typed_timeout(value):
    for name, request in (
        ("managers.create", {"key": "example", "name": "Example", "system_prompt": "Help.", "idle_timeout_seconds": value}),
        ("managers.update", {"manager_id": "manager-example", "fields": {"idle_timeout_seconds": value}}),
    ):
        operation = next(op for op in api_v1.operations() if op.operation_id == name)
        assert operation.confirms and operation.risk == "high"
        assert api_v1.validate_arguments(operation.request, request) == request
        view = operation.result["properties"]["manager"]
        assert "idle_timeout_seconds" in view["required"]
        assert view["properties"]["idle_timeout_seconds"]["type"] == "integer"


@pytest.mark.parametrize("value", [True, 60.0, "60", 59, 86401])
def test_portable_manager_schema_rejects_invalid_idle_timeout(value):
    document = {"format": "looper-config/v4", "managers": [
        {"id": "manager-example", "name": "Example", "idle_timeout_seconds": value}]}
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.CONFIG_IMPORT_DOCUMENT, document)


def test_portable_default_timeout_schema_accepts_only_zero():
    document = {"format": "looper-config/v4", "managers": [
        {"id": "manager", "name": "Manager", "idle_timeout_seconds": 0}]}
    assert api_v1.validate_arguments(api_v1.CONFIG_IMPORT_DOCUMENT, document) == document
    document["managers"][0]["idle_timeout_seconds"] = 60
    with pytest.raises(OpError):
        api_v1.validate_arguments(api_v1.CONFIG_IMPORT_DOCUMENT, document)
