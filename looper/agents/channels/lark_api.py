"""The Lark open platform's bot messages and contacts, using a tenant token."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from .lark_cli import LarkCliError, MESSAGE_GONE_CODES, sent_message_id

__all__ = ["BASE_URLS", "FEISHU_BASE_URL", "LARK_BASE_URL", "MESSAGE_GONE_CODES", "BotInfo", "LarkApi",
           "LarkApiError"]

FEISHU_BASE_URL = "https://open.feishu.cn"
LARK_BASE_URL = "https://open.larksuite.com"
BASE_URLS = {"feishu": FEISHU_BASE_URL, "lark": LARK_BASE_URL}
TOKEN_INVALID_CODES = frozenset({99991661, 99991663})
SCAN_CALLS_MAX = 40


class LarkApiError(LarkCliError):
    """A platform failure, including its API code and HTTP status when known."""

    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None) -> None:
        super().__init__(f"{message} (code {code})" if code is not None else message, code=code)
        self.http_status = http_status


class BotInfo(NamedTuple):
    app_name: str
    open_id: str


class LarkApi:
    """One app's HTTP client, tenant token and cached contact names."""

    def __init__(self, app_id: str, app_secret: str, *, base_url: str = FEISHU_BASE_URL,
                 http: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._http = http if http is not None else httpx.AsyncClient(trust_env=False)
        self._owns_http = http is None
        self._clock = clock
        self._token: str | None = None
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()
        self._names: dict[str, tuple[float, str]] = {}

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def base_url(self) -> str:
        return self._base_url

    def __repr__(self) -> str:
        return "LarkApi()"

    def _error(self, answer: dict[str, Any], status: int, default: str = "the Lark platform call failed", *,
               token: str | None = None) -> LarkApiError:
        message = str(answer.get("msg") or default)
        for credential in (self._app_secret, self._token, token, answer.get("tenant_access_token")):
            if isinstance(credential, str) and credential:
                message = message.replace(credential, "[redacted]")
        code = answer.get("code")
        return LarkApiError(message, code=code if type(code) is int else None, http_status=status)

    async def _request(self, method: str, path: str, *, headers: dict[str, str] | None = None,
                       params: Any = None, body: Any = None) -> httpx.Response:
        try:
            return await self._http.request(method, self._base_url + path, headers=headers, params=params,
                                            json=body, timeout=20.0)
        except httpx.HTTPError as exc:
            raise LarkApiError(f"could not reach the Lark platform: {type(exc).__name__}") from None

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            answer = response.json()
        except ValueError:
            raise LarkApiError("the Lark platform answered with invalid JSON",
                               http_status=response.status_code) from None
        if not isinstance(answer, dict):
            raise LarkApiError("the Lark platform answered with JSON that is not an object",
                               http_status=response.status_code)
        return answer

    async def tenant_token(self) -> str:
        """Reuse the token until its refresh window; concurrent calls share a refresh."""
        async with self._token_lock:
            if self._token is not None and self._clock() <= self._refresh_at:
                return self._token
            response = await self._request("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                                           body={"app_id": self.app_id, "app_secret": self._app_secret})
            answer = self._json(response)
            token = answer.get("tenant_access_token")
            if answer.get("code") != 0 or response.is_error or not isinstance(token, str) or not token:
                raise self._error(answer, response.status_code, "the Lark platform returned no tenant token")
            expire = answer.get("expire")
            if not isinstance(expire, (int, float)) or isinstance(expire, bool) or not math.isfinite(expire) or expire <= 0:
                raise LarkApiError("the Lark platform returned an invalid token lifetime",
                                   http_status=response.status_code)
            self._token = token
            self._refresh_at = self._clock() + expire - (300 if expire > 300 else expire / 2)
            return token

    async def call(self, method: str, path: str, *, params: Any = None, body: Any = None) -> dict[str, Any]:
        """Call an API as the bot, refreshing an invalid token and retrying once."""
        for attempt in range(2):
            token = await self.tenant_token()
            response = await self._request(method, path, params=params, body=body,
                                           headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 401:
                if self._token == token:
                    self._token = None
                if attempt == 0:
                    continue
                raise self._error(self._json(response), response.status_code, token=token)
            answer = self._json(response)
            if type(answer.get("code")) is int and answer["code"] in TOKEN_INVALID_CODES:
                error = self._error(answer, response.status_code, token=token)
                if self._token == token:
                    self._token = None
                if attempt == 0:
                    continue
                raise error
            if answer.get("code") != 0 or response.is_error:
                raise self._error(answer, response.status_code, token=token)
            return answer
        raise AssertionError("unreachable token retry")

    @staticmethod
    def _message(text: str | None, markdown: str | None, key: str | None = None) -> dict[str, Any]:
        if (text is None) == (markdown is None):
            raise ValueError("send exactly one of text and markdown")
        if key is not None and not 0 < len(key) <= 50:
            raise ValueError("an idempotency key has 1 to 50 characters")
        content = {"text": text} if text is not None else {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}}
        body = {"msg_type": "text" if text is not None else "post", "content": json.dumps(content)}
        if key is not None:
            body["uuid"] = key
        return body

    async def send(self, *, chat_id: str | None = None, user_id: str | None = None, text: str | None = None,
                   markdown: str | None = None, idempotency_key: str | None = None) -> str | None:
        if (chat_id is None) == (user_id is None):
            raise ValueError("send to exactly one of a chat and a user")
        body = self._message(text, markdown, idempotency_key)
        body["receive_id"] = chat_id if chat_id is not None else user_id
        return sent_message_id(await self.call("POST", "/open-apis/im/v1/messages",
                                               params={"receive_id_type": "chat_id" if chat_id is not None else "open_id"},
                                               body=body))

    async def reply(self, message_id: str, *, text: str | None = None, markdown: str | None = None,
                    idempotency_key: str | None = None) -> str | None:
        return sent_message_id(await self.call("POST", f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reply",
                                               body=self._message(text, markdown, idempotency_key)))

    async def edit(self, message_id: str, *, text: str) -> None:
        await self.call("PUT", f"/open-apis/im/v1/messages/{quote(message_id, safe='')}", body=self._message(text, None))

    async def react(self, message_id: str, emoji: str = "Get") -> None:
        await self.call("POST", f"/open-apis/im/v1/messages/{quote(message_id, safe='')}/reactions",
                        body={"reaction_type": {"emoji_type": emoji}})

    async def bot_info(self) -> BotInfo:
        bot = (await self.call("GET", "/open-apis/bot/v3/info")).get("bot")
        if not isinstance(bot, dict) or not bot.get("open_id"):
            raise LarkApiError("the app has no bot: enable its bot capability in the Lark developer console")
        return BotInfo(str(bot.get("app_name") or ""), str(bot["open_id"]))

    async def _lookup_names(self, ids: list[str]) -> list[dict[str, Any]]:
        answer = await self.call("GET", "/open-apis/contact/v3/users/batch",
                                 params=[("user_id_type", "open_id"), *(("user_ids", item) for item in ids)])
        return (answer.get("data") or {}).get("items") or []

    async def names(self, open_ids: Iterable[str]) -> dict[str, str]:
        """Resolve up to 100 missing ids, keeping successful names for five minutes."""
        wanted = list(dict.fromkeys(open_ids))
        now = self._clock()
        known = {item: self._names[item][1] for item in wanted
                 if item in self._names and now - self._names[item][0] < 300}
        missing = [item for item in wanted if item not in known][:100]
        for start in range(0, len(missing), 50):
            chunk = missing[start:start + 50]
            try:
                users = await self._lookup_names(chunk)
            except LarkApiError:
                users = []
                for item in chunk[:20]:
                    try:
                        users += await self._lookup_names([item])
                    except LarkApiError:
                        continue
            for user in users:
                open_id, name = user.get("open_id"), user.get("name") or user.get("en_name")
                if open_id in chunk and name:
                    known[open_id] = str(name)
                    self._names[open_id] = (self._clock(), str(name))
        return known

    async def search_people(self, query: str) -> tuple[list[dict[str, str]], bool]:
        """Find an email exactly, or scan the visible directory within a fixed call budget."""
        if "@" in query:
            answer = await self.call("POST", "/open-apis/contact/v3/users/batch_get_id",
                                     params={"user_id_type": "open_id"}, body={"emails": [query]})
            users = (answer.get("data") or {}).get("user_list") or []
            ids = list(dict.fromkeys(user["user_id"] for user in users if str(user.get("user_id", "")).startswith("ou_")))
            names = await self.names(ids)
            by_id = {user.get("user_id"): user for user in users}
            return [{"open_id": item, "name": names.get(item, ""), "email": str(by_id[item].get("email") or query),
                     "department": ""} for item in ids], True
        calls = 0
        departments = ["0"]
        token = ""
        while True:
            if calls >= SCAN_CALLS_MAX:
                return [], False
            data = (await self.call("GET", "/open-apis/contact/v3/departments/0/children",
                                   params={"fetch_child": "true", "department_id_type": "open_department_id",
                                           "page_size": 50, "page_token": token})).get("data") or {}
            calls += 1
            departments += [str(item["open_department_id"]) for item in data.get("items", [])
                            if item.get("open_department_id")]
            if not data.get("has_more"):
                break
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token == token:
                raise LarkApiError("the Lark directory returned an invalid page token")
            token = next_token
        people: dict[str, dict[str, str]] = {}
        needle = query.casefold()
        for department in dict.fromkeys(departments):
            token = ""
            while True:
                if calls >= SCAN_CALLS_MAX:
                    return list(people.values()), False
                data = (await self.call("GET", "/open-apis/contact/v3/users/find_by_department",
                                       params={"department_id": department, "department_id_type": "open_department_id",
                                               "user_id_type": "open_id", "page_size": 50,
                                               "page_token": token})).get("data") or {}
                calls += 1
                for user in data.get("items", []):
                    open_id = str(user.get("open_id") or "")
                    if not open_id.startswith("ou_") or not any(needle in str(user.get(key) or "").casefold()
                                                               for key in ("name", "en_name", "email")):
                        continue
                    people[open_id] = {"open_id": open_id, "name": str(user.get("name") or user.get("en_name") or ""),
                                       "email": str(user.get("enterprise_email") or user.get("email") or ""), "department": ""}
                    if len(people) == 10:
                        return list(people.values()), True
                if not data.get("has_more"):
                    break
                next_token = data.get("page_token")
                if not isinstance(next_token, str) or not next_token or next_token == token:
                    raise LarkApiError("the Lark directory returned an invalid page token")
                token = next_token
        return list(people.values()), True

    async def stream_endpoint(self) -> tuple[str, dict[str, Any]]:
        """Get a connection ticket without exposing the app secret to the stream client."""
        response = await self._request("POST", "/callback/ws/endpoint", headers={"locale": "zh"},
                                       body={"AppID": self.app_id, "AppSecret": self._app_secret})
        answer = self._json(response)
        if response.status_code != 200 or answer.get("code") != 0:
            raise self._error(answer, response.status_code)
        data = answer.get("data")
        url = data.get("URL") if isinstance(data, dict) else None
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            params = parse_qs(parsed.query) if parsed is not None else {}
            int(params["service_id"][0])
            if parsed is None or parsed.scheme not in ("ws", "wss") or not parsed.netloc or not params["device_id"][0]:
                raise ValueError
        except (ValueError, KeyError):
            raise LarkApiError("the Lark platform returned an invalid stream endpoint",
                               http_status=response.status_code) from None
        config = data.get("ClientConfig")
        return url, config if isinstance(config, dict) else {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()
