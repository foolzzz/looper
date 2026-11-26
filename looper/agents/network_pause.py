"""Durable task pauses driven only by a model endpoint connectivity probe.

No request, authentication or model inference is performed by the probe. A
deadline moves the pause to operator attention; only disabling recovery or
stopping/restarting the task then releases it.
"""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import network_pause_settings
from ..db import network_pauses as store

logger = logging.getLogger(__name__)
PROBE_INITIAL_SECONDS = 5.0
PROBE_MAX_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 5.0


def endpoint(launch) -> dict | None:
    """Freeze only connection coordinates, never keys, URL paths or credentials."""
    profile = launch.profile
    url = profile.provider.base_url
    if not url:
        env = dict(launch.settings.env)
        runtime = profile.runtime.type if hasattr(profile, 'runtime') else 'claude-code'
        variable = 'ANTHROPIC_BASE_URL' if runtime == 'claude-code' else 'OPENAI_BASE_URL'
        url = env.get(variable)
        if not url:
            from .runtimes import AuthKind
            url = {'claude-code': 'https://api.anthropic.com',
                   'codex': ('https://chatgpt.com' if profile.provider.auth is AuthKind.OAUTH
                             else 'https://api.openai.com')}.get(runtime)
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return None
        return {'host': parsed.hostname, 'port': parsed.port or (443 if parsed.scheme == 'https' else 80),
                'tls': parsed.scheme == 'https'}
    except ValueError:
        return None


async def probe(target: dict | None) -> bool:
    if target is None:
        return False  # No known endpoint must never be treated as recovered.
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(
            target['host'], target['port'], ssl=ssl.create_default_context() if target['tls'] else None),
            PROBE_TIMEOUT_SECONDS)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, asyncio.TimeoutError):
                pass


async def wait(task_id: str, *, target=None, kind=None, changed=None) -> bool:
    """Wait for an existing pause, or open one with ``target``. Cancellation keeps it durable.

    ``changed`` projects control state onto the task status and activity log;
    none of this state is inserted into a model's prompt or journal.
    """
    config = await network_pause_settings.load()
    row = await store.get(task_id)
    if row is None:
        if not config.enabled or kind is None:
            return False
        now = datetime.now(timezone.utc)
        row = await store.put(task_id, {'reason': 'Model API network unavailable; waiting for connectivity',
            'started_at': now, 'deadline': now + timedelta(seconds=config.max_pause_seconds),
            'continuation': {'endpoint': target, 'kind': kind}})
        logger.warning('network_pause task=%s pause_id=%s reason=%s', task_id, row['id'], row['reason'])
    if changed is not None:
        await changed(row)
    delay = PROBE_INITIAL_SECONDS
    while True:
        try:
            # A failed settings read is not operator authorization to release an existing pause.
            config = await network_pause_settings.load(strict=True)
            current = await store.get(task_id)
        except Exception:
            logger.exception('network_pause_state_unavailable task=%s; retaining pause', task_id)
            await asyncio.sleep(PROBE_MAX_SECONDS)
            continue
        if not config.enabled:
            await store.clear(task_id, reason='disabled', pause_id=row['id'])
            if changed is not None:
                await changed(None)
            logger.info('network_resume task=%s reason=disabled', task_id)
            return True
        if current is None:
            if changed is not None:
                await changed(None)
            return True
        row = current
        now = datetime.now(timezone.utc)
        if row['state'] == 'paused' and now >= row['deadline']:
            row = await store.expire(task_id, pause_id=row['id'])
            if row is not None:
                logger.warning('network_pause_expired task=%s pause_id=%s', task_id, row['id'])
                if changed is not None:
                    await changed(row)
            continue
        if row['state'] == 'waiting_user':
            # No further probe or model calls past the bound. Recheck operator settings only.
            await asyncio.sleep(PROBE_MAX_SECONDS)
            continue
        remaining = max(0, (row['deadline'] - now).total_seconds())
        await asyncio.sleep(min(remaining, PROBE_MAX_SECONDS, delay * random.uniform(.8, 1.2)))
        if datetime.now(timezone.utc) >= row['deadline']:
            continue
        connected = await probe(row['continuation'].get('endpoint'))
        if datetime.now(timezone.utc) >= row['deadline']:
            continue
        if connected:
            ended = await store.clear(task_id, reason='resumed', pause_id=row['id'])
            if ended is None:
                continue
            logger.info('network_resume task=%s pause_id=%s duration_seconds=%.3f', task_id, row['id'],
                        (datetime.now(timezone.utc) - row['started_at']).total_seconds())
            if changed is not None:
                await changed(None)
            return True
        delay = min(PROBE_MAX_SECONDS, max(PROBE_INITIAL_SECONDS, delay * 2))


async def existing(task_id: str) -> bool:
    """Launch gate for task-bound runtimes; a disabled installation does no pause read."""
    if not (await network_pause_settings.load()).enabled:
        return False
    if await store.get(task_id) is None:
        return False
    return await wait(task_id)
