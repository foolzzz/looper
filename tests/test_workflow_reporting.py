"""Workflow notices and delivery records on the normal task/channel paths."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.channels.telegram import TelegramChannelAdapter
from looper.agents.channels.allowlist import SenderAllowlist
from looper.agents.message import MessageEnvelope, MessageType, TASK_ROUTER_ADDRESS
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import llm_records
from tests.test_planner_agent import FINAL_RESULT, REVIEW_FAIL, REVIEW_PASS, WORK_FAILED, WORK_OK, always, chat, decide, drive, plan, step
from tests.test_review_panel_planner import configure
from looper.agents.workflow_reporting import process_record_text


@pytest.mark.parametrize('long_question', [False, True])
def test_open_gate_notice_reaches_web_thread_with_a_working_reply_number(
    tmp_path, monkeypatch, agent_db, long_question,
):
    import asyncio
    import re
    from looper.agents import gate_commands
    from looper.agents.bus import MessageBus
    from looper.agents.channels.web import WebChannelAdapter
    from looper.agents.tiers.planner import PlannerAgent, PlanStep, TaskPlan
    from looper.agents.user_gates import AgendaItem, Gate
    from looper.db import chat

    monkeypatch.setattr(gate_commands, '_snapshots', {})
    monkeypatch.setattr(gate_commands, '_pending', {})
    saved, posted = AsyncMock(), AsyncMock(return_value={'delivered': 'sent'})
    monkeypatch.setattr(chat, 'save_message', saved)
    monkeypatch.setattr(gate_commands, 'post_message', posted)
    question = '请选择存储方案？' + ('详细要求' * 200 if long_question else '')

    async def scenario():
        bus = MessageBus(runtime_root=tmp_path)
        planner = PlannerAgent(task_id='task', runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        monkeypatch.setattr(gate_commands.db_tasks, 'get_task', AsyncMock(return_value={'manager_id': manager.agent_id}))
        planner._bus = manager._bus = bus
        channel = WebChannelAdapter()
        steps = [PlanStep(f'gate-{n}', n, 'Storage', key=f'g{n}',
                         gate=Gate('Storage', (AgendaItem('q1', question),))) for n in range(2)]
        plan = TaskPlan('plan', 'goal', 'Choose storage', str(tmp_path), steps=steps, status='executing')
        try:
            for gate in steps:
                await planner._open_gate(plan, gate)
                await planner._report_progress(plan)
                queue = bus.get_queue(TASK_ROUTER_ADDRESS.agent_id)
                while not queue.empty():
                    report = queue.get_nowait()
                    if report.payload.get('gate_opened'):
                        await manager._handle_goal_status(MessageEnvelope.create(
                            source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                            message_type=MessageType.GOAL_STATUS, payload=report.payload,
                            metadata={'origin_channel_id': 'channel-telegram'}))
                        assert bus.get_queue('channel-telegram').empty()
                    await manager._handle_goal_status(MessageEnvelope.create(
                        source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                        message_type=MessageType.GOAL_STATUS, payload=report.payload,
                        metadata={'origin_channel_id': 'channel-web', 'origin_thread': 'web-thread'}))
                web = bus.get_queue('channel-web')
                while not web.empty():
                    assert (await channel.deliver(web.get_nowait())).success
            messages = [call.kwargs['content'] for call in saved.await_args_list]
            notices = [text for text in messages if '/gate ' in text]
            assert len(notices) == 2
            assert all(question[:100] in text for text in notices)
            assert all(len(text) < 700 for text in notices)
            assert all(call.kwargs['session_id'] == 'web-thread' for call in saved.await_args_list)
            assert any(text.startswith('进度更新: ') for text in messages)
            if long_question:
                assert all(question not in text and '…' in text for text in notices)
            numbers = [re.search(r'/gate (\d+) ', text)[1] for text in notices]
            assert len(set(numbers)) == 2
            for number, gate in zip(numbers, steps):
                inbound = MessageEnvelope.create(source=channel._address, target=manager.agent_address,
                    message_type=MessageType.USER_MESSAGE,
                    payload={'channel_context': {'session_id': 'web-thread'}})
                await gate_commands.handle(inbound, f'/gate {number} PostgreSQL', has_planner=lambda _: True,
                                           bus=bus, source=manager.agent_address, manager_id=manager.agent_id)
                assert posted.await_args.args[:2] == ('task', gate.label)
        finally:
            await planner.stop()
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_coco_delivery_keeps_the_requested_model_separate_from_the_missing_report():
    from looper.agents.tiers.planner import TaskPlan
    from looper.agents.workflow_reporting import process_record

    record = process_record(TaskPlan("plan", "goal", "task", "."), [], [{"role": "worker", "model": "GPT-5.5", "metadata": {
        "profile": {"name": "Coco", "runtime": {"type": "coco"}, "model": "GPT-5.5"}}}])
    text = process_record_text(record)
    assert "请求模型 GPT-5.5; 报告模型 未记录" in text


@pytest.mark.parametrize("notices", [True, False])
def test_plan_and_review_phase_reach_telegram_transport(planner_claude, agent_db, monkeypatch, notices):
    configure(agent_db, monkeypatch, phase_notices=notices)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"), decide("accept"))
    sent = []

    async def scenario(h):
        await h.start()
        assert (await h.finished()).payload["status"] == "completed"
        manager = ManagerAgent(config=ManagerConfig(work_dir=h.work_dir), runtime_root=h.work_dir)
        manager._bus = h.bus
        channel = TelegramChannelAdapter(bot_token="0:synthetic", allowlist=SenderAllowlist(
            "channel-telegram", frozenset({"7"})))
        channel._correlation_chat["corr-1"] = 7

        async def send_message(**kwargs):
            sent.append(kwargs["text"])
            return SimpleNamespace(message_id=len(sent))

        channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        try:
            for status in h.statuses:
                if status.message_type is MessageType.TASK_ACCEPTED and notices:
                    await manager._handle_task_accepted(MessageEnvelope.create(
                        source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                        message_type=MessageType.TASK_ACCEPTED, payload=status.payload,
                        metadata={"origin_channel_id": "channel-telegram"}))
                    assert h.bus.get_queue("channel-telegram").empty()
                if status.message_type is not MessageType.GOAL_STATUS or status.payload["status"] == "completed":
                    continue
                routed = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                    message_type=MessageType.GOAL_STATUS, payload=status.payload, correlation_id="corr-1",
                    metadata={"origin_channel_id": "channel-telegram"})
                await manager._handle_goal_status(routed)
                await channel.deliver(h.bus.get_queue("channel-telegram").get_nowait())
        finally:
            await manager.stop()

    drive(claude, always(WORK_OK, REVIEW_PASS), scenario)
    if notices:
        assert sum("已拆成" in text and "s1" in text and "review_a" in text for text in sent) == 1
        assert sum("评审第 1 轮" in text and "blocking" in text for text in sent) == 1
    else:
        assert sent and all(text.startswith("进度更新: ") for text in sent)


@pytest.mark.parametrize("failed", [False, True])
def test_workflow_delivery_stores_separate_process_record(planner_claude, agent_db, monkeypatch, failed):
    configure(agent_db, monkeypatch)
    monkeypatch.setattr(llm_records, "process_record_runs", AsyncMock(return_value=[{
        "role": "worker", "model": "reported-model", "metadata": {
            "profile": {"name": "dev", "runtime": {"type": "codex"}},
            "model_choice": {"model": "requested-model"}, "model_reported": "reported-model",
            "fallback_from": "first-profile"}}]), raising=False)
    replies = [plan(step("s1")), decide("fail", reason="Stop") if failed else decide("accept")]
    h = drive(planner_claude(*replies), always(WORK_FAILED if failed else WORK_OK))
    summary = h.final().payload["summary"]
    assert summary.endswith("Full record: task page / looper_tasks get")
    appendix = summary.split("## Process record", 1)[1]
    assert h.final().payload['delivery_summary'] == '## Process record' + appendix
    assert len("## Process record" + appendix) <= 1200
    assert all(text in appendix for text in ("Roles:", "codex", "reported-model", "first-profile",
                                           "Reviews:", "Verification:", "Escalations:"))
    record = process_record_text(h.final().payload["process_record"])
    assert all(text in record for text in ("Roles", "Reviews", "Verification", "Escalations", "dev",
                                            "reported-model", "requested-model", "first-profile"))
    if not failed:
        assert summary.startswith(FINAL_RESULT + "\n\n## Process record\n")


@pytest.mark.parametrize("failed", [False, True])
def test_task_without_workflow_keeps_delivery_bytes(planner_claude, agent_db, monkeypatch, failed):
    records = AsyncMock(side_effect=AssertionError("legacy delivery must not query workflow records"))
    monkeypatch.setattr(llm_records, "process_record_runs", records, raising=False)
    reply = decide("fail", reason="Stop") if failed else decide("accept")
    h = drive(planner_claude(plan(step("s1")), reply), always(WORK_FAILED if failed else WORK_OK))
    expected = ("## Why it ended\nStop\n\n## Done\n- nothing\n\n## Not done\n"
                "- s1 (failed): failed: tests fail") if failed else FINAL_RESULT
    assert h.final().payload["summary"] == expected
    assert 'delivery_summary' not in h.final().payload
    assert not records.called


@pytest.mark.parametrize('status', ['completed', 'failed'])
@pytest.mark.parametrize('long', [False, True])
@pytest.mark.parametrize('workflow', [False, True])
def test_web_completion_keeps_result_clip_and_separate_workflow_summary(tmp_path, agent_db, status, long, workflow):
    import asyncio
    from looper.agents.bus import MessageBus
    from looper.agents.tiers.planner import TaskPlan
    from looper.agents.workflow_reporting import process_record_summary, process_record

    delivery = process_record_summary(process_record(TaskPlan('plan', 'goal', 'Deliver', ''), [], [])) if workflow else ''
    tail = '\n\n## Verification\nChecks passed.\n\n## Open issues\nOne boundary still needs review.'
    body = 'Result ' + 'x' * (5000 - len('Result ') - len(tail)) + tail if long else 'Result'
    appendix = '\n\n' + delivery if workflow else ''

    async def scenario():
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        try:
            await manager._handle_goal_status(MessageEnvelope.create(
                source=TASK_ROUTER_ADDRESS, target=manager.agent_address,
                message_type=MessageType.GOAL_STATUS,
                payload={'status': status, 'summary': body + appendix,
                         **({'delivery_summary': delivery} if workflow else {})},
                metadata={'origin_channel_id': 'channel-web', 'origin_thread': 'web-thread'}))
            return bus.get_queue('channel-web').get_nowait().payload['text']
        finally:
            await manager.stop()
            await bus.shutdown()

    text = asyncio.run(scenario())
    if workflow:
        assert text.endswith(appendix)
    if long:
        result = text.split('：', 1)[1].removesuffix(appendix) if appendix else text.split('：', 1)[1]
        import re
        marker = re.search(r'\n… \((\d+) characters omitted; full result on the task page\)\n', result)
        assert marker is not None
        head, end = result[:marker.start()], result[marker.end():]
        assert body.startswith(head) and body.endswith(end)
        assert len(head) > 0 and len(end) >= 600
        assert int(marker[1]) == len(body) - len(head) - len(end)
        assert len(result) <= 2000 and result.endswith(tail)
    else:
        assert text.endswith(body + appendix)
        assert 'characters omitted' not in text
    assert text.count('## Process record') == int(workflow)


@pytest.mark.parametrize("notices", [True, False])
def test_verification_start_and_result_notices(planner_claude, agent_db, monkeypatch, notices):
    from tests.test_workflow_rules import configure as rules, VERIFY

    rules(agent_db, monkeypatch, phase_notices=notices)
    claude = planner_claude(plan(step("s1"), {**step("v1", "s1"), "role": VERIFY}),
                            decide("accept"), decide("accept"))
    h = drive(claude, always({**WORK_OK, "changed_files": {}, "verification": {"head": "abc123", "git": True}}))
    texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
    assert sum("验证第 1 轮（v1）：开始" in text for text in texts) == int(notices)
    assert sum("验证第 1 轮（v1）：通过" in text for text in texts) == int(notices)
    assert "HEAD abc123" in process_record_text(h.final().payload["process_record"])


def test_exhaustion_emits_one_escalation_notice(planner_claude, agent_db, monkeypatch):
    from tests.test_workflow_rules import configure as rules, GATE

    rules(agent_db, monkeypatch, False, phase_notices=True, max_step_retries=0, escalate_on_exhaustion=True)
    claude = planner_claude(plan(step("s1")),
        decide("replan", steps=[GATE, step("s2", "g1")], reason="Ask before continuing"))

    async def scenario(h):
        await h.start()
        await h.until(lambda: h.planner._plan and h.planner._plan.waiting_gates())

    h = drive(claude, always(WORK_FAILED), scenario)
    texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
    assert sum("s1 已升级" in text and "等你决定" in text for text in texts) == 1


def test_blocked_condition_emits_one_notice(planner_claude, agent_db, monkeypatch):
    from tests.test_workflow_rules import configure as rules, GATE

    rules(agent_db, monkeypatch, False, phase_notices=True, dependency_conditions=True)
    claude = planner_claude(plan(step("s1"), GATE,
        {**step("s2", "g1"), "ready_when": {"step": "g1", "on": "passed"}}),
        decide("accept", gates=[{"id": "g1", "action": "cancel", "resolution": "Source unavailable"}]),
        chat("Discard the blocked step."))
    h = drive(claude, always(WORK_OK))
    texts = [e.payload["phase_text"] for e in h.statuses if "phase_text" in e.payload]
    assert sum("s2 的开始条件无法满足" in text and "g1" in text for text in texts) == 1


def test_workflow_without_panel_preserves_every_review_round(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch, review_panel=None, phase_notices=True)
    claude = planner_claude(plan(step("s1", review=True)), decide("review"),
        decide("retry", feedback="Add the missing tests"), decide("review"), decide("accept"))
    verdicts = iter([REVIEW_FAIL, REVIEW_PASS])
    h = drive(claude, lambda e: next(verdicts) if e.message_type is MessageType.VERIFY_REQUEST else WORK_OK)
    summary = process_record_text(h.final().payload["process_record"])
    assert "s1 第 1 轮 / builtin: 失败, 1 blocking" in summary
    assert "s1 第 2 轮 / builtin: 通过, 0 blocking" in summary


def test_unavailable_runtime_records_are_explicit_without_losing_delivery(planner_claude, agent_db, monkeypatch):
    configure(agent_db, monkeypatch)
    monkeypatch.setattr(llm_records, "process_record_runs", AsyncMock(side_effect=OSError("Unavailable")))
    h = drive(planner_claude(plan(step("s1")), decide("accept")), always(WORK_OK))
    assert h.final().payload["status"] == "completed"
    assert "运行记录读取失败" in process_record_text(h.final().payload["process_record"])


@pytest.mark.parametrize('blocking', [False, True])
@pytest.mark.parametrize('reverse_passes', [False, True])
def test_delivery_keeps_first_seats_late_result_before_earlier_quorum_passes(
    planner_claude, agent_db, monkeypatch, blocking, reverse_passes,
):
    from looper.agents import step_workspaces
    from tests.test_review_panel_planner import BLOCK, MEMBERS, held_reviews

    monkeypatch.setattr(step_workspaces, 'inspect', AsyncMock(return_value=step_workspaces.Unusable(
        'This delivery test uses a plain work directory', repository=False)))
    configure(agent_db, monkeypatch, review_panel={'members': MEMBERS,
              'accept_when': {'k_of': 2, 'of': MEMBERS}})
    label = 'delivery-complete-verification'
    replies = [plan(step(label, review=True)), decide('review'), decide('accept')]
    if blocking:
        replies.append(decide('fail', reason='Late blocker needs repair'))

    async def scenario(h):
        await h.start()
        await h.until(lambda: len(h.held) == 3)
        first = h.held.pop(0)
        if reverse_passes:
            h.held.reverse()
        await h.release(REVIEW_PASS)
        await h.release(REVIEW_PASS)
        await h.until(lambda: h.planner._plan.steps[0].status == 'verified')
        await h.answer(first, BLOCK if blocking else REVIEW_PASS)
        final = await h.finished()
        reviews = final.payload['delivery_summary'].splitlines()[3]
        assert reviews.startswith(rf'- Reviews: {label} 第 1 轮 / {label}/review_a: ')
        assert ('失败, 1 blocking' if blocking else '通过, 0 blocking') in reviews
        latest, earlier = ('b', 'c') if reverse_passes else ('c', 'b')
        assert rf'{label}/review_{latest}: 通过' in reviews and rf'{label}/review_{earlier}:' not in reviews
        assert '另 1 条' in reviews
        assert len(final.payload['delivery_summary']) <= 1200

    drive(planner_claude(*replies), held_reviews, scenario)


def test_telegram_delivery_record_plain_text_fallback_has_no_escaping():
    import asyncio
    from looper.agents.task_notice import NotifyChannel, notice_text
    from tests.test_workflow_process_record import entry, task
    from looper.agents.tiers.journal import JournalKind
    from looper.agents.workflow_reporting import process_record

    fragment = 'write_api `run_cmd` __main__'
    summary = process_record(task(), [entry(JournalKind.ESCALATION, reason=fragment)], [])['delivery_summary']
    text = notice_text('t1', 'Delivery', 'completed', 'Done', channel=NotifyChannel.TELEGRAM,
                       delivery_summary=summary)
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        if 'parse_mode' in kwargs:
            raise ValueError('MarkdownV2 parse failure')
        return SimpleNamespace(message_id=1)

    async def run():
        channel = TelegramChannelAdapter(bot_token='0:synthetic', allowlist=SenderAllowlist(
            'channel-telegram', frozenset({'7'})))
        channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        receipt = await channel.deliver(MessageEnvelope.create(
            source='manager', target='channel-telegram', message_type=MessageType.CHAT_NOTICE,
            payload={'chat': '7', 'text': text}))
        assert receipt.success

    asyncio.run(run())
    assert len(sent) == 2 and 'parse_mode' not in sent[-1]
    assert sent[-1]['text'] == text
    assert fragment in sent[-1]['text'] and '\\' not in sent[-1]['text']
