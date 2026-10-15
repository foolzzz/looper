"""lark-cli as the Lark channel's transport: its commands, the bot it acts as, and contact lookups.

lark-cli chooses the app, and so the bot, through a profile of its own
configuration; the channel's optional ``profile`` is passed as ``--profile`` on
every call, and without one lark-cli's active profile is used. Open_ids are
per app, so the sender allowlist records the app it was made for
(``allowlist_app_id``) and the channel does not start under another one.

Only read calls run here besides the adapter's receive and send: ``whoami``,
the bot's info (``api GET /open-apis/bot/v3/info --as bot``) and
``contact +search-user --as user``; nothing reads lark-cli's configuration
file, which holds the app secret.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# The event the adapter consumes: a message sent to the bot.
RECEIVE_EVENT = "im.message.receive_v1"
PROFILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")
CALL_TIMEOUT_SECONDS = 20.0
IDENTITY_TTL_SECONDS = 30.0
NAMES_TTL_SECONDS = 300.0
SEARCH_MAX = 10
SINGLE_LOOKUPS_MAX = 20


class LarkCliError(RuntimeError):
    """A lark-cli call that failed, with lark-cli's reason."""


def profile_name(raw: Any) -> str:
    """The channel's ``profile``: ``""`` (lark-cli's active one) or a profile name; raises ``ValueError``."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("profile must be a lark-cli profile name, or empty for lark-cli's active profile")
    text = raw.strip()
    if text and not PROFILE_PATTERN.fullmatch(text):
        raise ValueError(f"{text[:80]!r} is not a lark-cli profile name (`lark-cli profile list` lists them)")
    return text


def _base(profile: str | None) -> list[str]:
    return ["lark-cli", *(["--profile", profile] if profile else [])]


def consume_command(profile: str | None = None) -> list[str]:
    """The lark-cli argv that streams the messages the bot receives, one JSON object per line."""
    return [*_base(profile), "event", "consume", RECEIVE_EVENT, "--as", "bot"]


def send_command(*, chat_id: str | None = None, user_id: str | None = None, text: str | None = None,
                 markdown: str | None = None, profile: str | None = None) -> list[str]:
    """The lark-cli argv that sends, as the bot, ``text`` or ``markdown`` (a rich-text post that renders it) to a
    chat (``oc_...``) or a user (an open_id, ``ou_...``): exactly one target and one body."""
    if (chat_id is None) == (user_id is None):
        raise ValueError("send to exactly one of a chat and a user")
    if (text is None) == (markdown is None):
        raise ValueError("send exactly one of text and markdown")
    target = ["--chat-id", chat_id] if chat_id is not None else ["--user-id", user_id]
    body = ["--text", text] if text is not None else ["--markdown", markdown]
    return [*_base(profile), "im", "+messages-send", "--as", "bot", *target, *body]


def edit_command(message_id: str, *, text: str | None = None, markdown: str | None = None,
                 profile: str | None = None) -> list[str]:
    """The lark-cli argv that replaces, as the bot, the content of a message it sent (``om_...``) with ``text`` or
    ``markdown``. Feishu edits one message at most 20 times."""
    if (text is None) == (markdown is None):
        raise ValueError("edit with exactly one of text and markdown")
    body = ["--text", text] if text is not None else ["--markdown", markdown]
    return [*_base(profile), "im", "+messages-edit", "--as", "bot", "--message-id", message_id, *body]


def sent_message_id(answer: Mapping[str, Any]) -> str | None:
    """The id of the message ``+messages-send`` sent, from its JSON answer."""
    data = answer.get("data") if isinstance(answer.get("data"), Mapping) else answer
    message_id = data.get("message_id") if isinstance(data, Mapping) else None
    return str(message_id) if message_id else None


def whoami_command(profile: str | None = None) -> list[str]:
    return [*_base(profile), "whoami"]


def bot_info_command(profile: str | None = None) -> list[str]:
    return [*_base(profile), "api", "GET", "/open-apis/bot/v3/info", "--as", "bot"]


def search_command(*, query: str | None = None, open_ids: Iterable[str] = (), profile: str | None = None
                   ) -> list[str]:
    """``contact +search-user --as user`` by keyword (a name or an email) or by open_ids (at most 100)."""
    argv = [*_base(profile), "contact", "+search-user", "--as", "user"]
    if query is not None:
        argv += ["--query", query, "--page-size", str(SEARCH_MAX)]
    ids = list(open_ids)
    if ids:
        argv += ["--user-ids", ",".join(ids)]
    return argv


def failure(stderr: bytes | str) -> str:
    """lark-cli's reason for a failed call: its JSON error's message, else the tail of what it printed."""
    text = (stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr).strip()
    try:
        message = (json.loads(text).get("error") or {}).get("message")
    except (ValueError, AttributeError):
        message = None
    return str(message) if message else text[-300:]


async def run(argv: list[str], *, timeout: float = CALL_TIMEOUT_SECONDS) -> bytes:
    """Run a lark-cli call; its stdout, or ``LarkCliError`` with lark-cli's reason."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise LarkCliError(f"lark-cli could not start: {exc}") from None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise LarkCliError(f"lark-cli did not answer within {timeout:g} s") from None
    if process.returncode != 0:
        raise LarkCliError(f"lark-cli exited {process.returncode}: {failure(stderr or b'')}")
    return stdout


async def run_json(argv: list[str]) -> dict[str, Any]:
    out = await run(argv)
    try:
        data = json.loads(out)
    except ValueError:
        raise LarkCliError(f"lark-cli printed something that is not JSON: {out[:200]!r}") from None
    if not isinstance(data, dict):
        raise LarkCliError("lark-cli printed JSON that is not an object")
    if data.get("ok") is False:
        raise LarkCliError(str((data.get("error") or {}).get("message") or "lark-cli answered ok: false"))
    return data


# --- The bot it acts as ---


@dataclass(frozen=True)
class Identity:
    """The app and bot a profile acts as."""

    profile: str  # the profile lark-cli used (the active one when the channel names none)
    app_id: str
    bot_name: str
    bot_open_id: str


_identities: dict[str, tuple[float, Identity | LarkCliError]] = {}


async def identity(profile: str | None = None, *, fresh: bool = False) -> Identity:
    """The app and bot ``profile`` acts as, from ``whoami`` and the bot's info; ``LarkCliError`` when lark-cli is
    missing, the profile is unknown, or it is not signed in. Kept ``IDENTITY_TTL_SECONDS`` unless ``fresh``."""
    key = profile or ""
    cached = _identities.get(key)
    if not fresh and cached is not None and time.monotonic() - cached[0] < IDENTITY_TTL_SECONDS:
        if isinstance(cached[1], LarkCliError):
            raise cached[1]
        return cached[1]
    try:
        found = await _identity(profile)
    except LarkCliError as exc:
        _identities[key] = (time.monotonic(), exc)
        raise
    _identities[key] = (time.monotonic(), found)
    return found


async def _identity(profile: str | None) -> Identity:
    me = await run_json(whoami_command(profile))
    if me.get("available") is False or not me.get("appId"):
        raise LarkCliError("lark-cli is not signed in for this profile: run `lark-cli auth login`"
                           + (f" --profile {profile}" if profile else ""))
    bot = (await run_json(bot_info_command(profile))).get("data") or {}
    if not isinstance(bot, Mapping) or not bot.get("open_id"):
        raise LarkCliError("the app has no bot: enable its bot capability in the Lark developer console")
    return Identity(profile=str(me.get("profile") or profile or ""), app_id=str(me["appId"]),
                    bot_name=str(bot.get("app_name") or ""), bot_open_id=str(bot["open_id"]))


# --- Contacts ---


def _people(data: Mapping[str, Any]) -> list[dict[str, str]]:
    users = (data.get("data") or {}).get("users") if isinstance(data.get("data"), Mapping) else None
    people = []
    for user in users or []:
        if not isinstance(user, Mapping) or not str(user.get("open_id") or "").startswith("ou_"):
            continue
        people.append({"open_id": str(user["open_id"]), "name": str(user.get("localized_name") or user.get("name")
                                                                     or ""),
                       "email": str(user.get("enterprise_email") or user.get("email") or ""),
                       "department": str(user.get("department") or "")})
    return people


async def search_people(query: str, profile: str | None = None) -> list[dict[str, str]]:
    """The users ``query`` (a name or an email) finds, as the signed-in user sees them: each ``open_id`` (for
    this app), ``name``, ``email`` and ``department``."""
    return _people(await run_json(search_command(query=query, profile=profile)))[:SEARCH_MAX]


_names: dict[tuple[str, str], tuple[float, str]] = {}


async def names(open_ids: Iterable[str], profile: str | None = None) -> dict[str, str]:
    """The names of ``open_ids`` (each kept ``NAMES_TTL_SECONDS``), looked up in one call for those not known. One
    id lark-cli cannot resolve (another app's, or a bad one) fails the whole call, so then each of the first
    ``SINGLE_LOOKUPS_MAX`` is looked up alone; an id still unresolved is left out."""
    now = time.monotonic()
    key = profile or ""
    wanted = list(dict.fromkeys(open_ids))
    known = {open_id: _names[(key, open_id)][1] for open_id in wanted
             if (key, open_id) in _names and now - _names[(key, open_id)][0] < NAMES_TTL_SECONDS}
    missing = [open_id for open_id in wanted if open_id not in known][:100]
    if not missing:
        return known
    try:
        people = _people(await run_json(search_command(open_ids=missing, profile=profile)))
    except LarkCliError:
        people = []
        for open_id in missing[:SINGLE_LOOKUPS_MAX]:
            try:
                people += _people(await run_json(search_command(open_ids=[open_id], profile=profile)))
            except LarkCliError:
                continue
    for person in people:
        if person["open_id"] in missing and person["name"]:
            _names[(key, person["open_id"])] = (now, person["name"])
            known[person["open_id"]] = person["name"]
    return known


def forget() -> None:
    """Drop what is cached (after a profile change, and in tests)."""
    _identities.clear()
    _names.clear()


# --- The channel's configuration ---

# Keys of the Lark channel's configuration from before lark-cli chose the app; dropped (``lark_config``).
LEGACY_KEYS = ("app_id_env", "app_secret_env")


def lark_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """The Lark channel's configuration as stored: ``profile`` checked, the legacy keys dropped. Raises
    ``ValueError``."""
    stored = {key: value for key, value in config.items() if key not in LEGACY_KEYS}
    stored["profile"] = profile_name(stored.get("profile"))
    return stored


def mismatch(built_for: str | None, identity: Identity) -> str | None:
    """Why an allowlist made for app ``built_for`` cannot be used with ``identity``'s app, if so."""
    if built_for and built_for != identity.app_id:
        return (f"the sender allowlist was made for app {built_for}; profile {identity.profile} is app "
                f"{identity.app_id}; open_ids differ per app, so rebuild the allowlist for this app")
    return None


async def check(config: Mapping[str, Any], *, fresh: bool = False) -> tuple[str | None, Identity | None]:
    """Whether lark-cli can run the channel as configured: (why not, or None; the bot it acts as, when known)."""
    try:
        found = await identity(config.get("profile") or None, fresh=fresh)
    except LarkCliError as exc:
        return f"lark-cli cannot act as the bot: {exc}", None
    return mismatch(config.get("allowlist_app_id"), found), found
