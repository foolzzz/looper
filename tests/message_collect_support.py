"""In-memory, synthetic user-read backend; never forwards to a platform."""

from looper.agents.message_collect import AuthStatus, Chat, Page, SearchHit, parse_message
from tests import msgflow_support as sup


class Backend:
    def __init__(self, messages=None):
        self.messages = messages or {}
        self.auth = AuthStatus("ready", sup.ME)
        self.calls = []
        self.search_omit = set()
        self.errors = {}

    async def auth_status(self, profile):
        self.calls.append(("auth", profile))
        return self.auth

    async def search(self, profile, start, end, page_token):
        self.calls.append(("search", start, end, page_token))
        hits = []
        for chat, messages in self.messages.items():
            for raw in messages:
                item = parse_message(raw)
                if start <= item.sent_at < end and item.external_id not in self.search_omit:
                    hits.append(
                        SearchHit(item.external_id, chat, item.sent_at, item.thread_id, item.root_id, item.parent_id)
                    )
        return Page(hits)

    async def list_messages(self, profile, kind, container, start, end, order, page_token):
        self.calls.append(("messages", kind, container, start, end, order, page_token))
        if container in self.errors:
            raise self.errors[container]
        if kind == "chat":
            messages = [raw for raw in self.messages.get(container, []) if not raw.get("parent_id")]
        else:
            messages = [raw for rows in self.messages.values() for raw in rows if raw.get("thread_id") == container]
        messages = [raw for raw in messages if start <= parse_message(raw).sent_at < end]
        return Page(sorted(messages, key=lambda raw: (raw["create_time"], raw["message_id"]), reverse=order == "desc"))

    async def list_chats(self, profile, page_token):
        self.calls.append(("listing", page_token))
        return Page([Chat(chat, "group", "Synthetic") for chat in sorted(self.messages)])

    async def lookup_people(self, profile, ids):
        self.calls.append(("names", tuple(ids)))
        return {open_id: "Synthetic" for open_id in ids}
