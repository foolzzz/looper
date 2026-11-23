"""Exact operator spend-hold replies bypass the model only in enforce mode."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from functools import wraps
import asyncio
import pytest

from looper.agents import cost_hold_routing as routing, cost_guard_settings as settings
from looper.agents.message import MessageEnvelope, MessageType, channel_address, MANAGER_ADDRESS


def envelope(text, sender='operator', group=False):
    return MessageEnvelope.create(source=channel_address('lark', 'lark-bot'), target=MANAGER_ADDRESS,
        message_type=MessageType.USER_MESSAGE, payload={'text': text, 'channel_context': {
            'sender_id': sender, 'chat_id': 'synthetic-chat', 'chat_type': 'group' if group else 'p2p'}})


def async_test(test):
    @wraps(test)
    def wrapped(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapped


@async_test
async def test_off_and_observe_do_no_routing_io(monkeypatch):
    monkeypatch.setattr(routing.store, 'list_pending', AsyncMock(side_effect=AssertionError('unexpected I/O')))
    for mode in (settings.Mode.OFF, settings.Mode.OBSERVE):
        monkeypatch.setattr(settings, 'cached', lambda: settings.Config(mode=mode))
        assert await routing.route(envelope('继续'), platform=SimpleNamespace()) is None


@async_test
async def test_exact_grammar_and_non_operator_fall_through(monkeypatch):
    monkeypatch.setattr(settings, 'cached', lambda: settings.Config(mode=settings.Mode.ENFORCE))
    monkeypatch.setattr(routing.operator_identity, 'operator_sender_id', AsyncMock(return_value='operator'))
    pending = AsyncMock(return_value=[{'id': 8}])
    monkeypatch.setattr(routing.store, 'list_pending', pending)
    for text in ('请继续', '继续 C-8 4 美元', '停 C-8 now', '继续 C-8', 'stop', '继续 C-8 -1'):
        assert await routing.route(envelope(text), platform=SimpleNamespace()) is None
    assert await routing.route(envelope('停', sender='someone'), platform=SimpleNamespace()) is None
    pending.assert_not_awaited()
    decide = AsyncMock(return_value={'state': 'continued'})
    monkeypatch.setattr(routing.cost_holds, 'decide', decide)
    result = await routing.route(envelope('继续 C-8 4'), platform=SimpleNamespace())
    assert result.action is not None
    assert '继续' in await result.action()
    assert decide.call_args.args[:3] == (8, 'continue', 4.0)


@async_test
async def test_ambiguous_hold_requires_id_and_over_cap_requires_amount(monkeypatch):
    monkeypatch.setattr(settings, 'cached', lambda: settings.Config(mode=settings.Mode.ENFORCE))
    monkeypatch.setattr(routing.operator_identity, 'operator_sender_id', AsyncMock(return_value='operator'))
    pending = AsyncMock(return_value=[{'id': 1}, {'id': 2}])
    monkeypatch.setattr(routing.store, 'list_pending', pending)
    result = await routing.route(envelope('继续'), platform=SimpleNamespace())
    assert 'C-' in result.reply
    pending.return_value = [{'id': 1}]
    monkeypatch.setattr(routing.cost_holds, 'decide', AsyncMock(side_effect=ValueError('请给出新的上限美元金额。')))
    result = await routing.route(envelope('继续'), platform=SimpleNamespace())
    assert '金额' in await result.action()


@pytest.mark.parametrize('mode',[settings.Mode.OFF,settings.Mode.OBSERVE])
def test_disabled_manager_never_calls_spend_reply_router(monkeypatch,mode):
    monkeypatch.setattr(settings,'cached',lambda:settings.Config(mode=mode))
    reply = routing.RouteResult('existing intent reply')
    manager=SimpleNamespace(ops=SimpleNamespace(platform=SimpleNamespace()),_route_intent=AsyncMock(return_value=reply))
    spend=AsyncMock(side_effect=AssertionError('disabled router called'))
    monkeypatch.setattr(routing,'route',spend)
    assert asyncio.run(routing.before_intent(manager,envelope('继续'))) is reply
    spend.assert_not_awaited()
