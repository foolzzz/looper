"""Native Coco Workers and Reviewers, using the Codex stream and an isolated Coco home."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ... import child_env
from . import ApiProtocol, AuthKind, Capability, Launcher, Probe, ProviderBinding, ReadOnly, RuntimeUnsupported
from . import coco_login
from .codex import CodexHome, CodexRuntime

if TYPE_CHECKING:
    from ..agent_settings import Launch
    from ..profile_registry import LegacyProviderSpec as ProviderSpec
    from . import ResidentSession

COCO = CodexHome(env="TRAE_HOME", config="traecli.toml", state="cli", local="coco", layout_id="coco-home/v1",
                 login=coco_login, runtime="coco", top_config=("check_for_update_on_startup = false",),
                 features=("memories = false",))


class CocoRuntime(CodexRuntime):
    type = "coco"
    label = "Coco (TRAE CLI)"
    default_executable = "coco"
    home = COCO
    launchers = frozenset({Launcher.NATIVE})
    ttadk_tool = None
    ttadk_runtime_name = None
    capabilities = frozenset({Capability.RUN, Capability.MCP, Capability.ADD_DIRS})
    protocols = frozenset({ApiProtocol.OPENAI_RESPONSES})
    auth_kinds = frozenset({AuthKind.OAUTH})
    login_models = coco_login.SUGGESTED_MODELS
    effort_levels = ()
    isolation_id = COCO.layout_id
    read_only = ReadOnly.SANDBOX
    provider_env_owned = frozenset({"TRAE_HOME", "TRAECLI_HOME"})

    def auth_kinds_for(self, launcher: Launcher) -> frozenset[AuthKind]:
        return self.auth_kinds

    def probe_for(self, executable: str, launcher: Launcher) -> Probe:
        return self.probe(executable)

    def probe(self, executable: str) -> Probe:
        # Coco has its own version sequence: never apply Codex's app-server version gate.
        try:
            with tempfile.TemporaryDirectory(prefix="looper-coco-probe-") as home:
                done = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                      timeout=10, check=False, env={**child_env.inherited(), "TRAE_HOME": home})
        except FileNotFoundError:
            return Probe(None, self.capabilities, f"{executable!r} was not found")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Probe(None, self.capabilities, f"{executable} --version did not run: {type(exc).__name__}: {exc}")
        first = next((line.strip() for line in (done.stdout or done.stderr).splitlines() if line.strip()), "")
        if done.returncode != 0 or not first:
            return Probe(None, self.capabilities, f"{executable} --version failed (exit code {done.returncode})"
                                                  + (f": {first}" if first else ""))
        return Probe(first[:200], self.capabilities)

    def bind_provider(self, provider: ProviderSpec, model: str, effort: str) -> ProviderBinding:
        if provider.auth is not AuthKind.OAUTH or provider.oauth_runtime_type != self.type:
            raise RuntimeUnsupported(f"coco runs with the coco login only; provider {provider.name!r} is not it")
        if provider.protocols != self.protocols:
            raise RuntimeUnsupported(f"coco speaks openai-responses; provider {provider.name!r} "
                                     "must supply that exact protocol")
        if len(provider.catalog) != 1 or provider.catalog[0].name != model:
            raise RuntimeUnsupported("coco requires one resolved model matching the launch model")
        if effort:
            raise RuntimeUnsupported("coco takes no effort level in Looper yet")
        return ProviderBinding(args=("-m", model), env=provider.env, unset=coco_login.ENV_UNSET)

    def check_pairing(self, provider: ProviderSpec, model: str, effort: str | None = None, *,
                      executable: str | None = None) -> None:
        super().check_pairing(provider, model, effort, executable=executable)
        if effort:
            raise RuntimeUnsupported("coco takes no effort level in Looper yet")
        if problem := _model_problem(model, executable or self.default_executable):
            raise RuntimeUnsupported(problem)

    async def login_problem(self, provider: ProviderSpec, *, executable: str | None = None) -> str | None:
        if problem := await coco_login.launch_problem():
            return problem
        # Resolved profiles expose only the selected model in their binding catalog.
        if len(provider.catalog) == 1:
            return await asyncio.to_thread(_model_problem, provider.catalog[0].name,
                                           executable or self.default_executable)
        return None

    def environment(self, launch: Launch) -> dict[str, str]:
        binding = self.bind_provider(launch.profile.provider, launch.model, launch.effort)
        for name in ("TRAE_HOME", "TRAECLI_HOME"):
            if name in dict(launch.env) or name in dict(binding.env):
                raise RuntimeUnsupported(f"coco runs in a home of Looper's: remove {name} from the Agent settings' "
                                         "or the provider's variables")
        return super().environment(launch)

    def resident_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                         max_turns: int = 0) -> list[str]:
        raise RuntimeUnsupported("the coco runtime cannot run the Manager or a Planner yet")

    def resume_args(self, session_id: str) -> list[str]:
        raise RuntimeUnsupported("the coco runtime cannot run the Manager or a Planner yet")

    def resident_session(self, label: str) -> ResidentSession:
        raise RuntimeUnsupported("the coco runtime cannot run the Manager or a Planner yet")

    def steer_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                      max_turns: int = 0, session_dir: Path | None = None) -> list[str]:
        raise RuntimeUnsupported("coco runs cannot be steered yet")

    def consult_command(self, executable: str, *, cwd: str | Path, prompt_file: Path, launch: Launch,
                        session_id: str, session_dir: Path | None, fork_dir: Path, max_turns: int = 0) -> list[str]:
        raise RuntimeUnsupported("coco runs cannot be consulted yet")

    def launch_record(self, launch: Launch) -> dict[str, Any]:
        return {**super().launch_record(launch), "commit_attribution": "trae"}


def _model_problem(model: str, executable: str) -> str | None:
    models = coco_login.available_models(executable)
    if models is not None and model.casefold() not in models:
        return (f"coco has no model {model!r} on this machine; it has: "
                f"{', '.join(sorted(set(models.values()))[:20])}")
    return None
