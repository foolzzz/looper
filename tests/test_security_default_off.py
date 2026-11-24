"""Management pages must not alter existing tools or prompts, even before either switch is used.

The fixture records each individual definition at 2975651b514ac28d2826b768cea2e60171a21b7c.
Runtime off-path behavior is exercised by the outbound-hook and untrusted-fence tests separately.
"""

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from tests.test_tool_levels import _every_tool

FROZEN = json.loads((Path(__file__).parent / "fixtures/security_model_visible.json").read_text())


@pytest.mark.parametrize("item", FROZEN)
def test_security_management_keeps_each_model_visible_definition(item):
    if item.startswith("tool:"):
        tool = _every_tool()[item.removeprefix("tool:")]
        value = json.dumps({"description": tool.description, "schema": tool.schema}, sort_keys=True, ensure_ascii=False)
    else:
        _, module, name = item.split(":")
        value = getattr(importlib.import_module(module), name)
    assert hashlib.sha256(value.encode()).hexdigest() == FROZEN[item], item
