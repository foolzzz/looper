"""``operator_sender_id``: the one allowlisted sender whose direct messages are the user's own (ledger RFC 4.3)."""

import pytest

from looper.agents import api_v1
from looper.agents.channels.allowlist import AllowlistError, normalized_config
from looper.agents.v1_domains.channel_configs import CONFIGS

LARK = {"allowed_open_ids": ["ou_operator01", "ou_colleague01"]}


@pytest.mark.parametrize("kind,config", [
    ("lark", {**LARK, "operator_sender_id": "ou_operator01"}),
    ("lark_bot", {**LARK, "operator_sender_id": " ou_colleague01 "}),
    ("telegram", {"allowed_user_ids": "7, 8", "operator_sender_id": "8"}),
])
def test_an_allowlisted_sender_can_be_the_operator(kind, config):
    stored = normalized_config(kind, config)
    assert stored["operator_sender_id"] == config["operator_sender_id"].strip()


@pytest.mark.parametrize("kind,config", [
    ("lark", {**LARK, "operator_sender_id": "ou_stranger001"}),   # valid id, not on the list
    ("lark", {**LARK, "operator_sender_id": "not an id"}),        # not an id
    ("lark", {**LARK, "operator_sender_id": 5}),
    ("lark", {"operator_sender_id": "ou_operator01"}),            # no allowlist to be on
    ("telegram", {"allowed_user_ids": [7], "operator_sender_id": "-100123"}),
])
def test_anything_else_is_refused(kind, config):
    with pytest.raises(AllowlistError):
        normalized_config(kind, config)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_it_can_be_cleared(value):
    assert normalized_config("lark", {**LARK, "operator_sender_id": value})["operator_sender_id"] is None


@pytest.mark.parametrize("kind", ["telegram", "lark", "lark_bot"])
def test_the_api_schema_accepts_it(kind):
    api_v1.validate_arguments(CONFIGS[kind], {"operator_sender_id": "x"})
    api_v1.validate_arguments(CONFIGS[kind], {"operator_sender_id": None})
