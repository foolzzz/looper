"""Check serve once, independently of its event loop and database, and notify as a bot."""

from __future__ import annotations

import asyncio
import json
import math
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from looper import ops_config
from looper.agents import operator_auth
from looper.agents.channels import lark_cli

TICK_STALE_SECONDS = 120
SLEEP_GAP = timedelta(minutes=3)
HTTP_TIMEOUT_SECONDS = 5


class Verdict(StrEnum):
    OK = "ok"
    NO_ANSWER = "no_answer"
    UNAUTHORIZED = "unauthorized"
    SCHEDULER_STOPPED = "scheduler_stopped"
    TICK_STALE = "tick_stale"


class Action(StrEnum):
    NONE = "none"
    ALERT = "alert"
    RECOVER = "recover"


@dataclass
class State:
    last_sample_at: datetime | None = None
    last_ok_at: datetime | None = None
    first_bad_at: datetime | None = None
    alert_started_at: datetime | None = None
    last_verdict: str | None = None
    last_send_error: str | None = None


def sample(port: int, token: str | None, host: str = "127.0.0.1") -> tuple[Verdict, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    address = "127.0.0.1" if host in ("", "localhost", "0.0.0.0") else "::1" if host == "::" else host
    if ":" in address:
        address = f"[{address}]"
    request = Request(f"http://{address}:{port}/api/v1/system/health", data=b"{}", headers=headers, method="POST")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            answer = json.load(response)
    except HTTPError as exc:
        exc.close()
        return (Verdict.UNAUTHORIZED, "unauthorized") if exc.code == 401 else (Verdict.NO_ANSWER, f"HTTP {exc.code}")
    except (OSError, URLError, ValueError, HTTPException) as exc:
        return Verdict.NO_ANSWER, str(getattr(exc, "reason", exc)) or type(exc).__name__
    if not isinstance(answer, dict) or answer.get("status") != "ok":
        return Verdict.NO_ANSWER, "health response status is not ok"
    # Versioned HTTP operations wrap their result; accept the projection itself as well.
    answer = answer.get("result", answer)
    if not isinstance(answer, dict) or answer.get("status") != "ok":
        return Verdict.NO_ANSWER, "health result status is not ok"
    scheduler = answer.get("scheduler")
    if not isinstance(scheduler, dict) or type(scheduler.get("running")) is not bool:
        return Verdict.NO_ANSWER, "invalid scheduler health"
    if not scheduler["running"]:
        return Verdict.SCHEDULER_STOPPED, "scheduler stopped"
    age = scheduler.get("last_ok_tick_age_seconds")
    if age is None:
        return Verdict.TICK_STALE, "last successful tick age is null"
    if type(age) not in (int, float) or not math.isfinite(age):
        return Verdict.NO_ANSWER, "invalid last successful tick age"
    if age > TICK_STALE_SECONDS:
        return Verdict.TICK_STALE, f"last successful tick was {age:g} seconds ago"
    return Verdict.OK, "ok"


def step(state: State, verdict: Verdict, now: datetime, after: timedelta) -> tuple[State, Action]:
    state = replace(state)
    if state.last_sample_at is not None and (now < state.last_sample_at or now - state.last_sample_at > SLEEP_GAP):
        state.first_bad_at = None
    action = Action.NONE
    if verdict == Verdict.OK:
        state.last_ok_at = now
        state.first_bad_at = None
        if state.alert_started_at is not None:
            action = Action.RECOVER
    else:
        state.first_bad_at = state.first_bad_at or now
        if state.alert_started_at is None and now - state.first_bad_at >= after:
            action = Action.ALERT
    state.last_sample_at = now
    state.last_verdict = verdict.value
    return state, action


def _read_state(path: Path) -> State:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        values = {}
        for name in ("last_sample_at", "last_ok_at", "first_bad_at", "alert_started_at"):
            value = data.get(name)
            values[name] = datetime.fromisoformat(value) if value is not None else None
            if values[name] is not None and values[name].utcoffset() is None:
                return State()
        for name in ("last_verdict", "last_send_error"):
            value = data.get(name)
            if value is not None and not isinstance(value, str):
                return State()
            values[name] = value
        return State(**values)
    except (OSError, ValueError, TypeError, AttributeError):
        return State()


def _write_json(path: Path, data: dict) -> None:
    ops_config.write_private(path, json.dumps(data, ensure_ascii=False, default=lambda value: value.isoformat()) + "\n")


def _credentials(data) -> dict | None:
    if (not isinstance(data, dict) or data.get("domain") not in ("feishu", "lark")
            or not all(isinstance(data.get(key), str) and data[key] for key in ("app_id", "app_secret"))):
        return None
    return {key: data[key] for key in ("app_id", "app_secret", "domain")}


async def _bot_credentials(ops: ops_config.OpsConfig, runtime_root: Path) -> dict | None:
    import asyncpg

    path = runtime_root / ".local/probe/lark_bot.json"
    connection = None
    credentials = None
    read_ok = False
    try:
        connection = await asyncpg.connect(ops.database_url, timeout=5, command_timeout=5)
        rows = await connection.fetch(
            "SELECT c.channel_id, c.config, s.bot_token FROM channel_configs c JOIN channel_secrets s USING (channel_id) "
            "WHERE c.channel_type = 'lark_bot'")
        candidates = []
        for row in rows:
            config = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            if (not ops.probe_lark_profile or ops.probe_lark_profile == row["channel_id"]
                    or ops.probe_lark_profile == config.get("profile")):
                candidate = _credentials({"app_id": config.get("app_id"), "domain": config.get("domain", "feishu"),
                                          "app_secret": row["bot_token"]})
                if candidate:
                    candidates.append(candidate)
        if len(candidates) == 1:
            credentials = candidates[0]
        read_ok = True
    except Exception:
        # A failed database is precisely when the independent probe must still send.
        pass
    finally:
        if connection is not None:
            try:
                await connection.close(timeout=5)
            except Exception:
                connection.terminate()
    if credentials:
        _write_json(path, {**credentials, **({"profile": ops.probe_lark_profile} if ops.probe_lark_profile else {})})
        return credentials
    if read_ok:
        # A reachable database with no unique match must not reuse another bot's credentials.
        path.unlink(missing_ok=True)
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("profile", "") == ops.probe_lark_profile:
            return _credentials(cached)
    except (OSError, ValueError):
        return None
    return None


async def _bot_send(ops: ops_config.OpsConfig, runtime_root: Path, text: str, key: str, target: dict) -> str | None:
    from looper.agents.channels.lark_api import BASE_URLS, LarkApi

    credentials = await _bot_credentials(ops, runtime_root)
    if credentials is None:
        return "no lark_bot credentials: the database is unreachable and no cached copy exists"
    api = LarkApi(credentials["app_id"], credentials["app_secret"], base_url=BASE_URLS[credentials["domain"]])
    try:
        await api.send(**target, text=text, idempotency_key=key)
    finally:
        await api.aclose()
    return None


def send(ops: ops_config.OpsConfig, runtime_root: Path, text: str, key: str) -> str | None:
    target = {"chat_id" if ops.probe_notify_chat.startswith("oc_") else "user_id": ops.probe_notify_chat}
    try:
        if ops.probe_notify_channel == "lark_bot":
            return asyncio.run(_bot_send(ops, runtime_root, text, key, target))
        if ops.probe_notify_channel != "lark":
            return "unsupported probe.notify_channel"
        argv = lark_cli.send_command(**target, text=text, profile=ops.probe_lark_profile or None, idempotency_key=key)
        assert "--as" in argv and argv[argv.index("--as") + 1] == "bot"
        result = subprocess.run(argv, env=lark_cli.environment(), timeout=20, capture_output=True)
        if result.returncode:
            lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            return lines[-1] if lines else f"lark-cli exited {result.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    except Exception as exc:
        # Do not persist driver exceptions that might include database or application credentials.
        return f"probe notification failed: {type(exc).__name__}"
    return None


def run(ops: ops_config.OpsConfig, runtime_root: Path) -> int:
    if not ops.probe_notify_channel or not ops.probe_notify_chat:
        print("probe is not configured: set probe.notify_channel and probe.notify_chat in the ops file")
        return 0
    verdict, reason = sample(ops.port, operator_auth.read_token(runtime_root), ops.host)
    path = runtime_root / ".local/probe/state.json"
    previous = _read_state(path)
    now = datetime.now(timezone.utc)
    state, action = step(previous, verdict, now, timedelta(minutes=ops.probe_after_minutes))
    if verdict == Verdict.OK and ops.probe_notify_channel == "lark_bot" and action == Action.NONE:
        # Prime and refresh before the first outage, while the database still answers.
        try:
            asyncio.run(_bot_credentials(ops, runtime_root))
        except OSError as exc:
            state.last_send_error = f"could not cache lark_bot credentials: {type(exc).__name__}"
    if action == Action.ALERT:
        minutes = max(0, int((now - state.first_bad_at).total_seconds() // 60))
        text = (f"告警：Looper 服务（{socket.gethostname()}，端口 {ops.port}）已经 {minutes} 分钟没有正常响应："
                f"{reason}。定时任务和渠道可能都停了。")
        state.last_send_error = send(ops, runtime_root, text, f"looper-probe-{int(state.first_bad_at.timestamp())}")
        if state.last_send_error is None:
            state.alert_started_at = now
    elif action == Action.RECOVER:
        minutes = max(0, int((now - state.alert_started_at).total_seconds() // 60))
        text = f"恢复：Looper 服务又正常了（中断约 {minutes} 分钟）。"
        state.last_send_error = send(ops, runtime_root, text,
                                     f"looper-probe-recovered-{int(state.alert_started_at.timestamp())}")
        if state.last_send_error is None:
            state.alert_started_at = None
    _write_json(path, asdict(state))
    if previous.last_verdict != state.last_verdict or action != Action.NONE:
        print(f"probe verdict={verdict.value} since={state.first_bad_at.isoformat() if state.first_bad_at else '-'} "
              f"action={action.value} error={state.last_send_error or '-'}", file=sys.stderr)
    return 0
