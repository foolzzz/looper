"""Shared, versioned contract for the independent model recording proxy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

ROUTE_FORMAT = 4
PROXY_ENV = frozenset(('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                       'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'))
TLS_ENV = frozenset(('SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS',
                     'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'))
STATE_DIR = Path('.local') / 'llmrec'
ROUTES_DIR = STATE_DIR / 'routes'
STATE_FILE = STATE_DIR / 'proxy.json'
LOCK_FILE = STATE_DIR / 'proxy.lock'
LOG_FILE = Path('logs') / 'llmrec-proxy.log'
ROUTE_ID = re.compile(r'^[A-Za-z0-9_-]{22}$')
BOOT = 'import sys; sys.path.insert(0, sys.argv[1]); from looper.agents.llmrec.__main__ import main; sys.exit(main(sys.argv[2:]))'


def boot_argv(executable, package_root, runtime_root, port) -> list[str]:
    return [executable, '-I', '-c', BOOT, str(package_root), '--runtime-root', str(runtime_root), '--port', str(port)]


class SkipReason(StrEnum):
    NOT_COVERED = 'not_covered'
    PROXY_UNAVAILABLE = 'proxy_unavailable'
    GATEWAY_UNAVAILABLE = 'gateway_unavailable'
    PROXY_OUTDATED = 'proxy_outdated'
    CREDENTIALS_IN_URL = 'credentials_in_url'


class RouteKind(StrEnum):
    RECORD = 'record'
    TRANSLATE = 'translate'


@dataclass(frozen=True)
class LlmrecRoute:
    id: str
    url: str
    upstream: str
    kind: RouteKind = RouteKind.RECORD
    record: bool = True


@dataclass(frozen=True)
class LlmrecSkip:
    reason: SkipReason
    detail: str = ''


@dataclass(frozen=True)
class RouteFile:
    format: int = ROUTE_FORMAT
    id: str = ''
    upstream: str = ''
    kind: str = 'record'
    record: bool = True
    api: str | None = None
    dialect: str | None = None
    models: dict[str, str] | None = None
    protocol: str | None = None
    runtime_type: str = 'claude-code'
    provider_id: str | None = None
    model: str | None = None
    agent_id: str = ''
    task_id: str | None = None
    step_id: str | None = None
    role: str | None = None
    created_at: str = ''
    record_id: int | None = None
    last_record_id: int | None = None
    ended_at: str | None = None
    proxy_env: dict[str, str] | None = None
    tls_env: dict[str, str] | None = None

    @classmethod
    def read(cls, path, *, allow_legacy=False) -> RouteFile | None:
        try:
            data = json.loads(Path(path).read_text())
            # Keep lifecycle ownership across upgrades without accepting old routes for new requests.
            if (not isinstance(data, dict)
                    or data.get('format') not in ((3, ROUTE_FORMAT) if allow_legacy else (ROUTE_FORMAT,))):
                return None
            route = cls(**data)
            if route.kind not in (RouteKind.RECORD, RouteKind.TRANSLATE) or not isinstance(route.record, bool):
                return None
            if route.kind == RouteKind.TRANSLATE:
                from .dialects import ChatDialect
                if (route.api != 'openai-chat' or not isinstance(route.dialect, str)
                        or route.dialect not in tuple(ChatDialect)
                        or not isinstance(route.models, dict) or not route.models
                        or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                               for key, value in route.models.items())):
                    return None
            elif any(value is not None for value in (route.api, route.dialect, route.models)):
                return None
            for env, allowed in ((route.proxy_env, PROXY_ENV), (route.tls_env, TLS_ENV)):
                if env is not None and (not isinstance(env, dict) or any(
                        key not in allowed or not isinstance(value, str) for key, value in env.items())):
                    return None
            if not isinstance(route.id, str) or not ROUTE_ID.fullmatch(route.id):
                return None
            from urllib.parse import urlsplit
            url = urlsplit(route.upstream)
            if url.scheme not in {'http', 'https'} or not url.hostname or url.username is not None or url.password is not None:
                return None
            if not isinstance(route.agent_id, str) or not route.agent_id:
                return None
            for value in (route.record_id, route.last_record_id):
                if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                    return None
            for value in (route.created_at, route.ended_at):
                if value is not None and (not isinstance(value, str) or not value or datetime.fromisoformat(value).tzinfo is None):
                    return None
            for name in ('runtime_type', 'upstream', 'agent_id'):
                if not isinstance(getattr(route, name), str):
                    return None
            for name in ('protocol', 'provider_id', 'model', 'task_id', 'step_id', 'role'):
                if getattr(route, name) is not None and not isinstance(getattr(route, name), str):
                    return None
            return route
        except (OSError, ValueError, TypeError):
            return None

    def write(self, path):
        data = asdict(self)
        if self.format == 3:
            # The v2.14.99 proxy must still be able to read a kept route after bind/release.
            for field in ('kind', 'record', 'api', 'dialect', 'models'):
                data.pop(field)
        path = Path(path)
        missing = []
        directory = path.parent
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        os.chmod(path.parent, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(data, file, ensure_ascii=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def no_proxy(env: dict[str, str]) -> dict[str, str]:
    """Keep loopback model requests out of inherited HTTP proxies."""
    names = [name for name in ("NO_PROXY", "no_proxy") if name in env] or ["NO_PROXY", "no_proxy"]
    for name in names:
        values = list(dict.fromkeys(value.strip() for value in env.get(name, "").split(",") if value.strip()))
        # A standalone wildcard already includes loopback. Some clients
        # recognize it only as the entire bypass value.
        if values != ["*"]:
            for value in ("127.0.0.1", "localhost", "::1"):
                if value not in values:
                    values.append(value)
        env[name] = ",".join(values)
    return env


def code_sha256() -> str:
    root = Path(__file__).resolve().parents[2]
    files = list((root / 'agents' / 'llmrec').glob('*.py')) + [root / 'db' / 'llm_requests.py', root / 'db' / 'gateway.py']
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: str(p.relative_to(root))):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()
