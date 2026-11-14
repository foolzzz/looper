"""Detached request recording process. Its database connection never gates forwarding."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import signal
import sys
import time

# Import the complete recorder before serving: an in-place upgrade must not mix versions.
from . import capture as capture, control, dialects, gateway, proxy, translate, translate_stream, writer  # noqa: F401
from . import LOCK_FILE, LOG_FILE, OWNER_FILE, OwnerMode, ROUTE_FORMAT, STATE_FILE, code_sha256, process_alive
from .. import proc_tree
from ..keeper import read_json, write_json
from ...db import llm_requests as llm_requests, gateway as gateway_db  # noqa: F401
from ...db.connection import init_pool

logger = logging.getLogger(__name__)


async def _serve(args, dsn):
    root = args.runtime_root
    parent = os.getppid()  # Capture before startup can yield to a dying parent.
    state_path = root / STATE_FILE
    sink = writer.DbSink(None)
    instance = secrets.token_urlsafe(16)
    server = proxy.Proxy(root, sink=sink, port=args.port, instance=instance)
    # Routes held by running agents pin this endpoint. A bind failure must leave
    # the state intact so the controller can retry the same port on its next tick.
    port = await server.start()
    code = code_sha256()
    write_json(state_path, {"instance": instance, "pid": os.getpid(),
                           "start_time": proc_tree._start_time(os.getpid()), "port": port,
                           "code_sha256": code, "config_sha256": control.config_sha256(code, dsn),
                           "route_format": ROUTE_FORMAT, "started_at": datetime.now(timezone.utc).isoformat()})
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop_signal(sig):
        logger.info("request recording proxy stopping: signal=%s", sig.name)
        stopped.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda sig=sig: stop_signal(sig))

    async def watch_owner():
        owner_seen = False
        while not stopped.is_set():
            owner = read_json(root / OWNER_FILE)
            owner_seen = owner_seen or owner is not None
            if owner and owner.get("mode") == OwnerMode.HANDOVER:
                alive = True  # Kept runs pin this endpoint until the next controller adopts it.
            elif owner and owner.get("mode") == OwnerMode.OWNED:
                if owner.get("pid") == parent and os.getppid() != parent:
                    alive = False  # Reparenting proves death even without ps or reaping the owner.
                else:
                    alive = (await asyncio.to_thread(process_alive, owner.get("pid"),
                                                     owner.get("start_time"))) is not False
            else:
                # Direct CLI boots without an owner file still follow their original parent.
                alive = not owner_seen and parent != 1 and os.getppid() == parent
            if not alive:
                if read_json(root / OWNER_FILE) != owner:
                    continue  # Adoption or a planned handover won the liveness check.
                logger.info("request recording proxy stopping: owner disappeared "
                            "mode=%s owner_pid=%s original_parent=%s current_parent=%s",
                            owner.get("mode") if owner else None, owner.get("pid") if owner else None,
                            parent, os.getppid())
                stopped.set()
                return
            try:
                await asyncio.wait_for(stopped.wait(), 1)
            except asyncio.TimeoutError:
                pass

    async def connect():
        backoff = 1
        while not stopped.is_set():
            try:
                sink.pool = await init_pool(dsn, min_size=1, max_size=2)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # Connection errors can include credentials and the DSN. Never render them.
                sink.write_errors += 1
                logger.warning("request recording database unavailable; retrying")
                try:
                    await asyncio.wait_for(stopped.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30)

    async def watch():
        last_request, count = time.monotonic(), 0
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), args.check_interval)
            except asyncio.TimeoutError:
                pass
            state = read_json(state_path)
            if not state or state.get("instance") != instance:
                logger.info("request recording proxy stopping: state removed or replaced")
                stopped.set()
            requests = server.stats["requests"]
            if requests != count:
                last_request, count = time.monotonic(), requests
            if time.monotonic() - last_request >= 24 * 60 * 60:
                logger.info("request recording proxy stopping: idle for 24 hours")
                stopped.set()

    connection_task = asyncio.create_task(connect())
    writer_task = asyncio.create_task(sink.run(recover=True))
    watch_task = asyncio.create_task(watch())
    owner_task = asyncio.create_task(watch_owner())

    def force_exit():
        logger.error("request recording shutdown timed out; exiting")
        os._exit(1)

    try:
        await stopped.wait()
    finally:
        # Parent death has no controller left to escalate a stuck stream or database drain.
        backstop = loop.call_later(5, force_exit)
        connection_task.cancel()
        watch_task.cancel()
        owner_task.cancel()
        await asyncio.gather(connection_task, watch_task, owner_task, return_exceptions=True)
        await server.close(grace_s=30)
        await sink.close(timeout=10)
        writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)
        if sink.pool is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(sink.pool.close(), 5)
        backstop.cancel()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--check-interval", type=float, default=60)
    args = parser.parse_args(argv)
    if args.check_interval <= 0 or not 0 <= args.port < 65536:
        parser.error("check interval must be positive and port must be between 0 and 65535")
    dsn = sys.stdin.readline().rstrip("\r\n")
    directory = args.runtime_root / LOCK_FILE.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(args.runtime_root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        log = args.runtime_root / LOG_FILE
        log.parent.mkdir(parents=True, exist_ok=True)
        logfile = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(logfile)
        handler = RotatingFileHandler(log, maxBytes=20 * 1024 ** 2, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        # HTTPX's INFO line includes URLs, including upstream query values.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        try:
            asyncio.run(_serve(args, dsn))
        except Exception:
            logger.error("request recording proxy stopped unexpectedly")
            return 1
        return 0
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
