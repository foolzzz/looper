"""Dashboard API for agent configuration, provider config, and self-update."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

from .agent_config import (
    AgentConfigError,
    global_agents_dir,
    load_agent_definitions,
    load_providers,
    providers_path,
)
from .self_update import _latest_wheel_url, DEFAULT_UPDATE_REPO

CHAT_AGENT_CONFIG_PATH = "/api/chat/agent-config"
CHAT_AGENT_CONFIG_APPLY_PATH = "/api/chat/agent-config/apply"
CHAT_PROVIDER_CONFIG_PATH = "/api/chat/provider-config"
CHAT_PROVIDER_CONFIG_APPLY_PATH = "/api/chat/provider-config/apply"
CHAT_UPDATE_CHECK_PATH = "/api/chat/update-check"
CHAT_UPDATE_APPLY_PATH = "/api/chat/update-apply"


class _AgentConfigServer(Protocol):
    runtime_root: Path


class AgentConfigRequestMixin:
    """Read and write agent/provider YAML from the dashboard."""

    server: _AgentConfigServer

    def _send_json(self, payload: dict, *, status: int = 200) -> None: ...
    def _send_error(self, message: str, *, status: int, error_code: str) -> None: ...
    def _read_json(self) -> dict: ...

    # --- Agent config ---

    def _agent_config_list(self) -> None:
        runtime_root = self.server.runtime_root
        try:
            providers = load_providers(runtime_root)
        except AgentConfigError:
            providers = {}

        valid, invalid, provider_issues = load_agent_definitions(
            None, runtime_root=runtime_root
        )

        agents = []
        for agent_id in sorted(valid):
            agent = valid[agent_id]
            agents.append(agent.to_dict())
        for agent_id in sorted(invalid):
            agents.append({
                "id": agent_id,
                "valid": False,
                "issues": invalid[agent_id],
            })

        provider_list = []
        for p in providers.values():
            provider_list.append(p.to_dict())

        self._send_json({
            "ok": True,
            "agents": agents,
            "providers": provider_list,
            "provider_issues": provider_issues,
        })

    def _agent_config_apply(self) -> None:
        try:
            body = self._read_json()
            agent_id = body.get("agent_id")
            if not agent_id or not isinstance(agent_id, str):
                raise ValueError("agent_id is required")
            fields = body.get("fields")
            if not isinstance(fields, dict):
                raise ValueError("fields must be a mapping")
            _write_agent_yaml(
                self.server.runtime_root, agent_id, fields
            )
        except (TypeError, ValueError) as exc:
            self._send_error(str(exc), status=400, error_code="invalid_agent_config")
            return
        except OSError as exc:
            self._send_error(
                f"Could not write agent config: {exc}",
                status=500,
                error_code="agent_config_write_failed",
            )
            return
        self._agent_config_list()

    # --- Provider config ---

    def _provider_config_list(self) -> None:
        runtime_root = self.server.runtime_root
        try:
            providers = load_providers(runtime_root)
            provider_list = [p.to_dict() for p in providers.values()]
            self._send_json({"ok": True, "providers": provider_list})
        except AgentConfigError as exc:
            self._send_error(
                str(exc), status=500, error_code="provider_config_error"
            )

    def _provider_config_apply(self) -> None:
        try:
            body = self._read_json()
            providers = body.get("providers")
            if not isinstance(providers, list):
                raise ValueError("providers must be a list")
            _write_providers_yaml(self.server.runtime_root, providers)
        except (TypeError, ValueError) as exc:
            self._send_error(str(exc), status=400, error_code="invalid_provider_config")
            return
        except OSError as exc:
            self._send_error(
                f"Could not write provider config: {exc}",
                status=500,
                error_code="provider_config_write_failed",
            )
            return
        self._provider_config_list()

    # --- Update ---

    def _update_check(self) -> None:
        from . import __version__

        wheel_url = _latest_wheel_url(DEFAULT_UPDATE_REPO)
        latest_version = None
        if wheel_url:
            import re
            match = re.search(r"loopx-([0-9]+\.[0-9]+\.[0-9]+)", wheel_url)
            if match:
                latest_version = match.group(1)

        self._send_json({
            "ok": True,
            "current_version": __version__,
            "latest_version": latest_version,
            "update_available": (
                latest_version is not None and latest_version != __version__
            ),
            "wheel_url": wheel_url,
        })

    def _update_apply(self) -> None:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "loopx.cli", "update", "apply", "--format", "json"],
                text=True,
                capture_output=True,
                timeout=120,
            )
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = {}
            self._send_json({
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "detail": payload,
                "stderr": result.stderr[-1000:] if result.stderr else "",
            })
        except subprocess.TimeoutExpired:
            self._send_error(
                "Update timed out after 120 seconds",
                status=504,
                error_code="update_timeout",
            )
        except Exception as exc:
            self._send_error(
                str(exc), status=500, error_code="update_failed"
            )


_AGENT_YAML_FIELDS = (
    "role", "runtime", "provider", "model", "reasoning_effort",
    "permission_mode", "sandbox", "max_concurrency", "enabled",
    "description", "system_prompt_file",
)


def _write_agent_yaml(runtime_root: Path, agent_id: str, fields: dict[str, Any]) -> None:
    agents_dir = global_agents_dir(runtime_root)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{agent_id}.yaml"

    lines: list[str] = []
    for key in _AGENT_YAML_FIELDS:
        if key in fields:
            value = fields[key]
            if value is None:
                continue
            if isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            else:
                lines.append(f"{key}: {value}")

    extra_args = fields.get("extra_args")
    if extra_args and isinstance(extra_args, list):
        lines.append("extra_args:")
        for arg in extra_args:
            lines.append(f"  - {arg}")

    path.write_text("\n".join(lines) + "\n")


def _write_providers_yaml(runtime_root: Path, providers: list[dict[str, Any]]) -> None:
    path = providers_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = ["providers:"]
    for p in providers:
        name = p.get("name", "default")
        kind = p.get("kind", "anthropic")
        auth = p.get("auth", {})
        auth_type = auth.get("type", "oauth_cli") if isinstance(auth, dict) else "oauth_cli"

        lines.append(f"  {name}:")
        lines.append(f"    kind: {kind}")

        auth_parts = [f"type: {auth_type}"]
        if isinstance(auth, dict):
            if auth.get("env"):
                auth_parts.append(f"env: {auth['env']}")
        lines.append(f"    auth: {{{', '.join(auth_parts)}}}")

        if p.get("base_url"):
            lines.append(f"    base_url: {p['base_url']}")

    path.write_text("\n".join(lines) + "\n")


__all__ = ["AgentConfigRequestMixin"]
