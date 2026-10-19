"""The tenant-token client against an isolated local platform."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from lark_platform_fake import FakeLarkPlatform
from looper.agents.channels import lark_api
from looper.agents.channels.lark_api import BotInfo, LarkApi, LarkApiError
from looper.agents.channels.lark_cli import MESSAGE_GONE_CODES, LarkCliError


@asynccontextmanager
async def platform(**kwargs: Any) -> AsyncIterator[tuple[FakeLarkPlatform, LarkApi]]:
    async with FakeLarkPlatform(**kwargs) as fake:
        api = LarkApi(fake.app_id, "test-secret", base_url=fake.base_url)
        try:
            yield fake, api
        finally:
            await api.aclose()


def contact(open_id: str, name: str = "Ada", *, departments: tuple[str, ...] = ("0",),
            email: str = "ada@example.test", en_name: str = "") -> dict[str, Any]:
    return {"open_id": open_id, "name": name, "en_name": en_name, "email": email,
            "enterprise_email": "", "department_ids": list(departments)}


def test_token_is_cached_and_concurrent_refresh_is_shared() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            assert api.app_id == fake.app_id
            assert api.base_url == fake.base_url
            assert len(set(await asyncio.gather(*(api.tenant_token() for _ in range(20))))) == 1
            assert fake.token_requests == 1
            await asyncio.gather(*(api.bot_info() for _ in range(20)))
            assert fake.token_requests == 1
            fake.invalidate_tokens()
            await api.bot_info()
            assert fake.token_requests == 2
    asyncio.run(run())


@pytest.mark.parametrize(("expire", "before", "after"), [(7200, 6899, 6901), (200, 99, 101),
                                                           (300, 149, 151)])
def test_token_refreshes_using_expiry(expire: int, before: int, after: int) -> None:
    async def run() -> None:
        now = [0.0]
        async with FakeLarkPlatform(expire=expire) as fake:
            api = LarkApi(fake.app_id, "test-secret", base_url=fake.base_url, clock=lambda: now[0])
            try:
                await api.tenant_token()
                now[0] = before
                await api.tenant_token()
                assert fake.token_requests == 1
                now[0] = after
                await asyncio.gather(*(api.tenant_token() for _ in range(20)))
                assert fake.token_requests == 2
            finally:
                await api.aclose()
    asyncio.run(run())


@pytest.mark.parametrize(("code", "status"), [(99991661, 400), (99991663, 400), (42, 401)])
def test_invalid_token_is_retried_once(code: int, status: int) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            await api.tenant_token()
            fake.fail_next("/open-apis/bot", code, "invalid token", status)
            assert (await api.bot_info()).open_id == fake.bot["open_id"]
            assert fake.token_requests == 2
            fake.fail_next("/open-apis/bot", code, "invalid token", status)
            fake.fail_next("/open-apis/bot", code, "still invalid", status)
            with pytest.raises(LarkApiError) as caught:
                await api.bot_info()
            assert caught.value.code == code
            assert caught.value.http_status == status
            assert "still invalid" in str(caught.value)
            assert fake.token_requests == 3
    asyncio.run(run())


def test_wrong_secret_never_leaks(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        async with FakeLarkPlatform() as fake:
            secret = "never-print-this-app-secret"
            api = LarkApi(fake.app_id, secret, base_url=fake.base_url)
            try:
                with pytest.raises(LarkApiError) as caught:
                    await api.tenant_token()
                assert isinstance(caught.value, LarkCliError)
                assert caught.value.code == 10003
                assert "invalid param" in str(caught.value)
                assert secret not in str(caught.value)
                assert secret not in repr(caught.value)
                assert secret not in repr(api)
                assert secret not in caplog.text
            finally:
                await api.aclose()
    asyncio.run(run())


@pytest.mark.parametrize(("target", "body", "receive_type", "msg_type", "content"), [
    ({"chat_id": "oc_chat"}, {"text": "hello 世界"}, "chat_id", "text", {"text": "hello 世界"}),
    ({"user_id": "ou_user"}, {"markdown": "**hello**"}, "open_id", "post",
     {"zh_cn": {"content": [[{"tag": "md", "text": "**hello**"}]]}}),
])
def test_send_targets_and_body(target: dict[str, str], body: dict[str, str], receive_type: str,
                               msg_type: str, content: dict[str, Any]) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            message_id = await api.send(**target, **body)
            assert message_id == fake.sent[0]["message_id"]
            assert fake.sent[0]["msg_type"] == msg_type
            assert fake.sent[0]["content"] == content
            assert fake.sent[0]["chat" if "chat_id" in target else "user"] == next(iter(target.values()))
            assert fake.requests[-1]["query"] == {"receive_id_type": [receive_type]}
    asyncio.run(run())


@pytest.mark.parametrize("kwargs", [{}, {"text": "a", "markdown": "b"}])
@pytest.mark.parametrize("method", ["send", "reply"])
def test_message_requires_exactly_one_body(kwargs: dict[str, str], method: str) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            with pytest.raises(ValueError):
                if method == "send":
                    await api.send(chat_id="oc_chat", **kwargs)
                else:
                    await api.reply("om_parent", **kwargs)
            assert fake.token_requests == 0
    asyncio.run(run())


@pytest.mark.parametrize("target", [{}, {"chat_id": "oc_chat", "user_id": "ou_user"}])
def test_send_requires_exactly_one_target(target: dict[str, str]) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            with pytest.raises(ValueError):
                await api.send(text="hello", **target)
            assert not fake.requests
    asyncio.run(run())


@pytest.mark.parametrize("method", ["send", "reply"])
def test_uuid_deduplicates_messages(method: str) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            async def send() -> str | None:
                if method == "send":
                    return await api.send(chat_id="oc_chat", text="hello", idempotency_key="a" * 50)
                return await api.reply("om_parent", markdown="hello", idempotency_key="a" * 50)
            assert await send() == await send()
            assert len(fake.sent) == 1
            assert fake.sent[0]["uuid"] == "a" * 50
    asyncio.run(run())


@pytest.mark.parametrize("key", ["", "a" * 51])
@pytest.mark.parametrize("method", ["send", "reply"])
def test_uuid_length_is_validated(key: str, method: str) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            with pytest.raises(ValueError):
                if method == "send":
                    await api.send(chat_id="oc_chat", text="hello", idempotency_key=key)
                else:
                    await api.reply("om_parent", text="hello", idempotency_key=key)
            assert not fake.requests
    asyncio.run(run())


def test_reply_edit_react_quote_message_ids() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            parent = "om_parent/with ?#%汉字"
            assert await api.reply(parent, text="reply") == "om_sent_1"
            await api.edit(parent, text="edited")
            await api.react(parent)
            await api.react(parent, "THUMBSUP")
            assert [item["kind"] for item in fake.sent] == ["reply", "edit", "reaction", "reaction"]
            assert all(item["parent"] == parent for item in fake.sent)
            assert fake.sent[0]["content"] == {"text": "reply"}
            assert fake.sent[1]["content"] == {"text": "edited"}
            assert fake.sent[2]["content"] == {"emoji_type": "Get"}
            assert fake.sent[3]["content"] == {"emoji_type": "THUMBSUP"}
            assert "/with" not in fake.requests[-1]["path"]
            fake.recall(parent)
            with pytest.raises(LarkApiError) as caught:
                await api.reply(parent, text="recalled")
            assert caught.value.code == 230011
            assert caught.value.code in MESSAGE_GONE_CODES
            assert caught.value.http_status == 400
    asyncio.run(run())


def test_bot_info_uses_top_level_bot_and_rejects_missing_bot() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            assert await api.bot_info() == BotInfo(fake.bot["app_name"], fake.bot["open_id"])
            fake.bot = {}
            with pytest.raises(LarkApiError, match="enable its bot capability"):
                await api.bot_info()
    asyncio.run(run())


def test_names_batch_cache_and_expiry() -> None:
    async def run() -> None:
        now = [0.0]
        async with FakeLarkPlatform() as fake:
            fake.users = [contact(f"ou_{index}", str(index)) for index in range(101)]
            fake.users[1].update(name="", en_name="English")
            api = LarkApi(fake.app_id, "test-secret", base_url=fake.base_url, clock=lambda: now[0])
            try:
                ids = [item["open_id"] for item in fake.users]
                result = await api.names([*ids, ids[0]])
                assert len(result) == 100
                assert result["ou_1"] == "English"
                calls = [request for request in fake.requests if request["path"].endswith("/batch")]
                assert [len(request["query"]["user_ids"]) for request in calls] == [50, 50]
                assert all(request["query"]["user_id_type"] == ["open_id"] for request in calls)
                assert await api.names(["ou_0", "ou_1"]) == {"ou_0": "0", "ou_1": "English"}
                assert len(fake.requests) == 3
                now[0] = 301
                fake.users[0]["name"] = "Updated"
                assert await api.names(["ou_0"]) == {"ou_0": "Updated"}
                assert len(fake.requests) == 4
            finally:
                await api.aclose()
    asyncio.run(run())


def test_names_falls_back_to_single_ids_with_a_bounded_budget() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            fake.users = [contact("ou_good"), contact("ou_english", "", en_name="English")]
            assert await api.names(["ou_bad", "ou_good", "ou_english"]) == {
                "ou_good": "Ada", "ou_english": "English"}
            batches = [request for request in fake.requests if request["path"].endswith("/batch")]
            assert len(batches) == 4
            assert await api.names([f"ou_missing_{index}" for index in range(25)]) == {}
            batches = [request for request in fake.requests if request["path"].endswith("/batch")]
            assert len(batches) == 25
    asyncio.run(run())


def test_search_email_resolves_name_and_returns_complete() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            fake.users = [contact("ou_ada")]
            assert await api.search_people("ada@example.test") == ([{
                "open_id": "ou_ada", "name": "Ada", "email": "ada@example.test", "department": ""}], True)
            lookup = next(request for request in fake.requests if request["path"].endswith("/batch_get_id"))
            assert lookup["body"] == {"emails": ["ada@example.test"]}
            assert lookup["query"] == {"user_id_type": ["open_id"]}
            assert await api.search_people("unknown@example.test") == ([], True)
    asyncio.run(run())


def test_search_walks_paginated_departments_and_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_api, "SCAN_CALLS_MAX", 100)
    async def run() -> None:
        async with platform() as (fake, api):
            fake.departments = [f"d{index}" for index in range(51)]
            fake.users = [contact(f"ou_root_{index}", "Other", email="other@example.test") for index in range(51)]
            fake.users += [contact("ou_duplicate", "aDA", departments=("0", "d0")),
                           contact("ou_last", "Other", departments=("d50",), en_name="ADA Last"),
                           contact("invalid_id", "Ada", departments=("d50",))]
            people, complete = await api.search_people("AdA")
            assert complete
            assert {person["open_id"] for person in people} == {"ou_duplicate", "ou_last"}
            assert len(people) == 2
            departments = [request for request in fake.requests if request["path"].endswith("/children")]
            assert len(departments) == 2
            scans = [request for request in fake.requests if request["path"].endswith("/find_by_department")]
            assert len([request for request in scans if request["query"]["department_id"] == ["0"]]) == 2
            assert any(request["query"]["department_id"] == ["d50"] for request in scans)
    asyncio.run(run())


@pytest.mark.parametrize("query", ["aDA", "lovELace", "exaMPle"])
def test_search_matches_name_english_name_and_email(query: str) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            fake.users = [contact("ou_ada", "Ada", en_name="Lovelace")]
            people, complete = await api.search_people(query)
            assert complete and [person["open_id"] for person in people] == ["ou_ada"]
    asyncio.run(run())


def test_search_stops_after_ten_matches() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            fake.departments = ["d1"]
            fake.users = [contact(f"ou_{index}") for index in range(20)]
            people, complete = await api.search_people("Ada")
            assert complete and len(people) == 10
            scans = [request for request in fake.requests if request["path"].endswith("/find_by_department")]
            assert len(scans) == 1
    asyncio.run(run())


def test_search_reports_budget_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_api, "SCAN_CALLS_MAX", 2)
    async def run() -> None:
        async with platform() as (fake, api):
            fake.departments = ["d1"]
            fake.users = [contact("ou_root"), contact("ou_dept", departments=("d1",))]
            people, complete = await api.search_people("Ada")
            assert not complete
            assert [person["open_id"] for person in people] == ["ou_root"]
            assert len([request for request in fake.requests if "/contact/" in request["path"]]) == 2
    asyncio.run(run())


@pytest.mark.parametrize("after_people", [False, True])
def test_search_propagates_contact_scope_failure(after_people: bool) -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            fake.departments = ["d1"]
            fake.users = [contact("ou_root")]
            scope = "Access denied: missing contact:user.base:readonly"
            if after_people:
                original = fake._answer
                def answer(method: str, path: str, query: dict[str, list[str]], body: Any,
                           headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
                    if query.get("department_id") == ["d1"]:
                        return 400, {"code": 99991672, "msg": scope}
                    return original(method, path, query, body, headers)
                fake._answer = answer
            else:
                fake.fail_next("/open-apis/contact", 99991672, scope)
            with pytest.raises(LarkApiError, match="contact:user.base:readonly") as caught:
                await api.search_people("Ada")
            assert caught.value.code == 99991672
    asyncio.run(run())


def test_network_failure_has_no_url_or_credentials() -> None:
    async def run() -> None:
        async with platform() as (fake, api):
            await api.tenant_token()
            await fake.stop()
            with pytest.raises(LarkApiError) as caught:
                await api.call("GET", "/open-apis/bot/v3/info", params={"ticket": "private-query"})
            text = str(caught.value)
            assert "could not reach the Lark platform" in text
            assert "ConnectError" in text
            assert "private-query" not in text and fake.base_url not in text
            assert "test-secret" not in text
    asyncio.run(run())


@pytest.mark.parametrize("answer", [httpx.Response(200, text="not JSON"),
                                    httpx.Response(200, json=[]), httpx.Response(200, json={}),
                                    httpx.Response(400, json={"code": 123, "msg": "missing scope: contact:read"})])
def test_call_rejects_invalid_answers(answer: httpx.Response) -> None:
    async def run() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if "/auth/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "mock-token", "expire": 7200})
            assert request.headers["authorization"] == "Bearer mock-token"
            assert request.extensions["timeout"]["read"] == 20
            return answer
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError) as caught:
                await api.call("GET", "/open-apis/bot/v3/info")
            assert caught.value.http_status == answer.status_code
            if answer.status_code == 400:
                assert caught.value.code == 123
                assert "missing scope: contact:read (code 123)" == str(caught.value)
            await api.aclose()
            assert not http.is_closed
    asyncio.run(run())


@pytest.mark.parametrize("answer", [{"code": 0}, {"code": 10003, "msg": "invalid app"}])
def test_token_requires_a_successful_answer_with_a_token(answer: dict[str, Any]) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json=answer))) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError):
                await api.tenant_token()
    asyncio.run(run())


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_errors_are_sanitized(exception: Callable[..., httpx.HTTPError]) -> None:
    async def run() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise exception("private-query and test-secret must not escape")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError) as caught:
                await api.tenant_token()
            assert type(caught.value).__name__ == "LarkApiError"
            assert "private-query" not in str(caught.value)
            assert "test-secret" not in str(caught.value)
            assert exception.__name__ in str(caught.value)
    asyncio.run(run())


def test_stream_endpoint_uses_app_credentials_without_bearer() -> None:
    async def run() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            import json
            assert request.url.path == "/callback/ws/endpoint"
            assert request.method == "POST"
            assert request.headers["locale"] == "zh"
            assert "authorization" not in request.headers
            assert json.loads(request.content) == {"AppID": "cli_test", "AppSecret": "test-secret"}
            return httpx.Response(200, json={"code": 0, "data": {
                "URL": "ws://127.0.0.1:1/ws?device_id=test&service_id=7", "ClientConfig": {"PingInterval": 10}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            url, config = await api.stream_endpoint()
            assert url == "ws://127.0.0.1:1/ws?device_id=test&service_id=7"
            assert config == {"PingInterval": 10}
    asyncio.run(run())


@pytest.mark.parametrize("url", [None, "", "ws://127.0.0.1:1/ws?device_id=test",
                                  "ws://127.0.0.1:1/ws?service_id=7",
                                  "ws://127.0.0.1:1/ws?device_id=test&service_id=oops"])
def test_stream_endpoint_requires_connection_ids(url: str | None) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"code": 0, "data": {"URL": url}}))) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError):
                await api.stream_endpoint()
    asyncio.run(run())


@pytest.mark.parametrize(("status", "code"), [(400, 10003), (403, 99991672), (201, 0)])
def test_stream_endpoint_rejects_http_or_platform_failure(status: int, code: int) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                status, json={"code": code, "msg": "endpoint refused"}))) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError) as caught:
                await api.stream_endpoint()
            assert caught.value.code == code
            assert caught.value.http_status == status
            assert "endpoint refused" in str(caught.value)
    asyncio.run(run())


@pytest.mark.parametrize("expire", [None, 0, -1, True, "7200", float("inf"), float("nan")])
def test_token_rejects_invalid_lifetime(expire: Any) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json={"code": 0, "tenant_access_token": "mock-token", "expire": expire})
                if not isinstance(expire, float) else httpx.Response(
                    200, content=b'{"code":0,"tenant_access_token":"mock-token","expire":' +
                    (b'Infinity' if expire == float("inf") else b'NaN') + b'}'))) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError, match="lifetime"):
                await api.tenant_token()
    asyncio.run(run())


def test_non_json_unauthorized_response_refreshes_once() -> None:
    async def run() -> None:
        token_count = 0
        call_count = 0
        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal token_count, call_count
            if "/auth/" in request.url.path:
                token_count += 1
                return httpx.Response(200, json={"code": 0, "tenant_access_token": f"mock-{token_count}",
                                                 "expire": 7200})
            call_count += 1
            if call_count == 1:
                return httpx.Response(401, text="not JSON")
            return httpx.Response(200, json={"code": 0, "bot": {"app_name": "Bot", "open_id": "ou_bot"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            assert (await api.bot_info()).open_id == "ou_bot"
            assert token_count == call_count == 2
    asyncio.run(run())


def test_reflected_credentials_are_redacted_from_errors(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if "/auth/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "private-token", "expire": 7200})
            return httpx.Response(400, json={"code": 42, "msg": "test-secret private-token rejected"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("cli_test", "test-secret", base_url="http://127.0.0.1", http=http)
            with pytest.raises(LarkApiError) as caught:
                await api.bot_info()
            for value in (str(caught.value), repr(caught.value), repr(api), caplog.text):
                assert "test-secret" not in value
                assert "private-token" not in value
            assert "rejected" in str(caught.value)
    asyncio.run(run())


def test_name_caches_are_isolated_between_apps() -> None:
    async def run() -> None:
        def handle(name: str) -> Callable[[httpx.Request], httpx.Response]:
            def respond(request: httpx.Request) -> httpx.Response:
                if "/auth/" in request.url.path:
                    return httpx.Response(200, json={"code": 0, "tenant_access_token": "mock-token", "expire": 7200})
                return httpx.Response(200, json={"code": 0, "data": {"items": [{"open_id": "ou_user", "name": name}]}})
            return respond
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle("One"))) as first_http:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle("Two"))) as second_http:
                first = LarkApi("first", "secret", base_url="http://127.0.0.1", http=first_http)
                second = LarkApi("second", "secret", base_url="http://127.0.0.1", http=second_http)
                assert await first.names(["ou_user"]) == {"ou_user": "One"}
                assert await second.names(["ou_user"]) == {"ou_user": "Two"}
    asyncio.run(run())


@pytest.mark.parametrize("json_body", [False, True])
def test_second_unauthorized_response_discards_token_and_redacts_error(json_body: bool) -> None:
    async def run() -> None:
        token_count = 0
        call_count = 0

        def handle(request: httpx.Request) -> httpx.Response:
            nonlocal token_count, call_count
            if "/auth/" in request.url.path:
                token_count += 1
                return httpx.Response(200, json={"code": 0, "tenant_access_token": f"private-token-{token_count}",
                                                 "expire": 7200})
            call_count += 1
            if call_count <= 2:
                if json_body:
                    return httpx.Response(401, json={"code": 99991663, "msg": f"private-token-{token_count} rejected"})
                return httpx.Response(401, text="not JSON")
            return httpx.Response(200, json={"code": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("app", "secret", http=http, base_url="http://127.0.0.1")
            with pytest.raises(LarkApiError) as caught:
                await api.call("GET", "/open-apis/bot/v3/info")
            assert caught.value.http_status == 401
            assert "private-token" not in str(caught.value)
            assert token_count == call_count == 2
            assert await api.call("GET", "/open-apis/bot/v3/info") == {"code": 0}
            assert token_count == 3
    asyncio.run(run())


@pytest.mark.parametrize("code", [[], {}])
def test_non_scalar_platform_code_is_an_api_error(code: Any) -> None:
    async def run() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if "/auth/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "mock-token", "expire": 7200})
            return httpx.Response(400, json={"code": code, "msg": "invalid platform code"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("app", "secret", http=http, base_url="http://127.0.0.1")
            with pytest.raises(LarkApiError, match="invalid platform code") as caught:
                await api.call("GET", "/open-apis/bot/v3/info")
            assert caught.value.code is None
    asyncio.run(run())


def test_directory_matches_only_name_english_name_or_email() -> None:
    async def run() -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if "/auth/" in request.url.path:
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "mock-token", "expire": 7200})
            users = [contact("ou_name", "Target"), contact("ou_enterprise", "Other", email="other@example.test")]
            users[1]["enterprise_email"] = "target@example.test"
            return httpx.Response(200, json={"code": 0, "data": {
                "items": [] if request.url.path.endswith("/children") else users, "has_more": False}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = LarkApi("app", "secret", http=http, base_url="http://127.0.0.1")
            people, complete = await api.search_people("target")
            assert complete
            assert [person["open_id"] for person in people] == ["ou_name"]
    asyncio.run(run())
