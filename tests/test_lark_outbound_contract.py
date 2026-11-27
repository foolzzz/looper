"""Every Lark message write goes through the image neutralizer; a new write path that skips it fails here.

Two layers: each public send/reply/edit/card entry point is driven with remote-image Markdown and the body that
reaches the wire is checked, and a scan of ``looper/`` fails on a Lark message write or a Markdown body builder
that is not in the lists below, so a new path has to be added here, with its neutralization, on purpose.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from looper.agents.card_actions import Card, lark_card
from looper.agents.channels import lark_cli
from looper.agents.channels.lark_api import LarkApi
from tests.test_markdown_images import has_live_image as _live_image

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST = "remote.example.invalid"
PAYLOADS = [
    f"![chart](https://{HOST}/a.png?d=secret)",
    f"before <img src=\"https://{HOST}/b.png\"> after",
    f"![ref][r]\n\n[r]: https://{HOST}/c.png",
]


class Recorder:
    """A ``LarkApi.call`` that records each write instead of sending it."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, Any]] = []

    async def __call__(self, method: str, path: str, *, params: Any = None, body: Any = None) -> dict[str, Any]:
        self.writes.append((method, path, body))
        return {"data": {"message_id": "om_x", "chat_id": "oc_x"}}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[LarkApi, Recorder]:
    recorder = Recorder()
    client = LarkApi.__new__(LarkApi)
    monkeypatch.setattr(LarkApi, "call", lambda self, method, path, **kw: recorder(method, path, **kw))
    return client, recorder


@pytest.mark.parametrize("payload", PAYLOADS)
def test_cli_transport_write_commands_carry_no_remote_image(payload: str) -> None:
    commands = [
        lark_cli.send_command(chat_id="oc_x", markdown=payload),
        lark_cli.send_command(user_id="ou_x", markdown=payload),
        lark_cli.reply_command("om_x", markdown=payload),
        lark_cli.edit_command("om_x", markdown=payload),
    ]
    for argv in commands:
        assert not _live_image(argv[argv.index("--markdown") + 1]), argv


@pytest.mark.parametrize("payload", PAYLOADS)
def test_bot_token_transport_writes_carry_no_remote_image(api: tuple[LarkApi, Recorder], payload: str) -> None:
    client, recorder = api

    async def run() -> None:
        await client.send(chat_id="oc_x", markdown=payload)
        await client.send(user_id="ou_x", markdown=payload)
        await client.reply("om_x", markdown=payload)
        await client.edit("om_x", markdown=payload)

    asyncio.run(run())
    assert len(recorder.writes) == 4
    for _, _, body in recorder.writes:
        content = json.loads(body["content"])
        assert not _live_image(content["zh_cn"]["content"][0][0]["text"]), body


@pytest.mark.parametrize("payload", PAYLOADS)
def test_cards_carry_no_remote_image(payload: str) -> None:
    for card in (Card(text=payload), Card(text=payload, mention="ou_abc123")):
        text = lark_card(card)["body"]["elements"][0]["content"]
        assert not _live_image(text), text


def test_the_image_probe_detects_a_live_image() -> None:
    """The assertions above must be able to fail: the unneutralized payloads are live images."""
    assert all(_live_image(payload) for payload in PAYLOADS[:2])
    assert _live_image(lark_cli.send_command(chat_id="oc_x", text=PAYLOADS[0])[-1])


# --- A new write path or Markdown body builder must be listed here ---

LARK_WRITE_METHODS = {"POST", "PUT", "PATCH"}
# (file, function) that write a message to the platform, and what keeps a remote image out of it.
MESSAGE_WRITES = {
    ("looper/agents/channels/lark_api.py", "send"): "body from _message (neutralized)",
    ("looper/agents/channels/lark_api.py", "reply"): "body from _message (neutralized)",
    ("looper/agents/channels/lark_api.py", "edit"): "body from _message (neutralized)",
    ("looper/agents/channels/lark_api.py", "send_card"): "card JSON built by card_actions.lark_card (neutralized)",
    ("looper/agents/channels/lark_api.py", "reply_card"): "card JSON built by card_actions.lark_card (neutralized)",
    ("looper/agents/channels/lark_api.py", "update_card"): "card JSON built by card_actions.lark_card (neutralized)",
    ("looper/agents/channels/lark_api.py", "react"): "an emoji type, no Markdown",
}
# Functions that build a Markdown element of a message; each must call neutralize_images itself.
MARKDOWN_BUILDERS = {
    ("looper/agents/channels/lark_api.py", "_message"),
    ("looper/agents/channels/lark_cli.py", "_body"),
    ("looper/agents/card_actions.py", "lark_card"),
}
CLI_WRITE_FLAGS = ("+messages-send", "+messages-reply", "+messages-edit")
CLI_WRITE_FILE = "looper/agents/channels/lark_cli.py"


def _functions(tree: ast.AST):
    """Every function with the nodes of its own body (nested functions are their own entry)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


REQUEST_HELPERS = {"call", "_request", "request", "_call"}


def _texts(node: ast.AST, env: dict[str, list[str]]) -> list[str]:
    """The strings ``node`` can evaluate to: literals, f-strings (``{}`` for a hole), ``+``, and names assigned
    in the same function. Empty when it is not statically known."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = [part.value if isinstance(part, ast.Constant) else "{}" for part in node.values]
        return ["".join(str(part) for part in parts)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [left + right for left in _texts(node.left, env) for right in _texts(node.right, env)]
    if isinstance(node, ast.Name):
        return env.get(node.id, [])
    return []


def _local_strings(function: ast.AST) -> dict[str, list[str]]:
    env: dict[str, list[str]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                env.setdefault(target.id, []).extend(_texts(value, env))
    return env


def message_write_calls(source: str) -> list[tuple[str, int]]:
    """(function, line) of every ``x.call|_request(<POST|PUT|PATCH>, "/open-apis/im/v1/messages...")``, with
    the method and the path given directly or through a local variable."""
    found = []
    for function in _functions(ast.parse(source)):
        env = _local_strings(function)
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and len(node.args) >= 2):
                continue
            if getattr(node.func, "id", getattr(node.func, "attr", "")) not in REQUEST_HELPERS:
                continue
            methods, paths = _texts(node.args[0], env), _texts(node.args[1], env)
            if (any(m in LARK_WRITE_METHODS for m in methods)
                    and any(p.startswith("/open-apis/im/v1/messages") for p in paths)):
                found.append((function.name, node.lineno))
    return found


def markdown_builders(source: str) -> list[tuple[str, bool]]:
    """(function, calls neutralize_images) of every function that builds a Markdown element: a dict with
    ``"tag": "md"|"markdown"``, or the ``--markdown`` flag."""
    found = []
    for function in _functions(ast.parse(source)):
        builds = neutralizes = False
        for node in ast.walk(function):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "tag" and isinstance(value, ast.Constant)
                            and value.value in {"md", "markdown", "lark_md"}):
                        builds = True
            elif isinstance(node, ast.Constant) and node.value == "--markdown":
                builds = True
            elif (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", ""))
                  == "neutralize_images"):
                neutralizes = True
        if builds:
            found.append((function.name, neutralizes))
    return found


def cli_write_flags(source: str) -> list[tuple[str, int]]:
    found = []
    for function in _functions(ast.parse(source)):
        for node in ast.walk(function):
            if isinstance(node, ast.Constant) and node.value in CLI_WRITE_FLAGS:
                found.append((function.name, node.lineno))
    return found


def test_the_scans_find_what_they_look_for() -> None:
    write = 'async def go(self):\n    await self.call("POST", f"/open-apis/im/v1/messages/{i}/reply", body=b)'
    assert message_write_calls(write) == [("go", 2)]
    assert not message_write_calls('async def go(self):\n    await self.call("GET", "/open-apis/im/v1/messages")')
    assert not message_write_calls('async def go(self):\n    await self.call("POST", "/open-apis/contact/v3/x")')
    assert not message_write_calls('async def go(self):\n    path = "/open-apis/im/v1/messages"\n    await self.call("GET", path)')


def test_the_write_scan_sees_a_path_in_a_variable() -> None:
    literal = 'async def go(self):\n    path = "/open-apis/im/v1/messages"\n    await self.call("POST", path, body=b)'
    fstring = 'async def go(self, i):\n    path = f"/open-apis/im/v1/messages/{i}"\n    await self.call("PUT", path)'
    method = 'async def go(self):\n    verb = "PATCH"\n    await self.call(verb, "/open-apis/im/v1/messages/x")'
    assert message_write_calls(literal) == [("go", 3)]
    assert message_write_calls(fstring) == [("go", 3)]
    assert message_write_calls(method) == [("go", 3)]


def test_the_write_scan_sees_a_direct_request_call() -> None:
    direct = 'async def go(self):\n    await self._request("POST", "/open-apis/im/v1/messages", body=b)'
    via_name = 'async def go(self):\n    p = "/open-apis/im/v1/messages"\n    await self._request("POST", p)'
    assert message_write_calls(direct) == [("go", 2)]
    assert message_write_calls(via_name) == [("go", 3)]
    assert markdown_builders('def f(t):\n    return {"tag": "md", "text": t}') == [("f", False)]
    assert markdown_builders('def f(t):\n    return {"tag": "md", "text": neutralize_images(t)}') == [("f", True)]
    assert markdown_builders('def f(t):\n    return ("--markdown", t)') == [("f", False)]
    assert not markdown_builders('def f(t):\n    return {"tag": "plain_text", "content": t}')
    assert cli_write_flags('def f():\n    return ["im", "+messages-send"]') == [("f", 2)]


def test_every_lark_message_write_is_listed() -> None:
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted((REPO_ROOT / "looper").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for function, line in message_write_calls(path.read_text()):
            found.setdefault((relative, function), []).append(line)
    unlisted = {key: lines for key, lines in found.items() if key not in MESSAGE_WRITES}
    assert not unlisted, ("a Lark message write must keep remote images out and be listed in MESSAGE_WRITES with what "
                          f"does it: {unlisted}")
    assert set(MESSAGE_WRITES) <= set(found), f"listed but not found: {sorted(set(MESSAGE_WRITES) - set(found))}"


def test_every_markdown_body_builder_neutralizes() -> None:
    found: dict[tuple[str, str], bool] = {}
    for path in sorted((REPO_ROOT / "looper").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for function, neutralizes in markdown_builders(path.read_text()):
            found[(relative, function)] = neutralizes
    assert not {key for key, ok in found.items() if not ok}, (
        f"a Markdown message body must pass neutralize_images: {sorted(k for k, ok in found.items() if not ok)}")
    assert set(found) == MARKDOWN_BUILDERS, f"unlisted or missing builders: {set(found) ^ MARKDOWN_BUILDERS}"


def test_lark_cli_write_flags_live_only_in_the_neutralizing_commands() -> None:
    found = {}
    for path in sorted((REPO_ROOT / "looper").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for function, line in cli_write_flags(path.read_text()):
            found[(relative, function)] = line
    assert set(found) == {(CLI_WRITE_FILE, name) for name in ("send_command", "reply_command", "edit_command")}, found


# --- The mention is validated where the Card is built ---

@pytest.mark.parametrize("mention", ["ou_abc123", "ou_1", "ou_ABCdef0123456789"])
def test_a_well_formed_open_id_is_mentioned(mention: str, caplog: pytest.LogCaptureFixture) -> None:
    card = Card(text="done", mention=mention)
    assert card.mention == mention
    assert lark_card(card)["body"]["elements"][0]["content"] == f"<at id={mention}></at> done"
    assert not caplog.records


@pytest.mark.parametrize("mention", [
    "", "ou_", "abc", "cli_abc123", "ou_a b", "ou_a></at><at id=ou_b", "ou_a\nb", 'ou_a"x', "ou_é", " ou_a", "ou_a ",
    "OU_abc", "ou_abc\n",
])
def test_a_malformed_mention_is_dropped_with_a_warning(mention: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="looper.agents.card_actions"):
        card = Card(text="done", mention=mention)
    assert card.mention is None
    assert lark_card(card)["body"]["elements"][0]["content"] == "done"
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    # the value itself is not logged: no record argument is it or contains it
    for record in caplog.records:
        args = record.args if isinstance(record.args, tuple) else tuple((record.args or {}).values())
        assert all(mention not in str(arg) or not mention for arg in args), record.args
        assert all(arg != mention for arg in args)


def test_a_long_malformed_mention_is_not_in_the_log_text(caplog: pytest.LogCaptureFixture) -> None:
    mention = "ou_a b-unique-7f3c9e1d5a2b4c68"
    with caplog.at_level("WARNING", logger="looper.agents.card_actions"):
        Card(text="done", mention=mention)
    assert caplog.records
    assert "7f3c9e1d5a2b4c68" not in caplog.text


def test_a_card_restored_from_storage_is_validated_too(caplog: pytest.LogCaptureFixture) -> None:
    assert Card.from_dict({"text": "t", "mention": "ou_ok1"}).mention == "ou_ok1"
    assert Card.from_dict({"text": "t", "mention": "<at id=x>"}).mention is None
    assert Card.from_dict({"text": "t", "mention": None}).mention is None
    assert Card("t", (), "ou_ok1").settled("done").mention == "ou_ok1"
