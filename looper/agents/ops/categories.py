"""What each Manager or API-client write does (assistant phase 3 RFC, section 6.3).

Declarations only: no runtime reads this registry yet. Like ``TOOL_LEVELS``, it stays separate from ``Tool`` so
the upgrade probe can still read that declaration's exact shape. Read calls have no category; ``looper_confirm``
uses the original call's kind. Planner and step-agent tools are outside this registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class ActionCategory(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    CONTROL = "control"
    REMOVE = "remove"
    RELAY = "relay"


@dataclass(frozen=True)
class ActionKind:
    category: ActionCategory
    noun: str
    verb: str
    subject: str  # The object key used to find its name and id in arguments or results.


CREATE, UPDATE, CONTROL, REMOVE, RELAY = (ActionCategory.CREATE, ActionCategory.UPDATE, ActionCategory.CONTROL,
                                         ActionCategory.REMOVE, ActionCategory.RELAY)

TOOL_ACTIONS: Mapping[str, ActionKind | Mapping[str, ActionKind]] = {
    "looper_task_create": ActionKind(CREATE, "任务", "新建", "task"),
    "looper_task_control": {
        "start": ActionKind(CONTROL, "任务", "开始", "task"),
        "stop": ActionKind(CONTROL, "任务", "停止", "task"),
        "restart": ActionKind(CONTROL, "任务", "重新开始", "task"),
        "start_over": ActionKind(CONTROL, "任务", "从头开始", "task"),
        "cancel": ActionKind(CONTROL, "任务", "取消", "task"),
    },
    "looper_task_instruct": ActionKind(RELAY, "任务", "补充说明", "task"),
    "looper_task_gate_reply": ActionKind(RELAY, "关卡", "回复", "gate"),
    "looper_task_update": ActionKind(UPDATE, "任务", "修改", "task"),
    "looper_concurrency": ActionKind(UPDATE, "任务并发", "修改", "task"),
    "looper_projects": {
        "create": ActionKind(CREATE, "项目", "新建", "project"),
        "update": ActionKind(UPDATE, "项目", "修改", "project"),
        "move": ActionKind(UPDATE, "项目", "移动", "project"),
        "delete": ActionKind(REMOVE, "项目", "删除", "project"),
    },
    "looper_agent_profiles": {
        "create": ActionKind(CREATE, "Agent Profile", "新建", "profile"),
        "update": ActionKind(UPDATE, "Agent Profile", "修改", "profile"),
        "delete": ActionKind(REMOVE, "Agent Profile", "删除", "profile"),
    },
    "looper_providers": {
        "register": ActionKind(CREATE, "LLM 账号", "注册", "provider"),
        "update": ActionKind(UPDATE, "LLM 账号", "修改", "provider"),
    },
    "looper_models": {
        "create": ActionKind(CREATE, "模型", "新建", "model"),
        "update": ActionKind(UPDATE, "模型", "修改", "model"),
        "delete": ActionKind(REMOVE, "模型", "删除", "model"),
    },
    "looper_catalog": {
        "install": ActionKind(CREATE, "能力", "安装", "install"),
        "update": ActionKind(UPDATE, "安装", "修改", "install"),
        "uninstall": ActionKind(REMOVE, "能力", "卸载", "install"),
    },
    "looper_settings": {
        "set": ActionKind(UPDATE, "设置", "修改", "settings"),
        "allowed_profiles": ActionKind(UPDATE, "可选 Profile", "修改", "settings"),
        "scheduler": ActionKind(UPDATE, "调度设置", "修改", "settings"),
        "waits": ActionKind(UPDATE, "等待设置", "修改", "settings"),
        "knowledge_vault": ActionKind(UPDATE, "知识库设置", "修改", "settings"),
        "llmrec": ActionKind(UPDATE, "记录保留设置", "修改", "settings"),
        "gateway": ActionKind(UPDATE, "网关设置", "修改", "settings"),
    },
    "looper_schedules": {
        "create": ActionKind(CREATE, "定时任务", "新建", "schedule"),
        "update": ActionKind(UPDATE, "定时任务", "修改", "schedule"),
        "enable": ActionKind(CONTROL, "定时任务", "启用", "schedule"),
        "disable": ActionKind(CONTROL, "定时任务", "停用", "schedule"),
        "run": ActionKind(CONTROL, "定时任务", "立即运行", "schedule"),
        "reset_planner_session": ActionKind(CONTROL, "定时任务", "重置规划者会话", "schedule"),
        "delete": ActionKind(REMOVE, "定时任务", "删除", "schedule"),
    },
    "looper_ledger_add": ActionKind(CREATE, "账本条目", "新建", "item"),
    "looper_ledger_update": {
        "amend": ActionKind(UPDATE, "账本条目", "修改", "item"),
        "note": ActionKind(UPDATE, "账本条目", "补充说明", "item"),
        "confirm": ActionKind(CONTROL, "账本条目", "确认", "item"),
        "close": ActionKind(CONTROL, "账本条目", "关闭", "item"),
        "cancel": ActionKind(CONTROL, "账本条目", "取消", "item"),
        "reopen": ActionKind(CONTROL, "账本条目", "重新打开", "item"),
    },
    "looper_reminder_create": ActionKind(CREATE, "提醒", "新建", "reminder"),
    "looper_reminder_cancel": ActionKind(REMOVE, "提醒", "取消", "reminder"),
    "looper_gates": {
        "reply": ActionKind(RELAY, "关卡", "回复", "gate"),
        "end": ActionKind(CONTROL, "关卡讨论", "结束", "gate"),
    },
}
