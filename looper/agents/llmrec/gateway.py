"""Translation launch contracts and the authenticated external gateway boundary."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from . import LlmrecSkip, SkipReason
from .translate import Rejection


def translated(launch) -> bool:
    from ..runtimes import ModelPath, model_path

    resolved = getattr(launch.profile, "resolved_model", None)
    protocols = (resolved.model.protocol,) if resolved is not None else launch.profile.provider.protocols
    return len(protocols) == 1 and model_path(launch.runtime, next(iter(protocols))) is ModelPath.TRANSLATED


def dialect_for(launch) -> str:
    from .dialects import ChatDialect, default_dialect

    from ..runtimes import VendorType

    resolved = getattr(launch.profile, "resolved_model", None)
    model = resolved.model if resolved is not None else launch.profile.provider.catalog_model(launch.model)
    provider = resolved.provider if resolved is not None else launch.profile.provider
    return ChatDialect((model.chat_dialect if model is not None else None)
                       or default_dialect(getattr(provider, "vendor_type", VendorType.CUSTOM))).value


def gateway_refusal(route, model: str = '') -> str:
    prefix = (f"The model {model} speaks OpenAI Chat Completions; Claude Code reaches it only through "
              "Looper's local gateway, but ")
    if isinstance(route, LlmrecSkip):
        if route.reason is SkipReason.PROXY_OUTDATED:
            return prefix + ("the running gateway is an older version kept alive by running agents; "
                             "it is replaced once they finish.")
        if route.reason is SkipReason.CREDENTIALS_IN_URL:
            return prefix + "the upstream URL contains credentials. Move credentials to the provider API key."
        if route.reason is SkipReason.GATEWAY_UNAVAILABLE:
            return prefix + ("the gateway could not start" + (f": {route.detail}" if route.detail else "")
                             + ". Check `looper doctor` → Local gateway.")
    return prefix + "the gateway is not installed in this process."


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class GatewayKey:
    def __init__(self, root):
        self.path = Path(root) / '.local/llmrec/gateway.key'
        self._stamp = None
        self._value = ''

    def exists(self) -> bool:
        return bool(self.reveal())

    def reveal(self) -> str:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
            if stamp != self._stamp:
                self._value = self.path.read_text().strip()
                self._stamp = stamp
            return self._value
        except (OSError, UnicodeError):
            self._stamp, self._value = None, ''
            return ''

    def rotate(self) -> str:
        value = 'lgw_' + secrets.token_urlsafe(32)
        _atomic_write(self.path, value + '\n')
        return value

    def check(self, headers) -> bool:
        expected = self.reveal().encode()
        if not expected:
            return False
        for name, value in _header_pairs(headers):
            if name == 'x-api-key' and hmac.compare_digest(value, expected):
                return True
            if name == 'authorization' and value[:7].lower() == b'bearer ':
                if hmac.compare_digest(value[7:], expected):
                    return True
        return False


class GatewayConfig:
    def __init__(self, root):
        self.path = Path(root) / '.local/llmrec/gateway.json'
        self._stamp, self._value = None, {'format': 1, 'enabled': False, 'record': False}

    def read(self) -> dict:
        try:
            stat = self.path.stat()
            stamp = stat.st_mtime_ns, stat.st_ino, stat.st_size
            if stamp != self._stamp:
                value = json.loads(self.path.read_text())
                if (not isinstance(value, dict) or value.get('format') != 1
                        or type(value.get('enabled')) is not bool or type(value.get('record')) is not bool):
                    raise ValueError('Invalid gateway configuration')
                self._stamp, self._value = stamp, value
            return self._value
        except (OSError, ValueError, UnicodeError):
            self._stamp, self._value = None, {'format': 1, 'enabled': False, 'record': False}
            return self._value

    def write(self, *, enabled: bool, record: bool) -> None:
        _atomic_write(self.path, json.dumps({'format': 1, 'enabled': enabled, 'record': record}))


def _header_pairs(headers):
    for name, value in (headers.items() if isinstance(headers, dict) else headers):
        yield (name.decode('latin-1') if isinstance(name, bytes) else name).lower(), (
            value if isinstance(value, bytes) else value.encode('latin-1'))


def check_route_token(headers, token: str) -> bool:
    if not token:
        return False
    expected = token.encode()
    return any(name == 'authorization' and value[:7].lower() == b'bearer '
               and hmac.compare_digest(value[7:], expected) for name, value in _header_pairs(headers))


async def route_api_key(route, sink) -> str:
    from looper.db import gateway

    try:
        async with asyncio.timeout(3):
            # Recording can listen before its database connects. Translation needs registry credentials.
            if getattr(sink, 'pool', None) is None:
                ready = getattr(sink, 'pool_ready', None)
                if ready is None:
                    raise Unavailable()
                await ready.wait()
            pool = sink.pool
            target = await gateway.resolve_route(route.provider_id, pool=pool)
    except Exception as error:
        raise Unavailable() from error
    if (target is None or not target['enabled'] or target['auth'] != 'api_key' or not target['api_key']
            or target['base_url'] != route.upstream):
        raise Rejection(403, 'permission_error', 'Route provider credentials unavailable or endpoint changed')
    return target['api_key']


def request_guard(headers, port) -> Rejection | None:
    fields = list(_header_pairs(headers))
    if any(name == 'origin' for name, _ in fields):
        return Rejection(403, 'permission_error', 'Browser requests are not allowed through the gateway')
    hosts = [value for name, value in fields if name == 'host']
    if len(hosts) != 1 or hosts[0] not in (f'127.0.0.1:{port}'.encode(), f'localhost:{port}'.encode()):
        return Rejection(403, 'permission_error', 'Invalid gateway host')
    return None


class Unavailable(Exception):
    pass


@dataclass(frozen=True)
class Target:
    base_url: str
    upstream_model: str
    dialect: str
    provider_id: str
    api_key: str


class AliasResolver:
    def __init__(self, pool):
        self.pool = pool
        self._cache = OrderedDict()

    async def resolve(self, alias) -> Target | None:
        from looper.db import gateway

        try:
            gateway.check_alias(alias)
        except ValueError:
            return None
        cached = self._cache.get(alias)
        # Subtracting timestamps can round an elapsed ten seconds below the TTL.
        if cached is not None and time.monotonic() < cached[0] + 10:
            self._cache.move_to_end(alias)
            return cached[1]
        from .dialects import default_dialect
        from ..runtimes import VendorType
        try:
            pool = self.pool() if callable(self.pool) else self.pool
            if pool is None:
                raise Unavailable()
            async with asyncio.timeout(3):
                row = await gateway.resolve_alias(alias, pool=pool)
        except Exception as error:
            raise Unavailable() from error
        target = None
        if row is not None:
            if not row['enabled']:
                raise Rejection(403, 'permission_error', 'provider disabled')
            if row['auth'] != 'api_key' or row['protocol'] != 'openai-chat' or not row['api_key']:
                raise Rejection(403, 'permission_error', 'provider API key unavailable')
            target = Target(row['base_url'], row['upstream_model'],
                row['chat_dialect'] or default_dialect(VendorType(row['vendor_type'])).value, row['provider_id'], row['api_key'])
        self._cache[alias] = time.monotonic(), target
        while len(self._cache) > 1024:
            self._cache.popitem(last=False)
        return target

    async def list_aliases(self):
        from looper.db import gateway
        try:
            pool = self.pool() if callable(self.pool) else self.pool
            if pool is None:
                raise Unavailable()
            async with asyncio.timeout(3):
                return await gateway.list_aliases(pool=pool)
        except Exception as error:
            raise Unavailable() from error
