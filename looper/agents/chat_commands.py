"""The chat commands Looper answers itself, in one list: Telegram's menu and ``/help`` (every channel) read it."""

from __future__ import annotations

import re

from . import chat_settings

# (name without the slash, Chinese description); the order is the menu's and the help's.
COMMANDS: list[tuple[str, str]] = [
    ("status", "查看当前任务进度"),
    ("new", "开始新的 Manager 会话"),
    ("stop", "停止管家当前的回复"),
    ("gates", "列出等你回答的关卡"),
    ("gate", "回答或结束一个关卡：/gate 编号 回答，/gate 编号 end"),
    ("profile", "查看或切换 Manager 会话的 Agent Profile"),
    ("help", "显示帮助"),
]

# "/help" and Telegram's "/help@bot".
_HELP = re.compile(r"^/help(?:@\w+)?$", re.IGNORECASE)


def available() -> list[tuple[str, str]]:
    """The commands in use: /stop only while stopping a reply is on (``chat_settings``)."""
    return [(name, desc) for name, desc in COMMANDS if name != "stop" or chat_settings.stop_enabled()]


def is_help(text: str) -> bool:
    return bool(_HELP.match(text.strip()))


def help_text() -> str:
    return "Looper 命令：\n直接发消息 — 提需求或问问题\n" + "\n".join(f"/{name} — {desc}" for name, desc in available())
