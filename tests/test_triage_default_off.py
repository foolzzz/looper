"""The foundation leaves model-visible tools and public subscription creation unchanged."""

import hashlib
import json

import pytest

from looper.agents import subscriptions
from looper.agents.ops.clients import CLIENT_TOOLS
from looper.agents.ops.service import MANAGER_TOOLS


def test_tools_are_byte_identical_to_before_the_triage_foundation():
    # Measured from the parent main checkout, including every name, description and argument schema.
    for tools, expected in ((MANAGER_TOOLS, "d27d964c3e11a4d5781f849427687f9176a03f5569c812990c0d34c52d5eb9fc"),
                            (CLIENT_TOOLS, "e918a38131eb6898e40ffbf04852f4ff356068facf2a9721875fbb887108792c")):
        value = [{"name": t.name, "description": t.description, "schema": t.schema} for t in tools]
        assert hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() == expected


def test_triage_cannot_be_created_before_its_execution_is_installed():
    with pytest.raises(ValueError, match="output_kind"):
        subscriptions.parse({"output_kind": "triage"}, update=True)
