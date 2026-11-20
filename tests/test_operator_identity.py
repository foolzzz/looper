"""Who the operator is: the typed rule every operator-only feature uses."""

import pytest

from looper.agents.operator_identity import is_operator

OP = "ou_operatorsynthetic"


@pytest.mark.parametrize("turn, expected", [
    ({"channel": "web", "group": False, "sender": None, "client_id": None, "operator_id": None}, True),
    ({"channel": "web", "group": False, "sender": None, "client_id": "client-1", "operator_id": OP}, False),
    ({"channel": "lark", "group": False, "sender": OP, "client_id": None, "operator_id": OP}, True),
    ({"channel": "lark", "group": False, "sender": "ou_someoneelse", "client_id": None, "operator_id": OP}, False),
    ({"channel": "lark", "group": True, "sender": OP, "client_id": None, "operator_id": OP}, False),
    ({"channel": "telegram", "group": False, "sender": "12345", "client_id": None, "operator_id": "12345"}, True),
    ({"channel": "lark", "group": False, "sender": OP, "client_id": None, "operator_id": None}, False),
    ({"channel": "lark", "group": False, "sender": None, "client_id": None, "operator_id": OP}, False),
])
def test_only_the_web_chat_and_the_operators_own_direct_chat_are_the_operator(turn, expected):
    assert is_operator(**turn) is expected
