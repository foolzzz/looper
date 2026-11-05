"""Ask stored API-key accounts for their model names, without registering any.

OpenAI formats share one ``/models`` listing per base URL; Anthropic uses
``/v1/models`` and its cursor and authentication conventions. Login accounts
ship their models with Looper and are skipped. The stored key is never logged,
returned or included in a problem; response bodies and exceptions stay private.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import httpx

from looper.db import profile_registry as db

from .agent_settings import SettingsError, model_name
from .profile_registry import CONTEXT_WINDOW_MAX, RegistryNotFound, provider_spec, unset_price_defaults
from .runtimes import ApiFamily, ApiProtocol, AuthKind

LIST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
LISTING_BUDGET_S = 15
MAX_PAGES = 10
MAX_MODELS = 1000
MAX_BYTES = 2_000_000
ANTHROPIC_VERSION = "2023-06-01"

logger = logging.getLogger(__name__)


class ListingProblem(str, Enum):
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    HTTP_ERROR = "http_error"
    REDIRECT = "redirect"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    BAD_RESPONSE = "bad_response"
    NO_URL = "no_url"
    LOGIN = "login"

    def text(self, status: int | None = None) -> str:
        return _PROBLEM_TEXT[self].format(status=status)


_PROBLEM_TEXT = {
    ListingProblem.UNAUTHORIZED: "the provider refused the request (HTTP {status}): check the provider's API key",
    ListingProblem.NOT_FOUND: "this endpoint has no model list (HTTP {status})",
    ListingProblem.HTTP_ERROR: "the provider answered HTTP {status}",
    ListingProblem.REDIRECT:
        "the provider redirected the request (HTTP {status}): use the final address as the base URL",
    ListingProblem.TIMEOUT: "no answer within 15 seconds",
    ListingProblem.UNREACHABLE: "could not connect to the endpoint",
    ListingProblem.BAD_RESPONSE: "the answer is not a model list",
    ListingProblem.NO_URL: "this endpoint has no URL of its own",
    ListingProblem.LOGIN: "a login provider's models come with Looper; fetching is for API-key providers",
}


@dataclass(frozen=True)
class ListedModel:
    name: str
    context_window: int | None = None


@dataclass(frozen=True)
class Listing:
    models: tuple[ListedModel, ...] = ()
    problem: ListingProblem | None = None
    status: int | None = None
    truncated: bool = False


def _context_window(entry: dict[str, Any]) -> int | None:
    for field in ("context_window", "context_length", "max_context_length", "max_input_tokens"):
        value = entry.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= CONTEXT_WINDOW_MAX:
            return value
    return None


async def list_models_at(family: ApiFamily, base_url: str, api_key: str | None, *,
                         client: httpx.AsyncClient) -> Listing:
    """Read a bounded model list; never follow redirects or expose the response body."""
    anthropic = family is ApiFamily.ANTHROPIC
    root = base_url.rstrip("/")
    if anthropic:
        root = root.removesuffix("/v1")
    url = root + ("/v1/models" if anthropic else "/models")
    headers = {"anthropic-version": ANTHROPIC_VERSION} if anthropic else {}
    if api_key:
        headers["x-api-key" if anthropic else "Authorization"] = api_key if anthropic else f"Bearer {api_key}"
    params: dict[str, str | int] = {"limit": 1000} if anthropic else {}
    models: dict[str, ListedModel] = {}
    bearer_retry = False
    size = 0
    try:
        for page in range(MAX_PAGES):
            while True:
                async with client.stream("GET", url, headers=headers, params=params,
                                         follow_redirects=False) as response:
                    status = response.status_code
                    if status in (401, 403) and anthropic and api_key and not bearer_retry:
                        bearer_retry = True
                        headers.pop("x-api-key", None)
                        headers["Authorization"] = f"Bearer {api_key}"
                        continue
                    problem = (ListingProblem.UNAUTHORIZED if status in (401, 403) else
                               ListingProblem.NOT_FOUND if status in (404, 405) else
                               ListingProblem.REDIRECT if 300 <= status < 400 else
                               ListingProblem.HTTP_ERROR if not 200 <= status < 300 else None)
                    if problem is not None:
                        return Listing(problem=problem, status=status)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            return Listing(problem=ListingProblem.BAD_RESPONSE, status=status)
                        body.extend(chunk)
                break
            try:
                answer = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return Listing(problem=ListingProblem.BAD_RESPONSE, status=status)
            data = answer.get("data") if isinstance(answer, dict) else None
            if not isinstance(data, list) or any(not isinstance(entry, dict) or
                                               not isinstance(entry.get("id"), str) for entry in data):
                return Listing(problem=ListingProblem.BAD_RESPONSE, status=status)
            for entry in data:
                name = entry["id"]
                if name not in models:
                    models[name] = ListedModel(name, _context_window(entry))
                if len(models) >= MAX_MODELS:
                    return Listing(tuple(sorted(models.values(), key=lambda item: item.name.casefold())),
                                   status=status, truncated=True)
            if answer.get("has_more") is not True or not isinstance(answer.get("last_id"), str):
                return Listing(tuple(sorted(models.values(), key=lambda item: item.name.casefold())), status=status,
                               truncated=answer.get("has_more") is True)
            params["after_id" if anthropic else "after"] = answer["last_id"]
        return Listing(tuple(sorted(models.values(), key=lambda item: item.name.casefold())),
                       status=status, truncated=True)
    except httpx.TimeoutException:
        return Listing(problem=ListingProblem.TIMEOUT)
    except httpx.TransportError:
        return Listing(problem=ListingProblem.UNREACHABLE)
    except (httpx.HTTPError, httpx.InvalidURL):
        return Listing(problem=ListingProblem.BAD_RESPONSE)


async def fetch_provider_models(provider_id: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """List each account format/URL once using its saved key, independently of its other endpoints."""
    row = await db.get_provider(provider_id)
    if row is None:
        raise RegistryNotFound("provider not found")
    spec = provider_spec(row)
    if spec.auth is AuthKind.OAUTH:
        protocol = spec.vendor_type.login_protocol
        return {"provider_id": provider_id, "listings": [{
            "family": protocol.family.value, "base_url": "", "protocols": [protocol.value],
            "state": "skipped", "problem": ListingProblem.LOGIN.text(), "truncated": False, "models": [],
        }]}

    api_key = await db.get_api_key(provider_id)
    registered = {(model["name"], model["protocol"]) for model in await db.list_models(provider_id=provider_id)}
    groups: dict[tuple[ApiFamily, str], list[ApiProtocol]] = {}
    for protocol in ApiProtocol:
        for endpoint in row["endpoints"]:
            if endpoint["protocol"] == protocol.value:
                groups.setdefault((protocol.family, endpoint["base_url"]), []).append(protocol)

    async def fetch(family: ApiFamily, base_url: str, protocols: list[ApiProtocol],
                    http: httpx.AsyncClient) -> dict[str, Any]:
        if not base_url:
            listing = Listing(problem=ListingProblem.NO_URL)
        else:
            try:
                async with asyncio.timeout(LISTING_BUDGET_S):
                    listing = await list_models_at(family, base_url, api_key, client=http)
            except TimeoutError:
                listing = Listing(problem=ListingProblem.TIMEOUT)
        logger.info("Provider model listing provider=%s family=%s host=%s result=%s", provider_id, family.value,
                    urlsplit(base_url).netloc, listing.problem.value if listing.problem else len(listing.models))
        models = []
        for model in listing.models:
            problem = None
            try:
                if not model_name(model.name):
                    problem = "a model needs a name"
            except SettingsError as exc:
                problem = str(exc)
            models.append({"name": model.name, "context_window": model.context_window,
                           "registered": [p.value for p in protocols if (model.name, p.value) in registered],
                           "builtin_price": any(value is None for value in
                                                unset_price_defaults(spec, model.name).values()),
                           "addable": problem is None, "problem": problem})
        return {"family": family.value, "base_url": base_url, "protocols": [p.value for p in protocols],
                "state": "skipped" if listing.problem is ListingProblem.NO_URL else
                         "failed" if listing.problem else "listed",
                "problem": listing.problem.text(listing.status) if listing.problem else None,
                "truncated": listing.truncated, "models": models}

    async def fetch_all(http: httpx.AsyncClient) -> dict[str, Any]:
        results = await asyncio.gather(*(fetch(family, url, protocols, http)
                                        for (family, url), protocols in groups.items()), return_exceptions=True)
        listings = []
        for ((family, url), protocols), result in zip(groups.items(), results):
            if isinstance(result, BaseException):
                # Keep another endpoint usable, without exposing exception text or response bodies.
                logger.warning("Provider model listing provider=%s family=%s result=%s error=%s", provider_id,
                               family.value, ListingProblem.BAD_RESPONSE.value, type(result).__name__)
                result = {"family": family.value, "base_url": url, "protocols": [p.value for p in protocols],
                          "state": "failed", "problem": ListingProblem.BAD_RESPONSE.text(),
                          "truncated": False, "models": []}
            listings.append(result)
        return {"provider_id": provider_id, "listings": listings}

    if client is not None:
        return await fetch_all(client)
    async with httpx.AsyncClient(timeout=LIST_TIMEOUT, follow_redirects=False, trust_env=True) as http:
        return await fetch_all(http)
