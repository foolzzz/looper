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
BOT_OPEN_ID = "ou_bot0000001"  # every profile's bot, as its info says
# A message's @-mention of the bot, as lark-cli 1.0.97 shows it: in ``mentions`` by open_id, and as ``@name`` in the
# text.
BOT_MENTION = {"id": BOT_OPEN_ID, "key": "@_user_1", "name": "HelperBot"}

_STAND_IN = """\
import base64, fcntl, json, os, re, sys, time
world = json.load(open({world!r}))
args = sys.argv[1:]
def _process_start():
    # The kernel's start of this process, taken before any sleep. The argv line below can still be appended
    # after a later call: a try killed at its timeout keeps running until the signal lands, and on a loaded
    # machine that write is not the call order. Callers that care about order read ".started" and sort by this.
    pid = os.getpid()
    try:
        if os.path.exists("/proc/self/stat"):
            text = open("/proc/self/stat").read()
            # field 22 is starttime in clock ticks; field 3 (state) is the first token after comm.
            ticks = int(text[text.rfind(")") + 2:].split()[19])
            return (ticks / float(os.sysconf("SC_CLK_TCK")), pid)
    except (OSError, ValueError, IndexError):
        pass
    try:
        import ctypes
        import ctypes.util
        class _Info(ctypes.Structure):
            _fields_ = [("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
                        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
                        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
                        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
                        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
                        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
                        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
                        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
                        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
                        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
                        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64)]
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib")
        info = _Info()
        size = ctypes.sizeof(info)
        if libc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) == size and info.pbi_pid == pid:
            return (info.pbi_start_tvsec + info.pbi_start_tvusec / 1e6, pid)
    except (AttributeError, OSError, ValueError):
        pass
    return (time.time(), pid)
_start = _process_start()
with open({log!r} + ".started", "a") as started:
    fcntl.flock(started, fcntl.LOCK_EX)
    started.write(json.dumps({{"start": _start[0], "pid": _start[1], "argv": args}}) + "\\n")
with open({log!r}, "a") as log:
    log.write(json.dumps(args) + "\\n")
def refuse(message, code=2, api_code=None):
    error = {{"type": "validation", "message": message}}
    if api_code is not None:
        error = {{"type": "api", "code": api_code, "message": message}}
    print(json.dumps({{"ok": False, "error": error}}), file=sys.stderr)
    sys.exit(code)
def answer(value):
    print(json.dumps(value))
    sys.exit(0)
name = ""
if args[:1] == ["--profile"]:
    name, args = args[1], args[2:]
key = None
if "--idempotency-key" in args:
    at = args.index("--idempotency-key")
    if len(args) <= at + 1 or not 0 < len(args[at + 1]) <= 50:
        refuse("--idempotency-key: at most 50 characters")
    key = args[at + 1]
    args = args[:at] + args[at + 2:]
if "--reply-in-thread" in args:
    args.remove("--reply-in-thread")
for flag in ("--text", "--markdown"):
    # lark-cli 1.0.100 reads "@path" from a file and takes "@@" for a literal "@".
    if flag in args and args[args.index(flag) + 1][:1] == "@" and args[args.index(flag) + 1][:2] != "@@":
        refuse('%s: cannot read file "%s"' % (flag, args[args.index(flag) + 1][1:]))
profile = world["profiles"].get(name or world["active"])
if profile is None:
    refuse('profile "%s" not found' % name, 3)
if args == ["whoami"]:
    answer({{"profile": name or world["active"], "appId": profile["app_id"], "available": profile["signed_in"],
             "identity": "user", "tokenStatus": "ready" if profile["signed_in"] else "missing"}})
if args == ["api", "GET", "/open-apis/bot/v3/info", "--as", "bot"]:
    if not profile["signed_in"]:
        refuse("not logged in")
    answer({{"ok": True, "identity": "bot", "data": {{"app_name": profile["bot"], "open_id": {bot!r},
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
def deliver(target, message_id, chat):
    # A separate journal records deliveries; the original argv log still records every attempt.
    with open({log!r} + ".delivered.jsonl", "a+") as journal:
        fcntl.flock(journal, fcntl.LOCK_EX)
        journal.seek(0)
        previous = [json.loads(line) for line in journal]
        found = next((item for item in previous if key and item["key"] == key and item["profile"] == name), None)
        if found:
            message_id = found["message_id"]
        match = re.search(r"(rl230020|rl99991400|to|hang)x([0-9]+)$", target)
        fail = False
        if match:
            with open({log!r}) as attempts:
                calls = [json.loads(line) for line in attempts]
                target_attempts = sum(target in call and any(method in call for method in
                    ("+messages-send", "+messages-reply")) for call in calls)
            fail = target_attempts <= int(match[2])
            if fail and match[1] in ("rl230020", "rl99991400"):
                refuse("rate limited", 1, 230020 if match[1] == "rl230020" else 99991400)
        if not found:
            journal.write(json.dumps({{"key": key, "profile": name, "target": target,
                                      "message_id": message_id, "chat_id": chat}}) + "\\n")
            journal.flush()
        if fail:
            if match[1] == "hang":
                # Delivery happened, but no response reaches the caller before its 20 s deadline.
                time.sleep(60)
            print(json.dumps({{"ok": False, "identity": "bot", "error": {{"type": "network", "subtype": "timeout",
                "message": "API call failed: server time out error"}}}}), file=sys.stderr)
            sys.exit(4)
        answer({{"ok": True, "identity": "bot", "data": {{"message_id": message_id, "chat_id": chat}}}})
if args[:4] == ["im", "+messages-send", "--as", "bot"]:
    rest = args[4:]
    if len(rest) != 4 or rest[0] not in ("--chat-id", "--user-id") or rest[2] not in ("--text", "--markdown"):
        refuse("specify at least one of --chat-id or --user-id")
    if rest[1].endswith("gone"):
        refuse("Bot/User can NOT be out of the chat")
    if rest[1].endswith("servertimeout"):  # the platform's time-out: whether it was sent is not known
        print(json.dumps({{"ok": False, "identity": "bot", "error": {{"type": "network", "subtype": "timeout",
            "message": "API call failed: server time out error"}}}}), file=sys.stderr)
        sys.exit(4)
    with open({log!r}) as sent:
        count = sum(1 for line in sent if "+messages-send" in line)
    deliver(rest[1], "om_%04d" % count, rest[1])
if args[:5] == ["im", "+messages-reply", "--as", "bot", "--message-id"] and len(args) == 8:
    if args[6] not in ("--text", "--markdown"):
        refuse("specify one of --text, --markdown or --content")
    if args[5].endswith("gone"):
        refuse("the message was recalled", api_code=230011)
    with open({log!r}) as sent:
        count = sum(1 for line in sent if "+messages-reply" in line)
    deliver(args[5], "om_r%04d" % count, "oc_group")
if args[:5] == ["im", "+messages-edit", "--as", "bot", "--message-id"] and len(args) == 8:
    if args[6] not in ("--text", "--markdown"):
        refuse("specify one of --text, --markdown or --content")
    if args[5].endswith("gone"):
        refuse("the message was recalled or the bot did not send it")
    answer({{"ok": True, "identity": "bot", "data": {{"message_id": args[5]}}}})
if args[:4] == ["im", "+messages-resources-download", "--as", "bot"]:
    rest = dict(zip(args[4::2], args[5::2]))
    key, output = rest["--file-key"], rest["--output"]
    if "/" in output or chr(92) in output or ".." in output:
        refuse("unsafe output path")
    if key.endswith("noscope"):
        print(json.dumps({{"ok": False, "error": {{"type": "authorization", "code": 99991672,
            "message": "access denied", "missing_scopes": ["im:message:readonly"]}}}}), file=sys.stderr)
        sys.exit(1)
    if key.endswith("slow"):
        time.sleep(3)
    resource = world["resources"].get(rest["--message-id"] + "/" + key)
    if resource is None:
        refuse("File not in msg.", api_code=234003)
    content = base64.b64decode(resource["base64"])
    path = os.path.join(os.getcwd(), output)
    with open(path, "wb") as saved:
        saved.write(content)
    answer({{"ok": True, "identity": "bot", "data": {{"saved_path": path, "size_bytes": len(content)}}}})
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
if args[:2] == ["api", "GET"] and args[2].startswith("/open-apis/im/v1/messages/") and args[3:] == ["--as", "bot"]:
    # A message read by id: the world's raw items (a merged forward first, then its children). An id ending in
    # "slow" outlasts the caller's first try; one ending in "servertimeout" gets the platform's time-out, as a long
    # merged forward sometimes does.
    from urllib.parse import unquote
    message_id = unquote(args[2].rsplit("/", 1)[1])
    if message_id.endswith("slow"):
        time.sleep(5)
    if message_id.endswith("servertimeout"):
        print(json.dumps({{"ok": False, "identity": "bot", "error": {{"type": "network", "subtype": "timeout",
            "message": "API call failed: server time out error"}}}}), file=sys.stderr)
        sys.exit(4)
    if message_id.endswith("noscope"):
        print(json.dumps({{"ok": False, "error": {{"type": "authorization", "code": 99991672,
            "message": "access denied", "missing_scopes": ["im:message:readonly"]}}}}), file=sys.stderr)
        sys.exit(1)
    items = world.get("messages", {{}}).get(message_id)
    if items is None:
        refuse("the message does not exist", api_code=230001)
    answer({{"code": 0, "msg": "success", "data": {{"items": items}}}})
if args[:5] == ["api", "GET", "/open-apis/im/v1/messages/mget", "--as", "bot"] and args[5:6] == ["--params"]:
    # The batch read: the world's "batch" items, else the message's own raw item, without a merged forward's
    # children; at once, also for an id the single read is slow on.
    message_id = json.loads(args[6])["message_ids"]
    items = world.get("batch", {{}}).get(message_id)
    if items is None:
        items = [item for item in world.get("messages", {{}}).get(message_id) or []
                 if item.get("message_id") == message_id][:1]
    if not items:
        refuse("the message does not exist", api_code=230001)
    answer({{"code": 0, "msg": "success", "data": {{"items": items}}}})
if args[:5] == ["api", "GET", "/open-apis/im/v1/messages", "--as", "bot"] and args[5:6] == ["--params"]:
    params = json.loads(args[6])
    if params.get("container_id_type") != "thread":
        refuse("this stand-in lists threads only")
    answer({{"code": 0, "data": {{"items": world.get("threads", {{}}).get(params["container_id"], []),
                                  "has_more": False}}}})
if args[:6] == ["im", "+messages-mget", "--as", "bot", "--no-reactions", "--message-ids"] and len(args) == 7:
    # lark-cli's rendering of the same message: content as text, the sender's name resolved.
    messages = world.get("mget", {{}}).get(args[6])
    if messages is None:
        refuse("the message does not exist", api_code=230001)
    answer({{"ok": True, "identity": "bot", "data": {{"messages": messages, "total": len(messages)}}}})
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
    "resources": {},
    # Read by id (raw items), through the batch read when it differs, through lark-cli's shortcut (rendered) and
    # threads' messages, newest first.
    "messages": {},
    "batch": {},
    "mget": {},
    "threads": {},
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
        # In a group: a message that names the bot without @-mentioning it, a listed sender's @-mention, an unlisted
        # sender's, and a message that @-mentions someone else.
        {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group", "message_id": "om_10",
         "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": "@HelperBot hi all"},
        {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group", "message_id": "om_11",
         "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": "@HelperBot what runs now?",
         "mentions": [BOT_MENTION]},
        {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group", "message_id": "om_12",
         "message_type": "text", "sender_id": OTHER, "sender_type": "user", "content": "@HelperBot the secret plan",
         "mentions": [BOT_MENTION]},
        {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group", "message_id": "om_13",
         "message_type": "text", "sender_id": OPEN_ID, "sender_type": "user", "content": "@Alan Turing look",
         "mentions": [{"id": OTHER, "key": "@_user_1", "name": "Alan Turing"}]},
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
    fake_claude(bin_dir / "lark-cli", _STAND_IN.format(log=str(log), world=str(world), bot=BOT_OPEN_ID))
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
