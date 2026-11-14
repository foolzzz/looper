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
from . import (LOG_FILE, OWNER_FILE, OwnerMode, PROXY_ENV, TLS_ENV, ROUTES_DIR, ROUTE_FORMAT, ROUTE_ID, STATE_FILE, LlmrecRoute, LlmrecSkip,
               RouteFile, RouteKind, SkipReason, boot_argv, code_sha256, process_alive)
from .. import proc_tree
from ..keeper import read_json, write_json

logger = logging.getLogger(__name__)
_control: ProxyControl | None = None
_SECRET_MARKERS = ("token", "secret", "key", "auth", "cookie")
# Stable explanations for run metadata and the recording viewer.
NOT_COVERED_DETAILS = {
    **{(runtime, auth): f"{runtime} picks request details from the endpoint URL; "
                        "recording through the proxy would change its requests"
       for runtime in ("codex", "omp", "pi", "dsh") for auth in ("oauth", "api_key")},
    ("grok", "oauth"): "Grok has no known endpoint override",
    ("cursor-agent", "oauth"): "Cursor's servers make the model requests",
    ("coco", "oauth"): "TRAE's servers make the model requests",
    **{(runtime, auth): "TTADK manages its own provider"
       for runtime in ("ttadk-claude-code", "ttadk-codex", "ttadk-cursor-agent", "ttadk-coco")
       for auth in ("oauth", "api_key")},
}


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


async def _gateway_unavailable(launch, *, agent_id, task_id):
    logger.warning("Local gateway unavailable for %s", agent_id)
    if task_id:
        from looper.db import activity

        try:
            await activity.insert_activity(task_id=task_id, worker=agent_id, kind="error",
                summary="This run cannot start: the local gateway is unavailable.")
        except Exception:
            logger.debug("could not insert gateway failure activity")
    # Exception text can contain endpoints or credentials; keep the launch diagnostic bounded and safe.
    return replace(launch, llmrec=LlmrecSkip(SkipReason.GATEWAY_UNAVAILABLE,
                                           "local proxy startup or route setup failed"))


class ProxyControl:
    def __init__(self, runtime_root, dsn, *, enabled, include_oauth=False):
        self.runtime_root = Path(runtime_root)
        self.dsn = dsn
        self.enabled = enabled
        self.include_oauth = include_oauth
        self.gateway_enabled = False
        self.gateway_port = None
        self._outdated = False
        self._health_cache = None
        self._failed_health = None
        self._health_at = 0.0
        self._start_lock = asyncio.Lock()
        self._start_attempted = False
        self._children: dict[int, asyncio.subprocess.Process] = {}
        self._owner = None
        self._closed = False
        self._resident: dict[str, RouteFile] = {}
        # Queued turns belong to this controller; keep old proxies' route files compatible.
        self._pending_record_ids: dict[str, int] = {}

    async def _claim_owner(self) -> None:
        if self._owner is None:
            started = await asyncio.to_thread(proc_tree._start_time, os.getpid())
            if not started:
                raise RuntimeError("cannot identify request recording owner")
            self._owner = {"mode": OwnerMode.OWNED, "pid": os.getpid(), "start_time": started,
                           "token": secrets.token_urlsafe(16)}
        path = self.runtime_root / OWNER_FILE
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(path, self._owner)

    async def close(self, *, handover=False, grace_s=5) -> None:
        """Stop owned proxies, or explicitly leave their endpoint for a planned upgrade."""
        async with self._start_lock:
            if self._closed:
                return
            owner = read_json(self.runtime_root / OWNER_FILE)
            if owner and (not self._owner or owner.get("token") != self._owner["token"]):
                return  # A replacement controller has already adopted the endpoint.
            if handover and self._owner:
                write_json(self.runtime_root / OWNER_FILE, {**self._owner, "mode": OwnerMode.HANDOVER})
                logger.info("request recording proxy left running for planned handover")
                return
            state = self._state()
            if state and self._owner:
                await self._stop(state, grace_s=grace_s)
            # A startup failure may leave a child that has not published proxy.json.
            for pid, child in list(self._children.items()):
                if child.returncode is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(child.wait(), grace_s)
                    logger.info("request recording proxy stopped pid=%s", pid)
                except asyncio.TimeoutError:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(child.wait(), 5)
                    logger.warning("request recording proxy killed after shutdown timeout pid=%s", pid)
                self._children.pop(pid, None)
            self._closed = True

    def _state(self) -> dict | None:
        state = read_json(self.runtime_root / STATE_FILE)
        if (not state or type(state.get("pid")) is not int or state["pid"] <= 0
                or not isinstance(state.get("start_time"), str) or not state["start_time"]
                or type(state.get("port")) is not int or not 0 < state["port"] < 65536
                or not isinstance(state.get("instance"), str)):
            return None
        return state

    async def _running(self, state) -> bool:
        child = self._children.get(state["pid"]) if state else None
        if child is not None and child.returncode is not None:
            return False
        return bool(state and await asyncio.to_thread(process_alive, state["pid"], state["start_time"]))

    async def _health(self, *, cached=False) -> dict | None:
        # Status polls never probe processes or HTTP, even when the periodic tick is late.
        if cached:
            return self._health_cache if time.monotonic() - self._health_at < 15 else None
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
            self._outdated = (result.get("code_sha256") != code_sha256()
                              or result.get("route_format") != ROUTE_FORMAT
                              or result.get("features", {}).get("translate", 0) < 1)
        return result

    def _has_active_routes(self) -> bool:
        return any(route and not route.ended_at for route in (
            RouteFile.read(path, allow_legacy=True) for path in (self.runtime_root / ROUTES_DIR).glob("*.json")))

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
                        logger.warning("request recording proxy killed after shutdown timeout pid=%s", state["pid"])
                    except ProcessLookupError:
                        pass
                deadline = time.monotonic() + 5
                while await self._running(state) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                if await self._running(state):
                    logger.error("request recording proxy did not stop pid=%s", state["pid"])
                    raise TimeoutError("request recording proxy did not stop")
            logger.info("request recording proxy stopped pid=%s", state["pid"])
        child = self._children.pop(state["pid"], None)
        if child is not None:
            await asyncio.wait_for(child.wait(), 5)
        current = self._state()
        if not preserve_state and (current is None or all(
                current[key] == state[key] for key in ("instance", "pid", "start_time"))):
            (self.runtime_root / STATE_FILE).unlink(missing_ok=True)
            self._start_attempted = False
        self._health_cache, self._health_at = None, 0.0
        if not preserve_state:
            self._failed_health = None

    async def _start(self, *, port=None) -> bool:
        async with self._start_lock:
            if self._closed:
                return False
            await self._claim_owner()
            if port is None and self.gateway_enabled:
                port = self.gateway_port
            if await self._health():
                if not self._outdated:
                    return port is None or self._state()["port"] == port
                # Kept agents still use this process, including idle resident routes.
                # Defer replacement until every route has ended; never advertise it as ready.
                if self._has_active_routes():
                    return False
                state = self._state()
                port = port if port is not None else state["port"]
                await self._stop(state)
            self._start_attempted = True
            previous = self._state()
            port = port if port is not None else previous["port"] if previous else 0
            if previous and self._failed_health and all(
                    self._failed_health.get(key) == previous[key] for key in ("instance", "pid")):
                # Keep the pinned port and failure counters until a replacement
                # publishes its state, including when its first boot fails.
                await self._stop(previous, grace_s=2, preserve_state=True)
                if await self._health():
                    return not (self.gateway_enabled and self._outdated)
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
                    return not (self.gateway_enabled and self._outdated)
                if process.returncode is not None:
                    self._children.pop(process.pid, None)
                    # A concurrent controller may have won the lock.
                    return bool(await self._health()) and not (self.gateway_enabled and self._outdated)
                await asyncio.sleep(0.05)
            return False

    async def at_start(self, kept_record_ids) -> None:
        from looper.db import system_settings
        from .gateway import GatewayConfig, GatewayKey

        await self._claim_owner()
        values = await system_settings.get_settings(["gateway.enabled", "gateway.port"])
        self.gateway_enabled = values.get("gateway.enabled") == "true"
        self.gateway_port = int(values["gateway.port"]) if values.get("gateway.port") else None
        if self.gateway_enabled and not GatewayKey(self.runtime_root).exists():
            GatewayKey(self.runtime_root).rotate()
        GatewayConfig(self.runtime_root).write(enabled=self.gateway_enabled, record=self.enabled)
        needed = self.enabled or self.gateway_enabled
        kept = set(kept_record_ids)
        state = self._state()
        health = await self._health()
        if health and self.gateway_enabled and self.gateway_port is not None and state["port"] != self.gateway_port:
            # An endpoint never silently moves to a different port. Active routes retain the old proxy.
            if not kept:
                await self._stop(state)
                await self._start(port=self.gateway_port)
                health = await self._health()
                state = self._state()
        if health:
            current = code_sha256()
            mismatch = (state.get("code_sha256") != current
                        or health.get("code_sha256") != current
                        or state.get("config_sha256") != config_sha256(current, self.dsn))
            if not kept and (not needed or mismatch):
                port = state["port"]
                await self._stop(state)
                if needed:
                    # Preserve the port through replacement without a stale process identity.
                    await self._start(port=(self.gateway_port or port) if self.gateway_enabled else port)
        elif needed:
            await self._start()
        state = self._state()
        if self.gateway_enabled and self.gateway_port is None and state and await self._health():
            self.gateway_port = state["port"]
            await system_settings.put_settings({"gateway.port": str(self.gateway_port)})
        for path in (self.runtime_root / ROUTES_DIR).glob("*.json"):
            route = RouteFile.read(path, allow_legacy=True)
            if route and route.record_id not in kept:
                replace(route, last_record_id=route.record_id if route.record_id is not None else route.last_record_id,
                        record_id=None,
                        ended_at=route.ended_at or _now()).write(path)

    async def tick(self) -> None:
        if self.gateway_enabled or (self.runtime_root / STATE_FILE).exists():
            health = await self._health()
            if not health or self._outdated:
                await self._start()

    async def set_enabled(self, enabled, *, include_oauth=None) -> None:
        self.enabled = enabled
        if include_oauth is not None:
            self.include_oauth = include_oauth
        from .gateway import GatewayConfig

        GatewayConfig(self.runtime_root).write(enabled=self.gateway_enabled, record=self.enabled)
        if enabled or self.gateway_enabled:
            try:
                await self._start()
            except (OSError, ValueError):
                logger.warning("could not start request recording proxy")

    async def set_gateway(self, enabled, *, port=None):
        from looper.db import system_settings
        from .gateway import GatewayConfig, GatewayKey

        if type(enabled) is not bool or port is not None and (type(port) is not int or not 0 < port < 65536):
            raise ValueError("gateway port must be an integer from 1 to 65535")
        state = self._state()
        selected = port if port is not None else self.gateway_port
        if enabled and selected is None and state and await self._health():
            # Pin the existing endpoint even when kept routes defer its upgrade.
            selected = state["port"]
        if enabled and state and selected is not None and selected != state["port"] and await self._health():
            if self._has_active_routes():
                raise ValueError("gateway port cannot change while agent routes are active")
            await self._stop(state)
        self.gateway_enabled = enabled
        self.gateway_port = selected
        if enabled and not GatewayKey(self.runtime_root).exists():
            GatewayKey(self.runtime_root).rotate()
        await system_settings.put_settings({"gateway.enabled": str(enabled).lower(),
            **({"gateway.port": str(selected)} if selected is not None else {})})
        GatewayConfig(self.runtime_root).write(enabled=enabled, record=self.enabled)
        if enabled:
            try:
                ready = await self._start()
            except (OSError, ValueError):
                ready = False
            state = self._state()
            if ready and selected is None and state:
                self.gateway_port = state["port"]
                await system_settings.put_settings({"gateway.port": str(self.gateway_port)})

    async def status(self, *, cached: bool = False) -> dict:
        health = await self._health(cached=cached)
        state = self._state()
        counters = health or self._failed_health or {}
        return {"enabled": self.enabled, "include_oauth": self.include_oauth, "proxy": {
            "state": "outdated" if health and self._outdated else "running" if health else
                     "unavailable" if self.enabled or self.gateway_enabled or state or self._start_attempted else "stopped",
            "port": state["port"] if state else None, "pid": state["pid"] if state else None,
            **{name: int(counters.get(name, 0)) for name in ("requests", "recorded", "gaps", "dropped", "write_errors")},
            "gap_reasons": counters.get("gap_reasons", {})},
            "translate": counters.get("translate", {}),
            "auth_failures": counters.get("auth_failures", 0), "alias_usage": counters.get("alias_usage", {})}

    async def attach(self, launch, *, agent_id, task_id, step_id=None, reuse=False, cwd=None):
        from ..runtimes import AuthKind, Launcher

        from .gateway import translated

        if translated(launch):
            return await self._attach_translated(launch, agent_id=agent_id, task_id=task_id,
                                                 step_id=step_id, reuse=reuse, cwd=cwd)
        if not self.enabled:
            return launch
        provider = launch.profile.provider
        runtime_type = launch.profile.runtime.type
        if (provider.auth == AuthKind.OAUTH and runtime_type == "claude-code"
                and launch.profile.runtime.launcher is not Launcher.TTADK and not self.include_oauth):
            return launch
        upstream = launch.runtime.llmrec_upstream(launch)
        if upstream is None:
            coverage_type = f"ttadk-{runtime_type}" if launch.profile.runtime.launcher is Launcher.TTADK else runtime_type
            detail = NOT_COVERED_DETAILS.get((coverage_type, provider.auth.value),
                                             f"{runtime_type} has no model endpoint override")
            return replace(launch, llmrec=LlmrecSkip(SkipReason.NOT_COVERED, detail))
        try:
            url = urlsplit(upstream)
            credentials = url.username is not None or url.password is not None or any(
                any(marker in name.lower() for marker in _SECRET_MARKERS)
                for name, _ in parse_qsl(url.query, keep_blank_values=True))
        except ValueError:
            credentials = True
        if credentials:
            return replace(launch, llmrec=LlmrecSkip(SkipReason.CREDENTIALS_IN_URL))
        if not await self._health(cached=not reuse):
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
        env = replace(launch, llmrec=None).environment()
        proxy_env = {name: value for name, value in env.items() if name in PROXY_ENV}
        # The proxy has its own cwd; relative trust paths belong
        # to the agent process that would have loaded them.
        tls_env = {name: os.path.realpath(os.path.join(cwd or os.getcwd(), value)) if value else value
                   for name, value in env.items() if name in TLS_ENV}
        previous = self._resident.get(agent_id) if reuse else None
        if previous is not None:
            path = self.runtime_root / ROUTES_DIR / f"{previous.id}.json"
            previous = RouteFile.read(path)
            if previous is not None and not previous.ended_at and (
                    previous.upstream, previous.runtime_type, previous.provider_id, previous.model,
                    previous.kind, previous.dialect, previous.models
            ) == (upstream, runtime_type, provider.id, launch.model, RouteKind.RECORD, None, None):
                if (previous.proxy_env, previous.tls_env) != (proxy_env, tls_env):
                    replace(previous, proxy_env=proxy_env, tls_env=tls_env).write(path)
                return replace(launch, llmrec=LlmrecRoute(
                    previous.id, f"http://127.0.0.1:{state['port']}/r/{previous.id}", upstream))
            if previous is not None:
                replace(previous, ended_at=previous.ended_at or _now()).write(path)
        identifier = secrets.token_urlsafe(16)
        resolved = getattr(launch.profile, "resolved_model", None)
        protocol = resolved.model.protocol.value if resolved else next(iter(provider.protocols)).value
        route = RouteFile(format=ROUTE_FORMAT, id=identifier, upstream=upstream, protocol=protocol,
                          runtime_type=runtime_type, provider_id=provider.id, model=launch.model,
                          agent_id=agent_id, task_id=task_id, step_id=step_id,
                          role=getattr(launch.role, "value", launch.role), created_at=_now(),
                          record_id=None, last_record_id=None, ended_at=None,
                          proxy_env=proxy_env, tls_env=tls_env)
        route.write(self.runtime_root / ROUTES_DIR / f"{identifier}.json")
        if reuse:
            self._resident[agent_id] = route
        port = state["port"]
        return replace(launch, llmrec=LlmrecRoute(identifier, f"http://127.0.0.1:{port}/r/{identifier}", upstream))

    async def _attach_translated(self, launch, *, agent_id, task_id, step_id=None, reuse=False, cwd=None):
        from .gateway import dialect_for

        upstream = launch.runtime.llmrec_upstream(launch)
        try:
            url = urlsplit(upstream)
            credentials = url.username is not None or url.password is not None or any(
                any(marker in name.lower() for marker in _SECRET_MARKERS)
                for name, _ in parse_qsl(url.query, keep_blank_values=True))
        except ValueError:
            credentials = True
        if credentials:
            return replace(launch, llmrec=LlmrecSkip(SkipReason.CREDENTIALS_IN_URL))
        health = await self._health(cached=not reuse)
        if not health:
            if not await self._start():
                return await _gateway_unavailable(launch, agent_id=agent_id, task_id=task_id)
            health = await self._health()
        if not health:
            return await _gateway_unavailable(launch, agent_id=agent_id, task_id=task_id)
        if (self._outdated or health.get("route_format") != ROUTE_FORMAT
                or health.get("features", {}).get("translate", 0) < 1):
            return replace(launch, llmrec=LlmrecSkip(SkipReason.PROXY_OUTDATED))
        state = self._state()
        if state is None:
            return await _gateway_unavailable(launch, agent_id=agent_id, task_id=task_id)
        provider = launch.profile.provider
        binding = launch.runtime.bind_provider(provider, launch.model, launch.effort)
        env = {**child_env.inherited(), **dict(launch.env), **dict(binding.env)}
        proxy_env = {name: value for name, value in env.items() if name in PROXY_ENV}
        tls_env = {name: os.path.realpath(os.path.join(cwd or os.getcwd(), value)) if value else value
                   for name, value in env.items() if name in TLS_ENV}
        dialect = dialect_for(launch)
        background = provider.catalog_model(launch.model).background_model
        # Claude Code consumes context suffixes itself; upstream API names never carry them.
        models = {}
        for name in (launch.model, background):
            if name:
                api_name = launch.runtime.api_model(name)
                models[name] = api_name
                models[api_name] = api_name
        runtime_type = launch.profile.runtime.type
        previous = self._resident.get(agent_id) if reuse else None
        if previous is not None:
            path = self.runtime_root / ROUTES_DIR / f"{previous.id}.json"
            previous = RouteFile.read(path)
            if previous is not None and not previous.ended_at and (
                    previous.upstream, previous.runtime_type, previous.provider_id, previous.model,
                    previous.kind, previous.dialect, previous.models
            ) == (upstream, runtime_type, provider.id, launch.model, RouteKind.TRANSLATE, dialect, models):
                if (previous.proxy_env, previous.tls_env, previous.record) != (proxy_env, tls_env, self.enabled):
                    replace(previous, proxy_env=proxy_env, tls_env=tls_env, record=self.enabled).write(path)
                return replace(launch, llmrec=LlmrecRoute(previous.id,
                    f"http://127.0.0.1:{state['port']}/r/{previous.id}", upstream,
                    kind=RouteKind.TRANSLATE, record=self.enabled))
            if previous is not None:
                replace(previous, ended_at=previous.ended_at or _now()).write(path)
        identifier = secrets.token_urlsafe(16)
        route = RouteFile(id=identifier, upstream=upstream, protocol="openai-chat", api="openai-chat",
                          kind=RouteKind.TRANSLATE, record=self.enabled, dialect=dialect, models=models,
                          runtime_type=runtime_type, provider_id=provider.id, model=launch.model,
                          agent_id=agent_id, task_id=task_id, step_id=step_id,
                          role=getattr(launch.role, "value", launch.role), created_at=_now(),
                          proxy_env=proxy_env, tls_env=tls_env)
        route.write(self.runtime_root / ROUTES_DIR / f"{identifier}.json")
        if reuse:
            self._resident[agent_id] = route
        return replace(launch, llmrec=LlmrecRoute(identifier,
            f"http://127.0.0.1:{state['port']}/r/{identifier}", upstream,
            kind=RouteKind.TRANSLATE, record=self.enabled))

    def bind(self, route_id, record_id):
        if not ROUTE_ID.fullmatch(route_id):
            raise ValueError("invalid request recording route")
        path = self.runtime_root / ROUTES_DIR / f"{route_id}.json"
        route = RouteFile.read(path, allow_legacy=True)
        if route is None:
            raise ValueError("request recording route is missing")
        if route.record_id is not None:
            if route.record_id != record_id:
                self._pending_record_ids[route_id] = record_id
            return
        # A replaced resident route may still serve its final handoff turn.
        replace(route, record_id=record_id).write(path)

    def release(self, record_id, *, end_route):
        for path in (self.runtime_root / ROUTES_DIR).glob("*.json"):
            route = RouteFile.read(path, allow_legacy=True)
            if route and route.record_id == record_id:
                pending = self._pending_record_ids.get(route.id)
                replace(route, last_record_id=record_id, record_id=None if end_route else pending,
                        ended_at=(_now() if end_route else route.ended_at)).write(path)
                self._pending_record_ids.pop(route.id, None)
            elif route and self._pending_record_ids.get(route.id) == record_id:
                self._pending_record_ids.pop(route.id)

    async def cache_reported(self, route_id: str, record_id: int) -> bool | None:
        """Proxy evidence for this run; unavailable or evicted evidence stays unknown."""
        state = self._state()
        if state is None:
            return None
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                response = await client.get(
                    f"http://127.0.0.1:{state['port']}/llmrec/cache-usage/{route_id}/{record_id}")
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get('instance') == state['instance']:
                    reported = data.get('cache_reported')
                    return reported if isinstance(reported, bool) else None
        except (httpx.HTTPError, ValueError):
            logger.warning('could not read translated run cache usage')
        return None


async def attach(launch, **kwargs):
    from ..profile_registry import RegistryError
    from ..runtimes import RuntimeUnsupported

    if _control is None:
        return launch
    try:
        return await _control.attach(launch, **kwargs)
    except RegistryError as exc:
        raise RuntimeUnsupported(str(exc)) from None
    except RuntimeUnsupported:
        raise
    except (OSError, ValueError):
        from .gateway import translated

        if translated(launch):
            return await _gateway_unavailable(launch, agent_id=kwargs["agent_id"], task_id=kwargs["task_id"])
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
