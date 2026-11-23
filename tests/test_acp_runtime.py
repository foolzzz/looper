"""The pilot's adapter surface cannot grow into other roles or a vendor runner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents.runtimes import ADAPTERS, AuthKind, Capability, CostSource, RuntimeUnsupported, VendorType
from looper.agents.runtimes.acp import AcpRuntime, record_command
from looper.agents.runtimes.acp_channel import AcpChannel
from looper.agents.run_usage import Usage


def test_reviewer_is_the_only_role_and_run_the_only_capability():
    runtime = ADAPTERS["acp"]
    assert runtime.roles == {"reviewer"}
    assert runtime.capabilities == {Capability.RUN}
    assert runtime.default_executable == ""
    assert runtime.auth_kinds == {AuthKind.OAUTH}
    assert runtime.protocols == {VendorType.ACP_COMMAND.login_protocol}


def test_command_is_verbatim_and_no_model_flags_or_prompt_are_appended(tmp_path):
    runtime = AcpRuntime()
    command = ("grok", "agent", "stdio", "--model", "operator-model")
    launch = SimpleNamespace(acp_reviewer=SimpleNamespace(command=command), model="declared-model")
    assert runtime.run_command("ignored", cwd=tmp_path, prompt_file=tmp_path / "secret-contract", launch=launch) == list(command)
    provider = SimpleNamespace(auth=AuthKind.OAUTH, oauth_runtime_type="acp")
    assert runtime.bind_provider(provider, "declared-model", "high").args == ()
    assert isinstance(runtime.run_channel(None, cwd=tmp_path, launch=launch), AcpChannel)


@pytest.mark.parametrize("method", ["resident_command", "steer_command"])
def test_resident_and_steering_paths_are_explicitly_unsupported(tmp_path, method):
    with pytest.raises(RuntimeUnsupported, match="not supported"):
        getattr(AcpRuntime(), method)("grok", cwd=tmp_path, prompt_file=tmp_path / "contract", launch=None)


def test_a_different_runtimes_login_cannot_bind():
    provider = SimpleNamespace(auth=AuthKind.OAUTH, oauth_runtime_type="grok")
    with pytest.raises(RuntimeUnsupported, match="acp-command"):
        AcpRuntime().bind_provider(provider, "declared-model", "")


def test_cost_wraps_the_already_priced_usage_without_recomputing():
    usage = Usage(input_tokens=12, output_tokens=3, cost_usd=0.123)
    cost = AcpRuntime().cost(usage, None)
    assert cost.usage is usage and cost.source is CostSource.PROVIDER_PRICE
    assert AcpRuntime().cost_source(None) is CostSource.PROVIDER_PRICE


def test_environment_does_not_borrow_another_runtime_login(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/another-runtime")
    monkeypatch.setenv("GROK_HOME", "/another-runtime")
    monkeypatch.setenv("XAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret")
    env = AcpRuntime().environment(SimpleNamespace(env=()))
    assert not {"CODEX_HOME", "GROK_HOME", "XAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"} & env.keys()


def test_record_command_masks_secret_flags_urls_and_registered_values(monkeypatch):
    from looper.agents import log_redaction
    monkeypatch.setattr(log_redaction, "_secrets", ("synthetic-registered-value",))
    argv = ["/operator/bin/grok", "agent", "stdio", "--api-key", "synthetic-key", "--access-token=synthetic-token",
            "https://user:password@example.invalid/api?api_key=synthetic-query&mode=review", "--model", "declared-model",
            "synthetic-registered-value", "sk-synthetic-key"]
    safe = record_command(argv)
    assert safe[:3] == ["grok", "agent", "stdio"]
    assert safe[3:6] == ["--api-key", "***", "--access-token=***"]
    assert safe[6] == "https://***@example.invalid/api?api_key=%2A%2A%2A&mode=review"
    assert safe[7:9] == ["--model", "declared-model"]
    assert safe[-2:] == ["***", "***"]
    assert record_command([]) == ["acp"]
    assert record_command(["grok", "agent", "stdio", "--apiKey", "synthetic-key", "--apikey=synthetic-key"]) == [
        "grok", "agent", "stdio", "--apiKey", "***", "--apikey=***"]


@pytest.mark.parametrize("error", [ValueError("acp_reviewer.command is not a JSON array"), RuntimeError("DB failed")])
def test_login_check_keeps_settings_read_failure_on_the_selected_runtime(monkeypatch, error):
    from looper.agents import acp_reviewer_settings
    monkeypatch.setattr(acp_reviewer_settings, "read", AsyncMock(side_effect=error))
    problem = asyncio.run(AcpRuntime().login_problem(None))
    assert "acp_reviewer settings unavailable" in problem
    assert ("acp_reviewer.command" in problem) if isinstance(error, ValueError) else ("RuntimeError" in problem)
