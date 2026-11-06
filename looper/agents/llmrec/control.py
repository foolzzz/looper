"""Control-plane ownership of the independently running request recorder."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import time
from urllib.parse import parse_qsl, urlsplit

import httpx

import looper
from looper import child_env
from . import (LOG_FILE, PROXY_ENV, TLS_ENV, ROUTES_DIR, ROUTE_FORMAT, ROUTE_ID, STATE_FILE, LlmrecRoute, LlmrecSkip,
               RouteFile, SkipReason, boot_argv, code_sha256)
from .. import proc_tree
from ..keeper import read_json

logger = logging.getLogger(__name__)
_control: ProxyControl | None = None
_SECRET_MARKERS = ("token", "secret", "key", "auth", "cookie")


def install(control: ProxyControl | None) -> None:
    global _control
    _control = control


def controller() -> ProxyControl | None:
    return _control


def config_sha256(code: str, dsn: str) -> str:
    return hashlib.sha256((code + "\0" + dsn).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _bypass(launch, *, agent_id, task_id):
    logger.warning("Model request recording bypassed for %s: %s", agent_id, SkipReason.PROXY_UNAVAILABLE.value)
    if task_id:
        from looper.db import activity

        try:
            await activity.insert_activity(task_id=task_id, worker=agent_id, kind="error",
                summary="Model requests of this run are not recorded: the recording proxy was unavailable.")
        except Exception:
            logger.debug("could not insert request recording bypass activity")
    return replace(launch, llmrec=LlmrecSkip(SkipReason.PROXY_UNAVAILABLE))


class ProxyControl:
    def __init__(self, runtime_root, dsn, *, enabled, include_oauth=False):
        self.runtime_root = Path(runtime_root)
        self.dsn = dsn
        self.enabled = enabled
        self.include_oauth = include_oauth
        self._outdated = False
        self._health_cache = None
        self._failed_health = None
        self._health_at = 0.0
        self._start_lock = asyncio.Lock()
        self._children: dict[int, asyncio.subprocess.Process] = {}

    def _state(self) -> dict | None:
        state = read_json(self.runtime_root / STATE_FILE)
        if (not state or type(state.get("pid")) is not int or state["pid"] <= 0
                or not isinstance(state.get("start_time"), str) or not state["start_time"]
                or type(state.get("port")) is not int or not 0 < state["port"] < 65536
                or not isinstance(state.get("instance"), str)):
            return None
        return state

    async def _running(self, state) -> bool:
        return bool(state and await asyncio.to_thread(proc_tree._start_time, state["pid"]) == state["start_time"])

    async def _health(self, *, cached=False) -> dict | None:
        if cached and time.monotonic() - self._health_at < 5:
            return self._health_cache
        result = None
        state = self._state()
        if self._failed_health and (not state or any(
                self._failed_health.get(key) != state[key] for key in ("instance", "pid"))):
            self._failed_health = None
        if await self._running(state):
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    response = await client.get(f"http://127.0.0.1:{state['port']}/llmrec/health")
                    if response.status_code not in (200, 503):
                        response.raise_for_status()
                    data = response.json()
                    if (isinstance(data, dict) and data.get("instance") == state["instance"]
                            and data.get("pid") == state["pid"]):
                        if response.status_code == 503:
                            self._failed_health = data
                        elif response.status_code == 200:
                            result = data
                            self._failed_health = None
            except (httpx.HTTPError, ValueError):
                pass
        self._health_cache, self._health_at = result, time.monotonic()
        if result is not None:
            self._outdated = result.get("route_format") != ROUTE_FORMAT
        return result

    async def _owns_process(self, state) -> bool:
        current = self._state()
        return bool(current and all(current[key] == state[key] for key in ("instance", "pid", "start_time"))
                    and await self._running(state))

    async def _stop(self, state, *, grace_s=35, preserve_state=False) -> None:
        running = await self._running(state)
        if running and not await self._owns_process(state):
            return
        if running:
            try:
                os.kill(state["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + grace_s
            while await self._running(state) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if await self._running(state):
                # Recheck state and process birth time before escalating: the
                # port or PID may now belong to a different instance.
                if await self._owns_process(state):
                    try:
                        os.kill(state["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + 5
                while await self._running(state) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if await self._running(state):
                    raise TimeoutError("request recording proxy did not stop")
        child = self._children.pop(state["pid"], None)
        if child is not None:
            await child.wait()
        current = self._state()
        if not preserve_state and (current is None or all(
                current[key] == state[key] for key in ("instance", "pid", "start_time"))):
            (self.runtime_root / STATE_FILE).unlink(missing_ok=True)
        self._health_cache, self._health_at = None, 0.0
        if not preserve_state:
            self._failed_health = None

    async def _start(self, *, port=None) -> bool:
        async with self._start_lock:
            if await self._health():
                return True
            previous = self._state()
            port = port if port is not None else previous["port"] if previous else 0
            if previous and self._failed_health and all(
                    self._failed_health.get(key) == previous[key] for key in ("instance", "pid")):
                # Keep the pinned port and failure counters until a replacement
                # publishes its state, including when its first boot fails.
                await self._stop(previous, grace_s=2, preserve_state=True)
                if await self._health():
                    return True
            log = self.runtime_root / LOG_FILE
            log.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                process = await asyncio.create_subprocess_exec(
                    *boot_argv(sys.executable, Path(looper.__file__).resolve().parent.parent, self.runtime_root, port),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=descriptor,
                    # TLS overrides belong to routes; truststore also reads process-level OpenSSL overrides.
                    start_new_session=True, env={k: v for k, v in child_env.inherited().items() if k not in TLS_ENV})
            finally:
                os.close(descriptor)
            self._children[process.pid] = process
            try:
                process.stdin.write((self.dsn + "\n").encode())
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = self._state()
                if state and state["pid"] == process.pid and await self._health():
                    return True
                if process.returncode is not None:
                    self._children.pop(process.pid, None)
                    # A concurrent controller may have won the lock.
                    return bool(await self._health())
                await asyncio.sleep(0.05)
            return False

    async def at_start(self, kept_record_ids) -> None:
        kept = set(kept_record_ids)
        state = self._state()
        health = await self._health()
        if health:
            current = code_sha256()
            mismatch = (state.get("code_sha256") != current
                        or state.get("config_sha256") != config_sha256(current, self.dsn))
            if not kept and (not self.enabled or mismatch):
                port = state["port"]
                await self._stop(state)
                if self.enabled:
                    # Preserve the port through replacement without a stale process identity.
                    await self._start(port=port)
            self._outdated = bool(kept and health.get("route_format") != ROUTE_FORMAT)
        elif self.enabled:
            await self._start()
        for path in (self.runtime_root / ROUTES_DIR).glob("*.json"):
            route = RouteFile.read(path)
            if route and route.record_id not in kept:
                replace(route, last_record_id=route.record_id if route.record_id is not None else route.last_record_id,
                        record_id=None,
                        ended_at=route.ended_at or _now()).write(path)

    async def tick(self) -> None:
        if (self.runtime_root / STATE_FILE).exists() and not await self._health():
            await self._start()

    async def set_enabled(self, enabled, *, include_oauth=None) -> None:
        self.enabled = enabled
        if include_oauth is not None:
            self.include_oauth = include_oauth
        if enabled:
            try:
                await self._start()
            except (OSError, ValueError):
                logger.warning("could not start request recording proxy")

    async def status(self) -> dict:
        health = await self._health()
        state = self._state()
        counters = health or self._failed_health or {}
        return {"enabled": self.enabled, "include_oauth": self.include_oauth, "proxy": {
            "state": "outdated" if health and self._outdated else "running" if health else
                     "unavailable" if self.enabled or state else "stopped",
            "port": state["port"] if state else None, "pid": state["pid"] if state else None,
            **{name: int(counters.get(name, 0)) for name in ("requests", "recorded", "gaps", "dropped", "write_errors")},
            "gap_reasons": counters.get("gap_reasons", {})}}

    async def attach(self, launch, *, agent_id, task_id, step_id=None, reuse=False, cwd=None):
        from ..runtimes import AuthKind

        if not self.enabled:
            return launch
        provider = launch.profile.provider
        runtime_type = launch.profile.runtime.type
        if provider.auth == AuthKind.OAUTH and not (self.include_oauth and runtime_type == "claude-code"):
            return launch
        upstream = launch.runtime.llmrec_upstream(launch)
        if upstream is None:
            return replace(launch, llmrec=LlmrecSkip(SkipReason.NOT_COVERED, runtime_type))
        try:
            url = urlsplit(upstream)
            credentials = url.username is not None or url.password is not None or any(
                any(marker in name.lower() for marker in _SECRET_MARKERS)
                for name, _ in parse_qsl(url.query, keep_blank_values=True))
        except ValueError:
            credentials = True
        if credentials:
            return replace(launch, llmrec=LlmrecSkip(SkipReason.CREDENTIALS_IN_URL))
        if not await self._health(cached=True):
            try:
                ready = await self._start()
            except (OSError, ValueError):
                ready = False
            if not ready:
                return await _bypass(launch, agent_id=agent_id, task_id=task_id)
        if self._outdated:
            return replace(launch, llmrec=LlmrecSkip(SkipReason.PROXY_OUTDATED))
        state = self._state()
        if state is None:
            return await _bypass(launch, agent_id=agent_id, task_id=task_id)
        identifier = secrets.token_urlsafe(16)
        resolved = getattr(launch.profile, "resolved_model", None)
        protocol = resolved.model.protocol.value if resolved else next(iter(provider.protocols)).value
        env = replace(launch, llmrec=None).environment()
        route = RouteFile(format=ROUTE_FORMAT, id=identifier, upstream=upstream, protocol=protocol,
                          runtime_type=runtime_type, provider_id=provider.id, model=launch.model,
                          agent_id=agent_id, task_id=task_id, step_id=step_id,
                          role=getattr(launch.role, "value", launch.role), created_at=_now(),
                          record_id=None, last_record_id=None, ended_at=None,
                          proxy_env={name: value for name, value in env.items() if name in PROXY_ENV},
                          # The proxy has its own cwd; relative trust paths belong
                          # to the agent process that would have loaded them.
                          tls_env={name: os.path.realpath(os.path.join(cwd or os.getcwd(), value)) if value else value
                                   for name, value in env.items() if name in TLS_ENV})
        route.write(self.runtime_root / ROUTES_DIR / f"{identifier}.json")
        port = state["port"]
        return replace(launch, llmrec=LlmrecRoute(identifier, f"http://127.0.0.1:{port}/r/{identifier}", upstream))

    def bind(self, route_id, record_id):
        if not ROUTE_ID.fullmatch(route_id):
            raise ValueError("invalid request recording route")
        path = self.runtime_root / ROUTES_DIR / f"{route_id}.json"
        route = RouteFile.read(path)
        if route is None:
            raise ValueError("request recording route is missing")
        replace(route, record_id=record_id, ended_at=None).write(path)

    def release(self, record_id, *, end_route):
        for path in (self.runtime_root / ROUTES_DIR).glob("*.json"):
            route = RouteFile.read(path)
            if route and route.record_id == record_id:
                replace(route, last_record_id=record_id, record_id=None,
                        ended_at=(_now() if end_route else route.ended_at)).write(path)


async def attach(launch, **kwargs):
    if _control is None:
        return launch
    try:
        return await _control.attach(launch, **kwargs)
    except (OSError, ValueError):
        return await _bypass(launch, agent_id=kwargs["agent_id"], task_id=kwargs["task_id"])


def bind(route_id, record_id):
    if _control is not None:
        try:
            _control.bind(route_id, record_id)
        except (OSError, ValueError):
            logger.warning("could not bind request recording route")


def release(record_id, *, end_route):
    if _control is not None:
        try:
            _control.release(record_id, end_route=end_route)
        except (OSError, ValueError):
            logger.warning("could not release request recording route")
