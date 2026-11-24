"""Eval-only, single-request Messages engine. No runtime, tools, retries or transcript storage."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from looper.agents import processor
from looper.agents.agent_settings import AgentType, resolve
from looper.agents.run_usage import Usage
from looper.agents.runtimes.pricing import BUILTIN_PRICES, ModelPrice


@dataclass(frozen=True)
class MessagesRun(processor.ProcessorRun):
    usage: dict[str, int] = field(default_factory=dict)


class MessagesEngine:
    def __init__(self, base_url: str, model: str, api_key_env: str, *,
                 prices_file: str | None = None, max_output_tokens: int = 4096, fence: bool = False):
        url = urlsplit(base_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("base URL must be HTTP(S), without credentials, query or fragment")
        key = os.environ.get(api_key_env)
        if not key:
            raise ValueError("API key environment variable is empty or unset")
        price = BUILTIN_PRICES.get(model)
        if prices_file:
            try:
                price = ModelPrice(**json.loads(Path(prices_file).read_text())[model])
            except (KeyError, TypeError, ValueError, OSError):
                raise ValueError("prices file must contain a price entry for the selected model") from None
        if (price is None or not price.complete or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                for v in price.view().values())):
            raise ValueError("model needs complete, finite nonnegative USD-per-million-token prices")
        if max_output_tokens < 1 or not model.strip():
            raise ValueError("model and positive max output tokens are required")
        self.url = base_url.rstrip("/") + "/messages"
        self.model, self._key, self.price = model, key, price
        self.max_output_tokens, self.fence = max_output_tokens, fence
        self.halted = False

    async def run(self, *, instructions: str, input_text: str, quiet_word: str, budget_usd: float,
                  timeout_s: float, contract: str | None = None, **_) -> MessagesRun:
        if self.halted:
            return MessagesRun(None, failure=processor.ProcessorFailure.BUDGET,
                               detail="Messages engine stopped; no further requests permitted")
        # Use the default profile's empty custom prompt and the same role/config builder as processor.run.
        config = await processor._role()
        system = processor._system_prompt(resolve(AgentType.PROCESSOR, {}), config,
                                          instructions, quiet_word, contract).text
        user = processor.user_turn(input_text, fence=self.fence)
        # Byte-fallback tokenizers cannot need more text tokens than UTF-8 bytes. Allow 1024 extra framing
        # tokens and reserve the full output cap; use the highest input/cache rate even if caching occurs.
        input_bound = len(system.encode()) + len(user.encode()) + 1024
        input_rate = max(self.price.input_usd_per_mtok, self.price.cache_read_usd_per_mtok,
                         self.price.cache_write_usd_per_mtok, self.price.cache_write_1h_usd_per_mtok or 0)
        reserve = (input_bound * input_rate + self.max_output_tokens * self.price.output_usd_per_mtok) / 1e6
        if reserve > budget_usd:
            self.halted = True
            return MessagesRun(None, cost_usd=0, failure=processor.ProcessorFailure.BUDGET,
                               detail="Messages request reservation exceeds remaining budget")
        payload = {"model": self.model, "max_tokens": self.max_output_tokens, "system": system,
                   "messages": [{"role": "user", "content": user}]}
        failure = processor.ProcessorFailure.FAILED
        detail = "invalid Messages response"
        usage = {}
        cost = None
        try:
            async with asyncio.timeout(timeout_s):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout_s) as client:
                    response = await client.post(self.url, json=payload, headers={
                        "x-api-key": self._key, "anthropic-version": "2023-06-01"})
            if response.status_code != 200:
                detail = f"Messages HTTP {response.status_code} (no retry)"
            elif self._key in response.text:
                detail = "Messages response contained a credential; discarded"
            else:
                data = response.json()
                raw = data["usage"]
                for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    count = raw[name] if name in ("input_tokens", "output_tokens") else raw.get(name, 0)
                    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                        raise ValueError
                    usage[name] = count
                cost = self.price.cost(Usage(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                    cache_read_tokens=usage["cache_read_input_tokens"],
                    cache_creation_tokens=usage["cache_creation_input_tokens"]))
                blocks = data["content"]
                if (data.get("type") != "message" or data.get("stop_reason") != "end_turn"
                        or not isinstance(blocks, list) or not blocks
                        or any(b.get("type") not in ("text", "thinking", "redacted_thinking") for b in blocks)):
                    raise ValueError
                reply = "".join(b["text"] for b in blocks if b["type"] == "text")
                if self._key in reply:  # JSON escapes can hide a credential from the raw-body check.
                    detail = "Messages response contained a credential; discarded"
                    raise ValueError
                if not reply.strip():
                    raise ValueError
                if cost > reserve or cost > budget_usd:
                    detail = "Messages usage exceeded reservation; endpoint token bounds need verification"
                    failure = processor.ProcessorFailure.BUDGET
                else:
                    return MessagesRun(reply, cost_usd=cost, usage=usage)
        except (TimeoutError, httpx.TimeoutException):
            detail, failure = "Messages timeout (no retry)", processor.ProcessorFailure.TIMEOUT
        except httpx.HTTPError:
            detail = "Messages transport failure (no retry)"
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            pass  # Never expose server bodies, headers, exception strings or private input.
        self.halted = True  # Unknown spend or an invalid response must never buy another request.
        return MessagesRun(None, cost_usd=cost, failure=failure, detail=detail, usage=usage)

    @staticmethod
    def facts(done: MessagesRun) -> dict:
        return {"usage": done.usage, "init_tools": 0, "init_mcp_servers": 0, "tool_uses": 0,
                "text_kept": False}
