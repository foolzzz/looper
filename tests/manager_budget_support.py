"""A resident subprocess stand-in for the Manager's session-budget integration checks."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fake_claude import fake_claude
from looper.agents import profile_registry
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig


@asynccontextmanager
async def manager(pool, root, monkeypatch, *, context=100_001, cost=0.1, compact="success", budget_error=False):
    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    executable = fake_claude(root / "claude", f'''
import json, os, sys
cost = 0
for line in sys.stdin:
    event = json.loads(line)
    if event.get('type') != 'user':
        continue
    text = event['message']['content']
    with open('inputs.jsonl', 'a') as f:
        f.write(json.dumps(text) + '\\n')
    print(json.dumps({{'type':'system', 'subtype':'init', 'session_id':'budget-session', 'model':'sonnet'}}), flush=True)
    if text == '/compact' and {compact!r} == 'raise':
        sys.exit(1)
    if text == '/compact' and {compact!r} in ('success', 'large'):
        print(json.dumps({{'type':'system', 'subtype':'compact_boundary', 'compact_metadata':{{'post_tokens':1200 if {compact!r} == 'success' else 120000}}}}), flush=True)
    answer = 'handoff facts' if text.startswith('[HANDOFF]') else 'answer'
    context = 100 if text == '/compact' or text.startswith('[HANDOFF]') else {context!r}
    usage = {{'input_tokens':context, 'output_tokens':1}}
    if context is None:
        usage = {{}}
    print(json.dumps({{'type':'assistant','message':{{'id':str(os.getpid()) + str(cost), 'model':'sonnet', 'content':[{{'type':'text','text':answer}}], 'usage':usage}}}}), flush=True)
    cost += {cost!r}
    over_cap = {budget_error!r} and text != '/compact'
    print(json.dumps({{'type':'result','subtype':'error_max_budget_usd' if over_cap else 'success', 'is_error':over_cap, 'session_id':'budget-session','result':answer,'total_cost_usd':cost, 'usage':usage}}), flush=True)
''')
    bus = MessageBus(runtime_root=root)
    instance = ManagerAgent(config=ManagerConfig(claude_bin=executable, work_dir=root), runtime_root=root)
    instance._bus = bus

    async def ask(text="hello"):
        await instance._handle_user_message(MessageEnvelope.create(
            source=channel_address("web"), target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
            payload={"text": text, "channel_context": {"session_id": "budget-chat"}},
            metadata={"origin_channel_id": "channel-web"}))
        queue = bus.get_queue("channel-web")
        return [item.payload["text"] for item in [queue.get_nowait() for _ in range(queue.qsize())]
                if item.message_type is MessageType.AGENT_REPLY]

    try:
        yield SimpleNamespace(manager=instance, bus=bus, ask=ask)
    finally:
        await instance.stop()
        await bus.shutdown()
