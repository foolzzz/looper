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
                   workflow: Mapping[str, Any] | None = None) -> str:
    """Render complete entries only; callers append this section for workflow tasks, including failures.

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
    # Reserve every heading, the safety statement and the exact omission count before selecting entries.
    total = sum(map(len, sections.values()))
    omission = f'已省略 {total} 条记录。'
    fixed = '## Process record\n\n' + '\n\n'.join(f'### {name}' for name in sections)
    budget = PROCESS_RECORD_MAX_CHARS - len(fixed) - len(REMOTE_SAFETY) - len(omission) - 30
    selected: dict[str, list[str]] = {name: [] for name in sections}
    omitted = 0
    for name, items in sections.items():
        for item in items:
            if len(item) + 1 <= budget:
                selected[name].append(item)
                budget -= len(item) + 1
            else:
                omitted += 1
    parts = ['## Process record']
    for name, items in selected.items():
        parts.append(f'### {name}' + ('\n' + '\n'.join(items) if items else ''))
        if name == 'Verification':
            parts[-1] += '\n' + REMOTE_SAFETY
    if omitted:
        parts.append(f'已省略 {omitted} 条记录。')
    return '\n\n'.join(parts)


class Phase(str, Enum):
    PLAN_CREATED = "plan_created"
    REVIEW_ROUND = "review_round"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_ROUND = "verification_round"
    ESCALATION = "escalation"
    CONDITION_BLOCKED = "condition_blocked"


def phase_text(event: Phase, data: Mapping[str, Any]) -> str:
    """Operator-facing notices from typed transitions, never model-generated prose."""
    step = data.get("step", "")
    attempt = data.get("attempt", 1)
    if event is Phase.PLAN_CREATED:
        workflow = data["workflow"]
        steps = data["steps"]
        breakdown = "；".join(f"{s.label} {s.instruction[:60]}" for s in steps[:12])
        if len(steps) > 12:
            breakdown += f"；另 {len(steps) - 12} 步"
        roles = "、".join(f"{r['key']}（{r['kind']}）" for r in workflow.get("roles", [])) or "默认角色"
        panel = workflow.get("review_panel") or {}
        required = conditions.leaves(conditions.parse(panel["accept_when"], "accept_when")) if panel else set()
        reviewers = "、".join(f"{member}（{'验收成员' if member in required else '建议'}）"
                               for member in panel.get("members", []))
        verification = (workflow.get("verification") or {}).get("role")
        return (f"已拆成 {len(steps)} 步（{breakdown}）；角色：{roles}"
                + (f"；评审：{reviewers}" if reviewers else "")
                + (f"；验证：{verification}" if verification else "") + "。开始执行。")
    if event is Phase.REVIEW_ROUND:
        counts = []
        for member, review in data.get("reviews", {}).items():
            result = review.get("result", review) or {}
            count = _blocking_count(result)
            counts.append((member, count))
        sources = "，".join(f"{name} {count}" for name, count in counts)
        return f"评审第 {attempt} 轮（{step}）：{sum(c for _, c in counts)} 个 blocking（{sources}），交 Planner 决策。"
    if event is Phase.VERIFICATION_STARTED:
        return f"验证第 {attempt} 轮（{step}）：开始。"
    if event is Phase.VERIFICATION_ROUND:
        outcome = "通过" if data.get("status") == "completed" else "失败"
        return f"验证第 {attempt} 轮（{step}）：{outcome}，交 Planner 决策。"
    if event is Phase.ESCALATION:
        return f"{step} 已升级（{data.get('reason') or '轮数已用尽'}），等你决定。"
    if event is Phase.CONDITION_BLOCKED:
        return f"{step} 的开始条件无法满足（{data.get('condition', '')}），等 Planner 处理。"
    raise ValueError(f"Unknown workflow phase: {event}")

