import asyncio

import httpx


def test_bot_non_json_rate_limit_is_retried():
    from looper.agents.channels.lark_bot import BotTransport
    from looper.agents.channels.lark_transport import GatedLarkTransport
    from looper.agents.channels.outbound import Gateway

    async def check():
        attempts = []
        def respond(request):
            if 'auth/' in request.url.path:
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic', 'expire': 7200})
            attempts.append(request)
            if len(attempts) < 3:
                return httpx.Response(429, text='busy', headers={'Retry-After': '3'})
            return httpx.Response(200, json={'code': 0, 'data': {'message_id': 'om_result'}})
        delays = []
        async def sleep(delay):
            delays.append(delay)
        inner = BotTransport('synthetic-app', 'synthetic-secret')
        await inner._api._http.aclose()
        inner._api._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        transport = GatedLarkTransport(inner, Gateway('channel-lark_bot', sleep=sleep), enabled=lambda: True)
        # Ephemeral status sends use the same gateway as durable messages.
        with transport.message_scope('status', durable=False):
            assert await transport.send(chat_id='oc_synthetic', text='hello', return_id=True) == 'om_result'
        assert len(attempts) == 3
        assert delays == [3, 3]
        await transport.aclose()
    asyncio.run(check())
