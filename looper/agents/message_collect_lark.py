"""Explicitly profiled, read-only Lark user collector.

Command flags were checked using the offline help/reference bundled with
@larksuite/cli 1.0.100. History uses raw items and epoch milliseconds; shortcut
search/listing only discovers containers. Auth's closed local wire contract is
{state,user_open_id,refresh_expires_at}; the installed binary does not document
that envelope offline. Real account/envelope compatibility remains unverified.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .channels import lark_cli
from .message_collect import AuthStatus, Chat, CollectorError, CollectorErrorKind, Page, SearchHit, parse_message


def _base(profile: str) -> list[str]:
    try:
        value = lark_cli.profile_name(profile)
    except ValueError:
        raise CollectorError(CollectorErrorKind.CONFIGURATION) from None
    if not value:
        raise CollectorError(CollectorErrorKind.CONFIGURATION)
    return ["lark-cli", "--profile", value]


def _payload(answer: dict[str, Any]) -> dict[str, Any]:
    data = answer.get("data", answer)
    if not isinstance(data, dict):
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    return data


def _page(data: dict[str, Any], key: str) -> Page:
    items = data.get(key)
    more = data.get("has_more")
    token = data.get("page_token", "")
    if not isinstance(items, list) or type(more) is not bool or not isinstance(token, str):
        raise CollectorError(CollectorErrorKind.PROTOCOL)
    return Page(items, more, token)


async def _read(argv: list[str]) -> dict[str, Any]:
    try:
        answer = await lark_cli.run_json(argv)
    except lark_cli.LarkCliOutputError:
        raise CollectorError(CollectorErrorKind.PROTOCOL) from None
    except lark_cli.LarkCliError as exc:
        if exc.code == 230002:
            kind = CollectorErrorKind.NOT_MEMBER
        elif exc.code == 429 or exc.code in (99991400, 99991429):
            kind = CollectorErrorKind.RATE_LIMIT
        elif exc.error_type in ("timeout", "network") or exc.not_sent:
            kind = CollectorErrorKind.TRANSIENT
        elif exc.error_type in ("auth", "unauthorized"):
            kind = CollectorErrorKind.AUTH_EXPIRED
        elif exc.error_type in ("permission", "forbidden") or exc.code in (99991672, 99991679):
            kind = CollectorErrorKind.PERMISSION
        else:
            kind = CollectorErrorKind.PROTOCOL
        # Never retain lark-cli's error text (it can contain credentials/content).
        raise CollectorError(kind, code=exc.code) from None
    code = answer.get("code", 0)
    if type(code) is not int or code != 0:
        raise CollectorError(
            CollectorErrorKind.NOT_MEMBER if code == 230002 else CollectorErrorKind.PERMISSION,
            code=code if type(code) is int else None,
        )
    return answer


class LarkUserCollector:
    async def auth_status(self, profile: str) -> AuthStatus:
        data = await _read([*_base(profile), "auth", "status", "--json"])
        if set(data) - {"state", "user_open_id", "refresh_expires_at", "ok"} or data.get("state") not in (
            "ready",
            "needs_refresh",
            "expired",
            "missing",
        ):
            raise CollectorError(CollectorErrorKind.PROTOCOL)
        identity = data.get("user_open_id")
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise CollectorError(CollectorErrorKind.PROTOCOL)
        if data["state"] in ("ready", "needs_refresh") and not identity:
            raise CollectorError(CollectorErrorKind.PROTOCOL)
        expiry = data.get("refresh_expires_at")
        try:
            expires = datetime.fromisoformat(expiry.replace("Z", "+00:00")) if isinstance(expiry, str) else None
            if expiry is not None and (expires is None or expires.tzinfo is None):
                raise ValueError
        except ValueError:
            raise CollectorError(CollectorErrorKind.PROTOCOL) from None
        return AuthStatus(data["state"], identity, expires)

    async def search(self, profile: str, start: datetime, end: datetime, page_token: str) -> Page[SearchHit]:
        # Outward rounding preserves millisecond boundaries across hourly slices.
        first = datetime.fromtimestamp(math.floor(start.timestamp()), timezone.utc).isoformat()
        last = datetime.fromtimestamp(math.ceil(end.timestamp()), timezone.utc).isoformat()
        argv = [
            *_base(profile),
            "im",
            "+messages-search",
            "--as",
            "user",
            "--start",
            first,
            "--end",
            last,
            "--no-reactions",
            "--page-size",
            "50",
            "--format",
            "json",
        ]
        if page_token:
            argv += ["--page-token", page_token]
        page = _page(_payload(await _read(argv)), "messages")
        hits = []
        for raw in page.items:
            if not isinstance(raw, dict) or not isinstance(raw.get("chat_id"), str) or not raw["chat_id"]:
                raise CollectorError(CollectorErrorKind.PROTOCOL)
            item = parse_message(raw)
            hits.append(
                SearchHit(item.external_id, raw["chat_id"], item.sent_at, item.thread_id, item.root_id, item.parent_id)
            )
        return Page(hits, page.has_more, page.next_token)

    async def list_messages(
        self,
        profile: str,
        container_kind: str,
        container_id: str,
        start: datetime,
        end: datetime,
        order: str,
        page_token: str,
    ) -> Page[dict[str, Any]]:
        if container_kind not in ("chat", "thread") or order not in ("asc", "desc") or not container_id:
            raise CollectorError(CollectorErrorKind.CONFIGURATION)
        params = {
            "container_id_type": container_kind,
            "container_id": container_id,
            "sort_type": "ByCreateTimeAsc" if order == "asc" else "ByCreateTimeDesc",
            "page_size": 50,
        }
        # The raw thread endpoint does not support server time filters.
        if container_kind == "chat":
            params.update(start_time=str(math.floor(start.timestamp())), end_time=str(math.ceil(end.timestamp())))
        if page_token:
            params["page_token"] = page_token
        answer = await _read(
            [*_base(profile), "api", "GET", "/open-apis/im/v1/messages", "--as", "user", "--params", json.dumps(params)]
        )
        page = _page(_payload(answer), "items")
        for raw in page.items:
            parse_message(raw)
        return page

    async def list_chats(self, profile: str, page_token: str) -> Page[Chat]:
        argv = [
            *_base(profile),
            "im",
            "+chat-list",
            "--as",
            "user",
            "--types",
            "p2p,group",
            "--page-size",
            "100",
            "--format",
            "json",
        ]
        if page_token:
            argv += ["--page-token", page_token]
        page = _page(_payload(await _read(argv)), "chats")
        chats = []
        for raw in page.items:
            if not isinstance(raw, dict) or not isinstance(raw.get("chat_id"), str) or not raw["chat_id"]:
                raise CollectorError(CollectorErrorKind.PROTOCOL)
            kind = raw.get("chat_mode", "unknown")
            if kind not in ("p2p", "group", "topic", "unknown") or not isinstance(raw.get("name", ""), str):
                raise CollectorError(CollectorErrorKind.PROTOCOL)
            chats.append(Chat(raw["chat_id"], kind, raw.get("name", "")))
        return Page(chats, page.has_more, page.next_token)

    async def lookup_people(self, profile: str, open_ids: list[str]) -> dict[str, str]:
        if not 1 <= len(open_ids) <= 30 or any(
            not isinstance(item, str) or not item or "," in item for item in open_ids
        ):
            raise CollectorError(CollectorErrorKind.CONFIGURATION)
        argv = [
            *_base(profile),
            "contact",
            "+search-user",
            "--as",
            "user",
            "--user-ids",
            ",".join(open_ids),
            "--format",
            "json",
        ]
        users = _payload(await _read(argv)).get("users")
        if not isinstance(users, list):
            raise CollectorError(CollectorErrorKind.PROTOCOL)
        names = {}
        for user in users:
            if not isinstance(user, dict) or not isinstance(user.get("open_id"), str):
                raise CollectorError(CollectorErrorKind.PROTOCOL)
            name = user.get("localized_name", user.get("name", ""))
            if not isinstance(name, str):
                raise CollectorError(CollectorErrorKind.PROTOCOL)
            if user["open_id"] in open_ids and name:
                names[user["open_id"]] = name.replace("\x00", "")
        return names
