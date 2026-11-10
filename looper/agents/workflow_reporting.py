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
PROCESS_RECORD_SUMMARY_MAX_CHARS = 1200
SECTIONS = ("Roles", "Reviews", "Verification", "Escalations", "Runs")
REMOTE_SAFETY = 'Remote-environment safety (read-only / dry run) is not enforced by Looper'


def _line(value: Any) -> str:
    return ' '.join(str(value if value is not None else '未记录').split())


def _data(entry: JournalEntry) -> Mapping[str, Any]:
    try:
        value = json.loads(entry.summary)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _verdict(member: str, result: Mapping[str, Any] | None, status: str = '', *, details: bool = True) -> str:
    if result is None:
        return f'{member}: {_line(status) or "未结束"}'
    count = _blocking_count(result)
    passed = result.get('passed') is True and not count
    outcome = '裁决后通过' if result.get('status') == 'passed_by_adjudication' else '通过' if passed else '失败'
    return f'{member}: {outcome}, {count} blocking' + (
        f', {_line(result["failure"])}' if details and result.get('failure') else '')


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
    sections: dict[str, list[str]] = {name: [] for name in SECTIONS}
    roles = sections['Roles']
    for role in (workflow or {}).get('roles') or []:
        roles.append(f'- 配置 {role["key"]}: Profile {_line(role.get("profile_id"))}')
    for role, profile in ((workflow or {}).get('profiles') or {}).items():
        roles.append(f'- 配置 {role}: Profile {_line(profile)}')
    if runs is None:
        roles.append('- 运行记录读取失败；运行时和模型信息不可用。')
    used_roles: dict[str, None] = {}
    summary_roles: dict[str, None] = {}
    summary_reviews: dict[str, str] = {}
    summary_verification: dict[str, str] = {}
    summary_escalations: list[str] = []
    ordinary_runs: dict[tuple[str, str, str], int] = {}
    for run in runs or []:
        metadata = run.get('metadata') or {}
        profile = metadata.get('profile') or metadata.get('agent') or {}
        task_role = (metadata.get('prompt_layers') or {}).get('task_role') or {}
        role = _line(task_role.get('key') or task_role.get('id') or run.get('role'))
        runtime = profile.get('runtime') or {}
        runtime_type = runtime.get('type')
        # Older Cursor runs kept the routed model separately from their Auto display label.
        # Otherwise an explicit null is not a missing key.
        reported = (metadata['routed_model'] if runtime_type == 'cursor-agent' and metadata.get('routed_model') else
                    metadata['model_reported'] if 'model_reported' in metadata else
                    metadata.get('model_display') if runtime_type == 'cursor-agent' else
                    None if runtime_type in ('codex', 'grok', 'pi', 'omp', 'dsh') else run.get('model'))
        details = (f'{role}: Profile {_line(profile.get("name") or profile.get("id"))}; 运行时 '
                   f'{_line(runtime_type or runtime.get("id"))}; 请求模型 '
                   f'{_line((metadata.get("model_choice") or {}).get("model") or profile.get("model"))}; '
                   f'报告模型 {_line(reported)}'
                   + (f'; 回落自 {_line(metadata["fallback_from"])}' if metadata.get('fallback_from') else ''))
        used_roles[f'- {details}'] = None
        summary_roles[f'{role} → {_line(runtime_type or runtime.get("id"))}/{_line(reported)}'
                      + (f'; 回落自 {_line(metadata["fallback_from"])}' if metadata.get('fallback_from') else '')] = None
        if metadata.get('fallback_from'):
            sections['Runs'].append(f'- 运行 {run.get("id", "?")} / {details}')
        else:
            key = (role, _line(reported), details)
            ordinary_runs[key] = ordinary_runs.get(key, 0) + 1
    # Actual use comes before configuration; repeated ordinary runs share one entry.
    roles[:0] = used_roles
    for (role, reported, details), count in ordinary_runs.items():
        sections['Runs'].append(f'- {count} {role} runs on {reported}; {details}')
    reviews, verification, escalations = (sections[name] for name in ('Reviews', 'Verification', 'Escalations'))
    reviewed: set[str] = set()
    rounds: dict[str, int] = {}
    for entry in journal:
        data, label = _data(entry), _line(entry.step)
        if entry.kind is JournalKind.PROFILE_FALLBACK:
            summary_roles[f'{_line(data.get("role"))} 回落: {_line(data.get("from"))} → {_line(data.get("to"))}'] = None
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
                result = seat.get('result') if wrapped else seat
                key = f'{entry.step_id}/{member}'
                summary_reviews.pop(key, None)  # Keep the latest evidence order when a step is revisited.
                summary_reviews[key] = (
                    f'{label} 第 {round_no} 轮 / '
                    + _verdict(member, result, seat.get('status', '') if wrapped else '', details=False))
            if data.get('stale'):
                reviews.append(f'- {label} 第 {round_no} 轮 / stale: {_line(", ".join(data["stale"]))}')
        elif entry.kind is JournalKind.REVIEW_ADJUDICATED:
            reviews.append(f'- {label} 第 {rounds.get(entry.step, 1)} 轮裁决 / {_line(data.get("member"))} finding {data.get("finding")}: '
                           f'{_line(data.get("level"))}; 理由 {_line(data.get("reason"))}')
        elif entry.kind is JournalKind.LATE_REVIEW:
            reviews.append(f'- 晚到 / 第 {rounds.get(entry.step.rsplit("/", 1)[0], 1)} 轮 / {_verdict(label, data)}')
        elif entry.kind is JournalKind.VERIFICATION_ROUND:
            summary_verification.pop(entry.step_id, None)
            summary_verification[entry.step_id] = f'{label} 第 {data.get("attempt", "?")} 轮: {_line(data.get("status"))}'
            verification.append(f'- {label} 第 {data.get("attempt", "?")} 轮: HEAD {_line(data.get("head"))}; '
                                f'结果 {_line(data.get("status"))}; git {_line(data.get("git"))}'
                                + (f'; {_line(data["failure"])}' if data.get('failure') else '')
                                + (f'; {_line(data["summary"])}' if data.get('summary') else ''))
        elif entry.kind is JournalKind.ESCALATION:
            escalations.append(f'- {label} 升级: {_line(data.get("reason"))}; '
                               f'关卡 {_line(", ".join(data.get("gates") or []))}')
            summary_escalations.append(f'{_notice_piece(label, 60)} 升级: {_notice_piece(data.get("reason"), 80)}')
    for step in plan.steps:
        # Seats hold the final verdict after adjudication or a late response.
        if step.reviews and step.result is not None:
            target = plan.step(step.reviews)
            summary_reviews[f'{target.step_id}/{step.label}'] = (
                f'{target.label} 第 {rounds.get(target.label, target.retries + 1)} 轮 / '
                + _verdict(step.label, step.result, details=False))
        if step.step_id not in reviewed:
            evidence = step.result or {}
            if 'verify_passed' in evidence:
                result = {'passed': evidence['verify_passed'], 'findings': evidence.get('verify_findings', [])}
                reviews.append(f'- {step.label} 第 {step.retries + 1} 轮 / {_verdict("builtin", result)}')
                summary_reviews[f'{step.step_id}/builtin'] = reviews[-1]
            for result in (step.result or {}).get('role_reviews') or []:
                reviews.append(f'- {step.label} 第 {step.retries + 1} 轮 / {_verdict(result.get("role", "builtin"), result)}')
                summary_reviews[f'{step.step_id}/{result.get("role", "builtin")}'] = (
                    f'{step.label} 第 {step.retries + 1} 轮 / '
                    + _verdict(result.get("role", "builtin"), result, details=False))
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
            summary_escalations.append(f'{_notice_piece(step.label, 60)} / {_notice_piece(gate.topic, 60)}: '
                                       f'{gate.state.value}; {_notice_piece("; ".join(conclusion) or "尚无结论", 80)}')
    summary = _summary_text({
        "Roles": list(summary_roles) if runs is not None else ['运行记录读取失败；运行时和模型信息不可用'],
        "Reviews": list(summary_reviews.values()), "Verification": list(summary_verification.values()),
        "Escalations": summary_escalations,
    })
    record = {"sections": {name: [] for name in sections}, "remote_safety": REMOTE_SAFETY, "omitted": 0,
              "delivery_summary": summary}
    # Reserve space per section so long run histories cannot crowd out outcomes.
    budget = (PROCESS_RECORD_DATA_MAX_CHARS - len(json.dumps(record, ensure_ascii=False))
              - len(str(sum(map(len, sections.values()))))) // len(SECTIONS)
    for name, items in sections.items():
        remaining = budget
        for item in items:
            size = len(json.dumps(item, ensure_ascii=False)) + 2
            if size <= remaining:
                record["sections"][name].append(item)
                remaining -= size
            else:
                record["omitted"] += 1
    return record


def _fit_entries(items: Iterable[str], budget: int, separator: str) -> tuple[list[str], int]:
    kept, omitted = [], 0
    for item in items:
        size = len(item) + (len(separator) if kept else 0)
        if size <= budget:
            kept.append(item)
            budget -= size
        else:
            omitted += 1
    return kept, omitted


def process_record_text(record: Mapping[str, Any], max_chars: int = PROCESS_RECORD_MAX_CHARS) -> str:
    """A section-budgeted text view; omissions never leave a partial entry."""
    sections = record["sections"]
    safety = record["remote_safety"]
    total = record.get("omitted", 0) + sum(len(items) for items in sections.values())
    parts = ["## Process record", *(f"### {name}" for name in SECTIONS), safety,
             f'已省略 {total} 条记录。']
    # Reserve separators, headings, safety and the largest possible omission count.
    budget = (max_chars - len("\n\n".join(parts)) - len(SECTIONS)) // len(SECTIONS)
    if budget < 0:
        raise ValueError("Process record budget is too small for headings, safety and omitted count")
    parts = ["## Process record"]
    omitted = record.get("omitted", 0)
    for name in SECTIONS:
        kept, dropped = _fit_entries(sections.get(name, []), budget, "\n")
        omitted += dropped
        parts.append(f"### {name}" + ("\n" + "\n".join(kept) if kept else ""))
    parts.extend((safety, f'已省略 {omitted} 条记录。'))
    return "\n\n".join(parts)


def _summary_text(sections: Mapping[str, list[str]]) -> str:
    names = SECTIONS[:-1]
    pointer = "Full record: task page / looper_tasks get"
    total = sum(map(len, sections.values()))
    marker = f'（另 {total} 条，完整记录见下）'
    reserved = len("## Process record\n" + pointer) + sum(len(name) + 3 + len(marker) for name in names)
    budget = (PROCESS_RECORD_SUMMARY_MAX_CHARS - reserved) // len(names)
    lines = ["## Process record"]
    for name in names:
        items = sections.get(name, [])
        if name in ('Reviews', 'Verification'):
            items = reversed(items)
        kept, omitted = _fit_entries(items, budget, "; ")
        value = "; ".join(kept) or ("未记录" if not omitted else "详见完整记录")
        lines.append(f'{name}: {value}' + (f'（另 {omitted} 条，完整记录见下）' if omitted else ''))
    return "\n".join([*lines, pointer])


def process_record_summary(record: Mapping[str, Any]) -> str:
    """Four code-owned delivery lines plus a pointer to the stored evidence."""
    return record["delivery_summary"]


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

