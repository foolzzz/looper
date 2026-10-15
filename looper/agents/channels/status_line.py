"""One status message per user message in a chat channel (Lark, Telegram), edited in place.

The Manager reports what it is doing as it answers (``STATUS_UPDATE`` with
``phase``: thinking, or a tool it runs). The web chat shows that as a passing
line; a chat channel used to send each as a message of its own, so one answer
came after a stream of small messages. Instead, the first phase of a message
sends one status message (「⏳ 处理中：思考中 · 0s」), and later phases edit it:
only when the text changed, at most once every ``EDIT_INTERVAL_SECONDS`` (a
phase that arrives sooner is shown by one trailing edit), and at most
``MAX_PROGRESS_EDITS`` times; after that the line is left as it is. The
answer goes as a new message, so the user is notified, and then the status
message is edited a last time: 「✅ 完成 · 用时 Ns · 工具 N 次」, or 「❌ 出错：…」
when the turn failed. A failed send or edit is logged once and the line is
left alone: it never turns into more messages. A reply the Manager sends on
its own (``autonomous``) has no status message, and a phase that comes after
its message's answer is ignored. What is tracked is bounded.

Only phases go to the status line (``status_kind``). A status that is
neither a phase nor streamed text is the Manager's task progress (「任务已分解为
N 个步骤，开始执行...」, the Planner's progress and its summary): the channel
sends it as a message of its own and leaves the status line alone. Streamed
text and a status without text are not shown.

Before any of that, the channel acknowledges a message it hands to the
Manager with a reaction (``Reactions``: Lark's 「Get」, Telegram's 👀). The
call runs in the background, so it never holds the message up; a failed one
is not retried and never becomes a chat message, and only the first failure
of a run is logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

EDIT_INTERVAL_SECONDS = 3.0
# Feishu edits one message at most 20 times (open.feishu.cn, 编辑消息): 18 progress edits, the closing edit, one spare.
MAX_PROGRESS_EDITS = 18
OPEN_MAX = 200  # status messages being edited
FINISHED_MAX = 1000  # messages answered, whose late phases are ignored
REASON_MAX_CHARS = 80

REACTIONS_PENDING_MAX = 100  # reaction calls still running

# (chat, text) -> the status message's id, or None when it could not be sent.
SendStatus = Callable[[str, str], Awaitable[str | None]]
# (chat, message id, text); raises when the edit fails.
EditStatus = Callable[[str, str, str], Awaitable[None]]
# (chat, message id); raises when the reaction could not be added.
React = Callable[[str, str], Awaitable[None]]

# Claude Code's tools, as a status line names what they do.
_TOOL_WORDS = {
    "Bash": "运行命令", "BashOutput": "查看命令输出", "KillShell": "结束命令",
    "Read": "读文件", "Write": "写文件", "Edit": "改文件", "MultiEdit": "改文件", "NotebookEdit": "改文件",
    "Grep": "搜索", "Glob": "找文件", "LS": "列目录",
    "WebFetch": "查网页", "WebSearch": "搜索网页",
    "Task": "派子任务", "Agent": "派子任务", "TodoWrite": "记待办", "Skill": "使用技能",
}


class StatusKind(Enum):
    """What a chat channel does with a ``STATUS_UPDATE``."""

    PHASE = "phase"  # shown on its message's status line
    PROGRESS = "progress"  # the Manager's task progress: sent as a message of its own
    IGNORED = "ignored"  # streamed text, or no text


def status_kind(payload: Mapping[str, Any]) -> StatusKind:
    if payload.get("delta"):
        return StatusKind.IGNORED
    if payload.get("phase"):
        return StatusKind.PHASE
    return StatusKind.PROGRESS if str(payload.get("text") or "").strip() else StatusKind.IGNORED


def phase_label(payload: Mapping[str, Any]) -> tuple[str, bool] | None:
    """A phase's short label and whether it is a tool run; None for a status that is not a phase."""
    if status_kind(payload) is not StatusKind.PHASE:
        return None
    if payload.get("activity") == "tool":
        tool = str(payload.get("tool") or "tool")
        word = _TOOL_WORDS.get(tool) or ("调用工具" if tool.startswith("mcp__") else "使用工具")
        return f"{word} ({tool})", True
    if payload.get("activity") == "thinking":
        return "思考中", False
    text = " ".join(str(payload.get("text") or "").split())
    return (text[:40] or "处理中"), False


@dataclass
class _Line:
    chat: str
    started: float
    label: str
    tools: int
    message_id: str | None = None
    shown: str = ""
    edits: int = 0
    last_edit: float = 0.0
    broken: bool = False  # a send or an edit failed: the line is left alone
    done: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    trailing: asyncio.Task[None] | None = None


class StatusLines:
    """The status messages of one channel, by correlation id (one per user message)."""

    def __init__(self, *, channel: str, send: SendStatus, edit: EditStatus,
                 clock: Callable[[], float] = time.monotonic, interval: float = EDIT_INTERVAL_SECONDS,
                 max_edits: int = MAX_PROGRESS_EDITS) -> None:
        self._channel = channel
        self._send = send
        self._edit = edit
        self._clock = clock
        self._interval = interval
        self._max_edits = max_edits
        self._open: OrderedDict[str, _Line] = OrderedDict()
        self._finished: OrderedDict[str, None] = OrderedDict()

    def _text(self, line: _Line) -> str:
        return f"⏳ 处理中：{line.label} · {int(self._clock() - line.started)}s"

    async def update(self, correlation_id: str, chat: str, payload: Mapping[str, Any]) -> None:
        """A status for the message ``correlation_id``: send its status message, or edit it when one is due."""
        phase = phase_label(payload)
        if phase is None or correlation_id in self._finished:
            return
        label, tool = phase
        line = self._open.get(correlation_id)
        if line is None:
            line = _Line(chat=chat, started=self._clock(), label=label, tools=int(tool))
            self._open[correlation_id] = line
            while len(self._open) > OPEN_MAX:
                _, dropped = self._open.popitem(last=False)
                self._close(dropped)
            async with line.lock:
                line.shown = self._text(line)
                line.last_edit = self._clock()
                try:
                    line.message_id = await self._send(chat, line.shown)
                except Exception as exc:
                    line.message_id = None
                    logger.warning("%s: could not send the status message: %s", self._channel, exc)
                if line.message_id is None:
                    line.broken = True
            return
        line.label = label
        line.tools += int(tool)
        await self._progress(line)

    async def _progress(self, line: _Line) -> None:
        """Edit the line now if one is allowed and due; else leave one trailing edit for when it is."""
        if line.broken or line.done or line.edits >= self._max_edits:
            return
        wait = self._interval - (self._clock() - line.last_edit)
        if wait > 0:
            if line.trailing is None or line.trailing.done():
                line.trailing = asyncio.ensure_future(self._trail(line, wait))
            return
        async with line.lock:
            if not (line.broken or line.done or line.edits >= self._max_edits):
                await self._apply(line, self._text(line), progress=True)

    async def _trail(self, line: _Line, wait: float) -> None:
        try:
            await asyncio.wait_for(line.closed.wait(), timeout=wait)
            return  # answered meanwhile: the closing edit shows the end
        except asyncio.TimeoutError:
            pass
        async with line.lock:
            if not (line.broken or line.done or line.edits >= self._max_edits):
                await self._apply(line, self._text(line), progress=True)

    async def _apply(self, line: _Line, text: str, *, progress: bool) -> None:
        if line.message_id is None or text == line.shown:
            return
        try:
            await self._edit(line.chat, line.message_id, text)
        except Exception as exc:
            line.broken = True
            logger.warning("%s: could not edit the status message; it is left as it is: %s", self._channel, exc)
            return
        line.shown = text
        if progress:
            line.edits += 1
            line.last_edit = self._clock()

    async def finish(self, correlation_id: str, *, error: str | None = None) -> None:
        """The message ``correlation_id`` was answered: close its status message (done, or the error)."""
        self._finished[correlation_id] = None
        while len(self._finished) > FINISHED_MAX:
            self._finished.popitem(last=False)
        line = self._open.pop(correlation_id, None)
        if line is None:
            return
        self._close(line)
        if line.trailing is not None:
            await asyncio.gather(line.trailing, return_exceptions=True)
        elapsed = int(self._clock() - line.started)
        text = (f"❌ 出错：{' '.join(error.split())[:REASON_MAX_CHARS]}" if error
                else f"✅ 完成 · 用时 {elapsed}s · 工具 {line.tools} 次")
        async with line.lock:
            if not line.broken:
                await self._apply(line, text, progress=False)

    @staticmethod
    def _close(line: _Line) -> None:
        line.done = True
        line.closed.set()

    def tracked(self) -> int:
        return len(self._open)


class Reactions:
    """The reaction a channel adds to each message it hands to the Manager, as soon as it sees the message."""

    def __init__(self, *, channel: str, react: React) -> None:
        self._channel = channel
        self._react = react
        self._pending: set[asyncio.Task[None]] = set()
        self._failed = False  # a failure was logged in this run

    def add(self, chat: str, message_id: str) -> None:
        """React to the message in the background; this returns at once."""
        if not message_id or len(self._pending) >= REACTIONS_PENDING_MAX:
            return
        task = asyncio.ensure_future(self._call(chat, message_id))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _call(self, chat: str, message_id: str) -> None:
        try:
            await self._react(chat, message_id)
        except Exception as exc:
            if self._failed:
                logger.debug("%s: could not react to message %s: %s", self._channel, message_id, exc)
                return
            self._failed = True
            logger.warning("%s: could not react to a message (later failures of this run are not logged): %s",
                           self._channel, exc)

    async def stop(self) -> None:
        """Cancel the calls still running; the next run logs its first failure again."""
        for task in list(self._pending):
            task.cancel()
        await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()
        self._failed = False
