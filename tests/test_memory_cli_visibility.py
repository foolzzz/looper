"""A memory confirmation is a dashboard/DM action even when a CLI has an operator token."""

import pytest

from looper import loopercli
from looper.agents import api_v1


def test_memory_confirmation_is_not_a_generated_cli_operation():
    document = api_v1.document()
    generated = {(head, tail) for head, tail, *_ in loopercli.generated_operations(document)}
    assert {("memory", "confirm"), ("memory", "confirm-many"), ("memory", "edit")}.isdisjoint(generated)
    assert {("memory", "reject"), ("memory", "retry"), ("memory", "delete")} <= generated
    for name in ("confirm", "confirm_many", "edit"):
        declared = document["paths"][f"/memory/{name}"]["post"]
        assert declared["x-looper-cli"] is False and declared["x-looper-operator-only"] is True


@pytest.mark.parametrize("action", ["confirm", "confirm-many", "edit"])
def test_memory_confirmation_commands_are_refused_by_the_cli_parser(action):
    with pytest.raises(loopercli.UsageError):
        loopercli.build_parser(api_v1.document()).parse_args([
            "memory", action, "--candidate", "M-1", "--operator-token-file", "synthetic-token"])
