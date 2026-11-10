"""Deterministic, bounded delivery records from persisted workflow evidence."""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from .tiers import conditions
from .tiers.journal import JournalEntry, JournalKind

if TYPE_CHECKING:
    from .tiers.planner import TaskPlan


PROCESS_RECORD_MAX_CHARS = 6000
PROCESS_RECORD_DATA_MAX_CHARS = 60000
REMOTE_SAFETY = 'Remote-environment safety (read-only / dry run) is not enforced by Looper'


def _line(value: Any) -> str:
    return ' '.join(str(value if value is not None else '未记录').split())


def _data(entry: JournalEntry) -> Mapping[str, Any]:
    try:
        value = json.loads(entry.summary)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _verdict(member: str, result: Mapping[str, Any] | None, status: str = '') -> str:
    if result is None:
        return f'{member}: {_line(status) or "未结束"}'
    count = _blocking_count(result)
    passed = result.get('passed') is True and not count
    outcome = '裁决后通过' if result.get('status') == 'passed_by_adjudication' else '通过' if passed else '失败'
    return f'{member}: {outcome}, {count} blocking' + (
        f', {_line(result["failure"])}' if result.get('failure') else '')


def _blocking_count(result: Mapping[str, Any]) -> int:
    return sum(item.get('severity') == 'blocking' if isinstance(item, Mapping)
               else isinstance(item, str) and result.get('passed') is not True
               for item in result.get('findings') or [])


def process_record(plan: TaskPlan, journal: Iterable[JournalEntry], runs: Iterable[Mapping[str, Any]] | None, *,
                   workflow: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Collect bounded, structured evidence for workflow tasks, including failures.

    ``runs`` contains raw record columns ``id``, ``role``, ``model`` and the selected ``metadata`` fields.
    Model requests and runtime-reported models remain separate, including a missing reported model.
    """
    sections: dict[str, list[str]] = {name: [] for name in ('Roles', 'Reviews', 'Verification', 'Escalations')}
    roles = sections['Roles']
    for role in (workflow or {}).get('roles') or []:
        roles.append(f'- 配置 {role["key"]}: Profile {_line(role.get("profile_id"))}')
    for role, profile in ((workflow or {}).get('profiles') or {}).items():
        roles.append(f'- 配置 {role}: Profile {_line(profile)}')
    if runs is None:
        roles.append('- 运行记录读取失败；运行时和模型信息不可用。')
    for run in runs or []:
        metadata = run.get('metadata') or {}
        profile = metadata.get('profile') or metadata.get('agent') or {}
        task_role = (metadata.get('prompt_layers') or {}).get('task_role') or {}
        role = task_role.get('key') or task_role.get('id') or run.get('role')
        runtime = profile.get('runtime') or {}
        roles.append(f'- 运行 {run.get("id", "?")} / {_line(role)}: Profile '
                     f'{_line(profile.get("name") or profile.get("id"))}; 运行时 '
                     f'{_line(runtime.get("type") or runtime.get("id"))}; 请求模型 '
                     f'{_line((metadata.get("model_choice") or {}).get("model"))}; 报告模型 {_line(run.get("model"))}'
                     + (f'; 回落自 {_line(metadata["fallback_from"])}' if metadata.get('fallback_from') else ''))
    reviews, verification, escalations = (sections[name] for name in ('Reviews', 'Verification', 'Escalations'))
    reviewed: set[str] = set()
    rounds: dict[str, int] = {}
    for entry in journal:
        data, label = _data(entry), _line(entry.step)
        if entry.kind is JournalKind.PROFILE_FALLBACK:
            roles.append(f'- 回落 {_line(data.get("role"))} / {label}: {_line(data.get("from"))} → '
                         f'{_line(data.get("to"))}; 原因 {_line(data.get("reason"))}')
        elif entry.kind is JournalKind.REVIEW_ROUND:
            reviewed.add(entry.step_id)
            round_no = data.get('round', 0) + 1
            rounds[entry.step] = round_no
            for member, seat in (data.get('reviews') or {}).items():
                wrapped = isinstance(seat, Mapping) and 'result' in seat
                verdict = _verdict(member, seat.get('result') if wrapped else seat,
                                   seat.get('status', '') if wrapped else '')
                reviews.append(f'- {label} 第 {round_no} 轮 / {verdict}')
            if data.get('stale'):
                reviews.append(f'- {label} 第 {round_no} 轮 / stale: {_line(", ".join(data["stale"]))}')
        elif entry.kind is JournalKind.REVIEW_ADJUDICATED:
            reviews.append(f'- {label} 第 {rounds.get(entry.step, 1)} 轮裁决 / {_line(data.get("member"))} finding {data.get("finding")}: '
                           f'{_line(data.get("level"))}; 理由 {_line(data.get("reason"))}')
        elif entry.kind is JournalKind.LATE_REVIEW:
            reviews.append(f'- 晚到 / 第 {rounds.get(entry.step.rsplit("/", 1)[0], 1)} 轮 / {_verdict(label, data)}')
        elif entry.kind is JournalKind.VERIFICATION_ROUND:
            verification.append(f'- {label} 第 {data.get("attempt", "?")} 轮: HEAD {_line(data.get("head"))}; '
                                f'结果 {_line(data.get("status"))}; git {_line(data.get("git"))}'
                                + (f'; {_line(data["failure"])}' if data.get('failure') else '')
                                + (f'; {_line(data["summary"])}' if data.get('summary') else ''))
        elif entry.kind is JournalKind.ESCALATION:
            escalations.append(f'- {label} 升级: {_line(data.get("reason"))}; '
                               f'关卡 {_line(", ".join(data.get("gates") or []))}')
    for step in plan.steps:
        if step.step_id not in reviewed:
            evidence = step.result or {}
            if 'verify_passed' in evidence:
                result = {'passed': evidence['verify_passed'], 'findings': evidence.get('verify_findings', [])}
                reviews.append(f'- {step.label} 第 {step.retries + 1} 轮 / {_verdict("builtin", result)}')
            for result in (step.result or {}).get('role_reviews') or []:
                reviews.append(f'- {step.label} 第 {step.retries + 1} 轮 / {_verdict(result.get("role", "builtin"), result)}')
        if step.gate is not None:
            gate = step.gate
            conclusion = []
            if gate.summary:
                for name, title in (('decisions', '决定'), ('assumptions', '假设'), ('open', '待定')):
                    if values := getattr(gate.summary, name):
                        conclusion.append(f'{title}: {_line("; ".join(values))}')
            if gate.resolution:
                conclusion.append(_line(gate.resolution))
            escalations.append(f'- {step.label} / {_line(gate.topic)}: {gate.state.value}; '
                               + ('; '.join(conclusion) or '尚无结论'))
    record = {"sections": {name: [] for name in sections}, "remote_safety": REMOTE_SAFETY, "omitted": 0}
    # Bound the persisted JSON, retaining whole entries and counting every omission.
    budget = (PROCESS_RECORD_DATA_MAX_CHARS - len(json.dumps(record, ensure_ascii=False))
              - len(str(sum(map(len, sections.values())))))
    for name, items in sections.items():
        for item in items:
            size = len(json.dumps(item, ensure_ascii=False)) + 2
            if size <= budget:
                record["sections"][name].append(item)
                budget -= size
            else:
                record["omitted"] += 1
    return record


def process_record_text(record: Mapping[str, Any], max_chars: int = PROCESS_RECORD_MAX_CHARS) -> str:
    """The Manager's compact text view, with an explicit truncation marker."""
    parts = ["## Process record"]
    for name in ("Roles", "Reviews", "Verification", "Escalations"):
        items = record["sections"][name]
        parts.append(f"### {name}" + ("\n" + "\n".join(items) if items else ""))
        if name == "Verification":
            parts[-1] += "\n" + record["remote_safety"]
    if record.get("omitted"):
        parts.append(f'已省略 {record["omitted"]} 条记录。')
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        marker = "…truncated"
        return text[:max_chars - len(marker)].rstrip() + marker
    return text


class Phase(str, Enum):
    PLAN_CREATED = "plan_created"
    REVIEW_ROUND = "review_round"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_ROUND = "verification_round"
    ESCALATION = "escalation"
    CONDITION_BLOCKED = "condition_blocked"


def _notice_piece(value: Any, limit: int = 160) -> str:
    from textwrap import shorten

    return shorten(_line(value or ""), width=limit, placeholder="…",
                   break_long_words=False, break_on_hyphens=False)


def phase_text(event: Phase, data: Mapping[str, Any]) -> str:
    """Operator-facing notices from typed transitions, never model-generated prose."""
    step = _notice_piece(data.get("step", ""), 60)
    attempt = data.get("attempt", 1)
    if event is Phase.PLAN_CREATED:
        workflow = data["workflow"]
        steps = data["steps"]
        breakdown = "；".join(f"{_notice_piece(s.label, 60)} {_notice_piece(s.instruction, 60)}" for s in steps[:12])
        if len(steps) > 12:
            breakdown += f"；另 {len(steps) - 12} 步"
        roles = "、".join(f"{_notice_piece(r['key'], 60)}（{_notice_piece(r['kind'], 60)}）"
                         for r in workflow.get("roles", [])) or "默认角色"
        panel = workflow.get("review_panel") or {}
        required = conditions.leaves(conditions.parse(panel["accept_when"], "accept_when")) if panel else set()
        reviewers = "、".join(f"{_notice_piece(member, 60)}（{'验收成员' if member in required else '建议'}）"
                               for member in panel.get("members", []))
        verification = _notice_piece((workflow.get("verification") or {}).get("role"), 60)
        return (f"已拆成 {len(steps)} 步（{breakdown}）；角色：{roles}"
                + (f"；评审：{reviewers}" if reviewers else "")
                + (f"；验证：{verification}" if verification else "") + "。开始执行。")
    if event is Phase.REVIEW_ROUND:
        counts = []
        for member, review in data.get("reviews", {}).items():
            result = review.get("result", review) or {}
            count = _blocking_count(result)
            counts.append((member, count))
        sources = "，".join(f"{_notice_piece(name, 60)} {count}" for name, count in counts)
        return f"评审第 {attempt} 轮（{step}）：{sum(c for _, c in counts)} 个 blocking（{sources}），交 Planner 决策。"
    if event is Phase.VERIFICATION_STARTED:
        return f"验证第 {attempt} 轮（{step}）：开始。"
    if event is Phase.VERIFICATION_ROUND:
        outcome = "通过" if data.get("status") == "completed" else "失败"
        summary = _notice_piece(data.get("summary"))
        return f"验证第 {attempt} 轮（{step}）：{outcome}" + (f"（{summary}）" if summary else "") + "，交 Planner 决策。"
    if event is Phase.ESCALATION:
        return f"{step} 已升级（{_notice_piece(data.get('reason') or '轮数已用尽')}），等你决定。"
    if event is Phase.CONDITION_BLOCKED:
        return f"{step} 的开始条件无法满足（{_notice_piece(data.get('condition', ''))}），等 Planner 处理。"
    raise ValueError(f"Unknown workflow phase: {event}")

