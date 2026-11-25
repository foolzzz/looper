"""Native CLI stand-in used only by group dialogs; never contacts a network."""

from __future__ import annotations

import json
import os
import select
from pathlib import Path
import sys
import time

OPERATOR = "ou_evaloperator01"
OTHER = "ou_evalother0001"
BOT_SENDER = "ou_evalpeerbot01"
BOT = "ou_evalbot000001"
APP = "cli_evalsession"
CHAT = "oc_evalsession"


def main(root: Path) -> None:
    args = sys.argv[1:]
    if args[:1] == ["--profile"]:
        args = args[2:]
    with (root / "calls.jsonl").open("a") as out:
        out.write(json.dumps(args) + "\n")

    def answer(data):
        print(json.dumps(data), flush=True)

    def ok(data):
        answer({"ok": True, "identity": "bot", "data": data})

    if args == ["whoami"]:
        answer({"profile": "eval", "appId": APP, "available": True,
                "identity": "user", "tokenStatus": "ready"})
    elif args[:3] == ["api", "GET", "/open-apis/bot/v3/info"]:
        ok({"app_name": "Eval helper", "open_id": BOT, "activate_status": 2})
    elif args[:2] == ["contact", "+search-user"]:
        ok({"has_more": False, "users": [
            {"open_id": OPERATOR, "localized_name": "Eval operator", "email": "operator@example.test"},
            {"open_id": OTHER, "localized_name": "Eval member", "email": "member@example.test"},
            {"open_id": BOT_SENDER, "localized_name": "Eval peer bot"}]})
    elif args[:3] == ["event", "consume", "im.message.receive_v1"]:
        # Keep one consumer alive; the driver appends events only after its model is configured.
        with (root / "events.jsonl").open() as events:
            while True:
                line = events.readline()
                if line:
                    print(line.rstrip("\n"), flush=True)
                else:
                    ready, _, _ = select.select([0], [], [], 0.05)
                    if ready and os.read(0, 1) == b"":
                        break
    elif args[:2] == ["im", "+messages-resources-download"]:
        key = args[args.index("--file-key") + 1]
        output = Path(args[args.index("--output") + 1])
        source = root / "resources" / key
        if source.parent != root / "resources" or output.name != str(output):
            raise SystemExit("eval downloads require a synthetic key and a bare output filename")
        output.write_bytes(source.read_bytes())
        ok({"saved_path": str(output)})
    elif args[:2] in (["im", "+messages-reply"], ["im", "+messages-send"], ["im", "+messages-edit"]):
        flag = "--markdown" if "--markdown" in args else "--text"
        text = args[args.index(flag) + 1]
        if text.startswith("@@"):
            text = text[1:]
        target_flag = next(key for key in ("--message-id", "--chat-id", "--user-id") if key in args)
        target = args[args.index(target_flag) + 1]
        message_id = "om_eval" + str(time.time_ns())
        with (root / "delivered.jsonl").open("a") as out:
            out.write(json.dumps({"method": args[1], "target": target, "text": text}) + "\n")
        ok({"message_id": message_id, "chat_id": CHAT})
    elif args[:3] == ["im", "reactions", "create"]:
        ok({"reaction_id": "re_eval", "reaction_type": {"emoji_type": "Typing"}})
    elif args[:3] == ["im", "reactions", "delete"]:
        ok({})
    elif args[:2] == ["api", "GET"]:
        ok({"items": [], "has_more": False, "chat_mode": "group"})
    else:
        print(json.dumps({"ok": False, "error": {"type": "validation",
                                                  "message": "unsupported eval CLI command"}}), file=sys.stderr)
        raise SystemExit(2)


def install(bin_dir: Path, root: Path) -> None:
    root.mkdir()
    (root / "events.jsonl").touch()
    (root / "delivered.jsonl").touch()
    script = bin_dir / "lark-cli"
    source = Path(__file__).resolve()
    script.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv.insert(1, {str(root)!r})\n"
                      f"runpy.run_path({str(source)!r}, run_name='__main__')\n")
    script.chmod(0o755)


if __name__ == "__main__":
    location = Path(sys.argv.pop(1))
    main(location)
