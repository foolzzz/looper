"""A stand-in ``lark-cli`` for the tests: it answers the Lark channel's calls from a small world of profiles, users
and events, as lark-cli 1.0.97 would, and anything else as lark-cli does (a JSON error on stderr, a non-zero exit).
Nothing reaches a real chat, user or account; ``conftest`` puts a lark-cli that refuses every call first on PATH
for all other tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from fake_claude import fake_claude
from looper.agents.channels import lark_cli

OPEN_ID = "ou_abcdefgh12"
OTHER = "ou_zyxwvut987"

_STAND_IN = """\
import json, sys
world = json.load(open({world!r}))
args = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(json.dumps(args) + "\\n")
def refuse(message, code=2):
    print(json.dumps({{"ok": False, "error": {{"type": "validation", "message": message}}}}), file=sys.stderr)
    sys.exit(code)
def answer(value):
    print(json.dumps(value))
    sys.exit(0)
name = ""
if args[:1] == ["--profile"]:
    name, args = args[1], args[2:]
profile = world["profiles"].get(name or world["active"])
if profile is None:
    refuse('profile "%s" not found' % name, 3)
if args == ["whoami"]:
    answer({{"profile": name or world["active"], "appId": profile["app_id"], "available": profile["signed_in"],
             "identity": "user", "tokenStatus": "ready" if profile["signed_in"] else "missing"}})
if args == ["api", "GET", "/open-apis/bot/v3/info", "--as", "bot"]:
    if not profile["signed_in"]:
        refuse("not logged in")
    answer({{"ok": True, "identity": "bot", "data": {{"app_name": profile["bot"], "open_id": "ou_bot0000001",
                                                    "activate_status": 2}}}})
if args[:4] == ["contact", "+search-user", "--as", "user"]:
    rest = dict(zip(args[4::2], args[5::2]))
    users = world["users"]
    if "--user-ids" in rest:
        ids = rest["--user-ids"].split(",")
        if any(i not in {{u["open_id"] for u in users}} for i in ids):
            refuse("Refer to the documentation to fix the error")
        users = [u for u in users if u["open_id"] in ids]
    if "--query" in rest:
        users = [u for u in users if rest["--query"].lower() in (u["localized_name"] + u["email"]).lower()]
    answer({{"ok": True, "identity": "user", "data": {{"has_more": False, "users": users}}}})
if args[:4] == ["im", "+messages-send", "--as", "bot"]:
    rest = args[4:]
    if len(rest) != 4 or rest[0] not in ("--chat-id", "--user-id") or rest[2] not in ("--text", "--markdown"):
        refuse("specify at least one of --chat-id or --user-id")
    if rest[1].endswith("gone"):
        refuse("Bot/User can NOT be out of the chat")
    with open({log!r}) as sent:
        count = sum(1 for line in sent if "+messages-send" in line)
    answer({{"ok": True, "identity": "bot", "data": {{"message_id": "om_%04d" % count, "chat_id": rest[1]}}}})
if args[:5] == ["im", "+messages-edit", "--as", "bot", "--message-id"] and len(args) == 8:
    if args[6] not in ("--text", "--markdown"):
        refuse("specify one of --text, --markdown or --content")
    if args[5].endswith("gone"):
        refuse("the message was recalled or the bot did not send it")
    answer({{"ok": True, "identity": "bot", "data": {{"message_id": args[5]}}}})
if args[:6] == ["im", "reactions", "create", "--as", "bot", "--message-id"] and args[7:8] == ["--data"]:
    if not json.loads(args[8]).get("reaction_type", {{}}).get("emoji_type"):
        refuse("reaction_type.emoji_type is required")
    if args[6].endswith("noscope"):
        # How lark-cli reports a scope the app lacks.
        print(json.dumps({{"ok": False, "identity": "bot", "error": {{
            "type": "authorization", "subtype": "missing_scope", "code": 99991672,
            "message": "access denied: the app lacks a scope this call needs",
            "missing_scopes": ["im:message.reactions:write_only"]}}}}), file=sys.stderr)
        sys.exit(1)
    answer({{"ok": True, "identity": "bot", "data": {{"reaction_id": "re_" + args[6],
                                                    "reaction_type": json.loads(args[8])["reaction_type"]}}}})
if args == ["event", "consume", "im.message.receive_v1", "--as", "bot"]:
    # Like lark-cli 1.0.97: stdin EOF is the signal to stop, so a consumer whose stdin is /dev/null or closed
    # stops before it delivers anything.
    import os, select
    ready, _, _ = select.select([0], [], [], 0.2)
    if ready and os.read(0, 1) == b"":
        print("[event] stdin closed — shutting down.", file=sys.stderr, flush=True)
        sys.exit(0)
    for event in world["events"]:
        print(json.dumps(event), flush=True)
    sys.exit(0)
refuse('unknown command "%s" for "lark-cli"' % args[0])
"""

WORLD: dict[str, Any] = {
    "active": "cli_main",
    "profiles": {
        "cli_main": {"app_id": "cli_main", "signed_in": True, "bot": "HelperBot"},
        "other": {"app_id": "cli_other", "signed_in": True, "bot": "OtherBot"},
        "out": {"app_id": "cli_out", "signed_in": False, "bot": "OutBot"},
    },
    "users": [
        {"open_id": OPEN_ID, "localized_name": "Ada Lovelace", "email": "ada@example.test", "department": "R&D"},
        {"open_id": OTHER, "localized_name": "Alan Turing", "email": "alan@example.test", "department": "Ops"},
    ],
    "events": [
        {"type": "im.message.receive_v1", "chat_id": "oc_dm", "chat_type": "p2p", "message_id": "om_9",
         "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": "status please"},
        {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group", "message_id": "om_10",
         "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": "@HelperBot hi all"},
    ],
}


@pytest.fixture
def lark_cli_stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stand-in first on PATH; returns the file it logs each argv to."""
    log = tmp_path / "lark-cli.jsonl"
    world = tmp_path / "world.json"
    world.write_text(json.dumps(WORLD))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude(bin_dir / "lark-cli", _STAND_IN.format(log=str(log), world=str(world)))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    lark_cli.forget()
    # The server runs with stdin at /dev/null under launchd; a child that inherits it sees EOF at once.
    saved_stdin = os.dup(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    try:
        yield log
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        lark_cli.forget()
