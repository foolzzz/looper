"""An absent optional entry must not prevent serve's runtime registration.

Runner assets, Node and npm packages are checked by the startup wrapper;
native CLIs and the shared TTADK launcher by registry probes. ACP is explicitly
configured, never auto-detected. API needs Python SDK packages, not a CLI.
Each case starts a fresh interpreter so import-time reads cannot be cached away.
"""
from __future__ import annotations

import importlib.metadata
import importlib.resources
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.db_support import DSN, run_db


CASES = ["available", "dsh-runner", "pi-runner", "dsh-package", "pi-package", "node",
         "dsh", "pi", "codex", "omp", "grok", "cursor-agent", "coco",
         "ttadk", "ttadk-codex", "ttadk-coco", "ttadk-cursor-agent", "acp", "api-openai", "api-anthropic"]


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("missing", CASES)
def test_runtime_missing_keeps_serve(tmp_path: Path, missing: str) -> None:
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "HOME": str(tmp_path), "PATH": str(tmp_path / "bin"),
           "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "tests"), os.environ.get("PYTHONPATH", "")))}
    # Keep inherited login homes out of the isolated child.
    for key in list(env):
        if key.startswith(("CLAUDE_", "ANTHROPIC_", "OPENAI_", "XAI_", "CODEX_", "GROK_", "CURSOR_",
                           "TRAE", "OMP_", "PI_", "DSH_", "NODE_OPTIONS")):
            del env[key]
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), missing, str(tmp_path)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "registration completed" in result.stdout


def _registration_probe(missing: str, root: Path) -> None:
    """Keep serve imports and real probes in the child, after removing the entry."""
    resources = root / "resources"
    resources.mkdir()
    package = "looper.agents.runtimes.runner"
    # Use actual absent files in a private resource tree, without editing the checkout.
    original_files = importlib.resources.files
    source = Path(__file__).resolve().parents[1] / "looper/agents/runtimes/runner"
    for kind in ("pi", "dsh"):
        if missing != f"{kind}-runner":
            shutil.copytree(source / f"{kind}_runner", resources / f"{kind}_runner")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(importlib.resources, "files",
                      lambda anchor: resources if anchor == package else original_files(anchor))
        if missing.startswith("api-"):
            absent = missing.removeprefix("api-")
            original_version = importlib.metadata.version

            def version(name):
                if name == absent:
                    raise importlib.metadata.PackageNotFoundError(name)
                return original_version(name)

            class AbsentSdk:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split(".")[0] == absent:
                        raise ModuleNotFoundError(fullname)

            patch.setattr(importlib.metadata, "version", version)
            sys.meta_path.insert(0, AbsentSdk())

        from looper.agents.runtimes.pi import PROBE_HELP_TOKENS
        from looper.agents.runtimes.dsh import SUPPORTED_VERSION

        bin_dir = root / "bin"
        bin_dir.mkdir()
        versions = {name: "0.1.0" for name in ("codex", "omp", "coco", "traex", "ttadk", "cursor-agent", "acp")}
        versions.update(node="v22.19.0", grok="1.0.0", pi="1.3.0", dsh=SUPPORTED_VERSION)
        for name, version_text in versions.items():
            target = bin_dir / name
            if name in ("pi", "dsh"):
                directory = root / "packages" / name
                directory.mkdir(parents=True)
                if missing != f"{name}-package":
                    (directory / "package.json").write_text(json.dumps({
                        "name": "@earendil-works/pi-coding-agent" if name == "pi" else "@deepseek-ai/dsh",
                        "version": version_text,
                    }))
                target.symlink_to(directory / name)
                target = directory / name
            target.write_text(
                "#!/bin/sh\ncase \"$*\" in\n"
                f"  --version) echo '{version_text}' ;;\n"
                f"  --help) echo '{' '.join(PROBE_HELP_TOKENS)}' ;;\n"
                "  'login status') echo 'Not logged in'; exit 1 ;;\n"
                "  'models --json') echo '[]' ;;\n"
                "  *) exit 9 ;;\nesac\n")
            target.chmod(0o700)
        if missing == "ttadk-codex":
            settings_dir = root / ".ttadk"
            settings_dir.mkdir()
            (settings_dir / "setting.json").write_text(json.dumps({
                "ai_tool_commands": {"codex": "absent-codex"}}))
        absent_binary = {"ttadk-codex": "absent-codex", "ttadk-coco": "traex",
                         "ttadk-cursor-agent": "cursor-agent"}.get(missing, missing)
        if absent_binary in versions:
            (bin_dir / absent_binary).unlink()

        # Import the actual serve module only after all missing-entry conditions exist.
        from looper import ops_config
        from looper.agents import profile_registry as registry, serve
        from looper.agents.runtimes import ADAPTERS, RuntimeUnsupported
        from looper.db import profile_registry as db

        async def registration(pool):
            found = await registry.register_found_runtimes()
            rows = {row["name"]: row for row in await db.list_runtimes()}
            expected = {"claude-code", "coco", "codex", "omp", "grok", "cursor-agent", "dsh", "pi", "api",
                        "ttadk-coco", "ttadk-codex", "ttadk-cursor-agent"}
            excluded = set()
            if absent_binary in {"dsh", "pi", "codex", "omp", "grok", "cursor-agent", "coco"}:
                excluded.add(absent_binary)
            if missing == "ttadk":
                excluded.update(name for name in expected if name.startswith("ttadk-"))
            if missing.startswith("api-"):
                excluded.add("api")
            assert set(rows) == expected - excluded
            assert set(found) == expected - excluded - {"claude-code"}

            if missing.endswith(("-runner", "-package")) or missing in {"node", "dsh", "pi"}:
                kinds = ("dsh", "pi") if missing == "node" else (missing.split("-")[0],)
                for kind in kinds:
                    adapter = ADAPTERS[kind]
                    reason = adapter.unavailable
                    assert reason, kind
                    if missing.endswith("-runner"):
                        assert "runner is missing" in reason
                    elif missing.endswith("-package"):
                        assert "not inside" in reason
                    else:
                        assert "PATH" in reason
                    with pytest.raises(RuntimeUnsupported) as caught:
                        adapter.resident_command("", cwd=root, prompt_file=root / "prompt", launch=None)
                    assert str(caught.value) == reason
            for name in excluded:
                with pytest.raises(registry.RegistryNotFound) as caught:
                    await registry.probe_runtime(f"rt-{name}")
                reason = str(caught.value)
                assert ("Python package is not installed" if name == "api" else "PATH") in reason
                if name == "api":
                    assert missing.removeprefix("api-") in reason

            for kind in ("pi", "dsh"):
                if missing not in {"node", kind, f"{kind}-runner", f"{kind}-package"}:
                    assert ADAPTERS[kind].unavailable is None
                    assert ADAPTERS[kind].supervisor is not None
            # Codex/Cursor share their binary with TTADK; Coco uses traex.
            failed_probes = {"codex": {"ttadk-codex"}, "cursor-agent": {"ttadk-cursor-agent"},
                             "ttadk-codex": {"ttadk-codex"}, "ttadk-coco": {"ttadk-coco"},
                             "ttadk-cursor-agent": {"ttadk-cursor-agent"}}.get(missing, set())
            for name, row in rows.items():
                if name in failed_probes:
                    assert row["probe_error"] and "was not found" in row["probe_error"]
                else:
                    assert row["probe_error"] is None, (name, row["probe_error"])
            await pool.execute(
                "INSERT INTO system_settings (key, value) VALUES ('acp_reviewer.command', $1)",
                json.dumps(["acp", "--acp"]))
            acp_problem = await ADAPTERS["acp"].login_problem(None)
            assert acp_problem == ("acp_reviewer.command executable is unavailable" if missing == "acp" else None)
            print("registration completed")

        async def exercise(pool):
            # Only the rest of the server is replaced: the wrapper imports/enables
            # both runners, then its owned startup performs the real registration.
            async def owned(**kwargs):
                await registration(pool=pool)

            patch.setattr(serve, "_run_agent_system_owned", owned)
            config = ops_config.OpsConfig(runtime_root=root, path=root / ops_config.FILE, database_url=DSN)
            await serve._run_agent_system(ops=config, runtime_root=root, work_dir=root)

        run_db(exercise, patch, timeout=60)


if __name__ == "__main__":
    _registration_probe(sys.argv[1], Path(sys.argv[2]))
