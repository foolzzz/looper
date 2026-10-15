"""Which of a project's own instruction files, CLAUDE.md and AGENTS.md, each runtime is given
(``looper/agents/runtimes/instructions.py``).

What each runtime reads itself was found with Claude Code 2.1.285, Codex
0.162.0 and omp 18.4.3 under Looper's isolation (the multi-runtime RFC,
section 9); these tests check Looper's side: the files each runtime is said to
be given, the Codex config that makes it read CLAUDE.md, the text Looper adds
to omp's system prompt, and the run's record of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tomllib
from pathlib import Path
from typing import Any

import pytest

from looper.agents.agent_settings import AgentSettings, Launch
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.agents.profile_registry import AgentProfile, ProviderSpec, RuntimeSpec
from looper.agents.runtimes import ADAPTERS, ApiProtocol, AuthKind, adapter
from looper.agents.runtimes.instructions import INJECTED_MAX_BYTES, record
from looper.agents.system_prompt import SystemPrompt

LOOPER_PROMPT = "LOOPER PROMPT: the contract comes last."


def _launch(runtime: str, provider: ProviderSpec | None = None) -> Launch:
    provider = provider or ProviderSpec("prov-cpa", "cpa", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                                        base_url="http://127.0.0.1:8317/v1", api_key="sk-test-0123456789")
    profile = AgentProfile(f"agent-{runtime}", f"{runtime}/gpt-5.5",
                           RuntimeSpec(f"rt-{runtime}", runtime, runtime, runtime, adapter(runtime).capabilities),
                           provider, "gpt-5.5", "low")
    return Launch(AgentSettings(profile=profile))


def _repository(tmp_path: Path, **files: str) -> Path:
    """A git repository with ``files`` at its root, and an AGENTS.md above it that is no file of the project's."""
    (tmp_path / "AGENTS.md").write_text("ABOVE THE REPOSITORY\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    return repo


def _given(cwd: Path) -> dict[str, list[dict[str, Any]]]:
    return {name: record(runtime.instruction_files(cwd), cwd) for name, runtime in ADAPTERS.items()}


def _native(*paths: str) -> list[dict[str, str]]:
    return [{"path": path, "delivery": "native"} for path in paths]


def _omp_prompt(cwd: Path, root: Path) -> str:
    """The system prompt file an omp run in ``cwd`` is started with."""
    command = adapter("omp").run_command("omp", cwd=cwd, prompt_file=SystemPrompt(LOOPER_PROMPT).write_file(root),
                                         launch=_launch("omp"))
    return Path(command[command.index("--append-system-prompt") + 1]).read_text(encoding="utf-8")


# --- In a repository: each runtime reads its own file, and the other where the project has only that ---


@pytest.mark.parametrize("files, expected", [
    ({"CLAUDE.md": "c"}, {"claude-code": ["CLAUDE.md"], "codex": ["CLAUDE.md"], "omp": ["CLAUDE.md"]}),
    ({"AGENTS.md": "a"}, {"claude-code": ["AGENTS.md"], "codex": ["AGENTS.md"], "omp": ["AGENTS.md"]}),
    ({"CLAUDE.md": "c", "AGENTS.md": "a"},
     {"claude-code": ["CLAUDE.md"], "codex": ["AGENTS.md"], "omp": ["AGENTS.md"]}),
    ({}, {"claude-code": [], "codex": [], "omp": []}),
], ids=["only CLAUDE.md", "only AGENTS.md", "both", "neither"])
def test_each_runtime_is_given_one_file_of_the_repository_read_by_itself(
    tmp_path: Path, files: dict[str, str], expected: dict[str, list[str]],
) -> None:
    repo = _repository(tmp_path, **files)
    assert _given(repo) == {runtime: _native(*paths) for runtime, paths in expected.items()}


def test_below_the_root_claude_code_reads_the_projects_claude_md_and_the_others_one_file_a_directory(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "CLAUDE.md").write_text("root\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "AGENTS.md").write_text("pkg\n")
    assert _given(repo / "pkg") == {"claude-code": _native("../CLAUDE.md"),
                                    "codex": _native("../CLAUDE.md", "AGENTS.md"),
                                    "omp": _native("../CLAUDE.md", "AGENTS.md")}


def test_codex_reads_claude_md_where_a_directory_has_no_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    login_dir = tmp_path / "operator-codex"
    login_dir.mkdir()
    (login_dir / "auth.json").write_text('{"tokens": "stand-in"}')
    monkeypatch.setenv("CODEX_HOME", str(login_dir))
    login = ProviderSpec("prov-codex-login", "codex-login", AuthKind.OAUTH, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                         "codex")
    for launch in (_launch("codex"), _launch("codex", login)):
        command = adapter("codex").run_command("codex", cwd=tmp_path, launch=launch,
                                               prompt_file=SystemPrompt(LOOPER_PROMPT).write_file(tmp_path / "rt"))
        home = Path(command[1].removeprefix("CODEX_HOME="))
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        assert config["project_doc_fallback_filenames"] == ["CLAUDE.md"], launch.profile.provider.name


def test_in_a_repository_omp_reads_the_file_itself(tmp_path: Path) -> None:
    repo = _repository(tmp_path, **{"CLAUDE.md": "REPOSITORY CLAUDE.md\n"})
    prompt = _omp_prompt(repo, tmp_path / "rt")
    assert "REPOSITORY CLAUDE.md" not in prompt and "# Project instructions" not in prompt
    assert prompt.startswith("# Runtime notes (OMP)") and prompt.endswith(LOOPER_PROMPT)


# --- Outside a repository: omp's discovery is off, so Looper gives it the work directory's file ---


@pytest.mark.parametrize("files, given", [
    ({"CLAUDE.md": "WORK CLAUDE.md\n"}, "CLAUDE.md"),
    ({"AGENTS.md": "WORK AGENTS.md\n"}, "AGENTS.md"),
    ({"CLAUDE.md": "WORK CLAUDE.md\n", "AGENTS.md": "WORK AGENTS.md\n"}, "AGENTS.md"),
], ids=["only CLAUDE.md", "only AGENTS.md", "both"])
def test_outside_a_repository_omp_gets_the_work_directorys_file_in_its_system_prompt(
    tmp_path: Path, files: dict[str, str], given: str,
) -> None:
    (tmp_path / "CLAUDE.md").write_text("ABOVE THE WORK DIRECTORY\n")
    work = tmp_path / "work"
    work.mkdir()
    for name, text in files.items():
        (work / name).write_text(text)
    prompt = _omp_prompt(work, tmp_path / "rt")

    # The file comes first, saying where it is from; then omp's runtime notes and Looper's prompt, last.
    assert prompt.startswith(f"# Project instructions: {given}\n") and str(work / given) in prompt
    at = [prompt.index(part) for part in (f"WORK {given}", "# Runtime notes (OMP)", LOOPER_PROMPT)]
    assert at == sorted(at) and prompt.endswith(LOOPER_PROMPT)
    assert "ABOVE THE WORK DIRECTORY" not in prompt
    assert [text for name, text in files.items() if name != given and text.strip() in prompt] == []
    # The other runtimes read it themselves; the record says how each was given it.
    entries = _given(work)
    assert entries["claude-code"] == _native("CLAUDE.md" if "CLAUDE.md" in files else "AGENTS.md")
    assert entries["codex"] == _native(given)
    (omp,) = entries["omp"]
    text = files[given].strip()
    assert omp == {"path": given, "delivery": "injected", "sha256": hashlib.sha256(text.encode()).hexdigest(),
                   "bytes": len(files[given].encode()), "clipped_bytes": 0}


def test_outside_a_repository_without_either_file_omp_gets_no_project_section(tmp_path: Path) -> None:
    prompt = _omp_prompt(tmp_path, tmp_path / "rt")
    assert prompt.startswith("# Runtime notes (OMP)") and _given(tmp_path)["omp"] == []


def test_an_oversized_file_is_clipped_and_the_record_says_by_how_much(tmp_path: Path) -> None:
    text = "# Rules\n" + "r" * INJECTED_MAX_BYTES + "THE END\n"
    (tmp_path / "AGENTS.md").write_text(text)
    prompt = _omp_prompt(tmp_path, tmp_path / "rt")

    clipped = len(text.encode()) - INJECTED_MAX_BYTES
    assert "THE END" not in prompt and f"[clipped: {clipped} more bytes; read the file for the rest]" in prompt
    (omp,) = _given(tmp_path)["omp"]
    assert omp["bytes"] == len(text.encode()) and omp["clipped_bytes"] == clipped
    assert omp["sha256"] == hashlib.sha256(text[:INJECTED_MAX_BYTES].strip().encode()).hexdigest()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_file_that_cannot_be_read_is_left_out_and_the_record_says_why(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("UNREADABLE\n")
    os.chmod(tmp_path / "CLAUDE.md", 0)
    try:
        prompt = _omp_prompt(tmp_path, tmp_path / "rt")
        (omp,) = _given(tmp_path)["omp"]
    finally:
        os.chmod(tmp_path / "CLAUDE.md", 0o600)
    assert prompt.startswith("# Runtime notes (OMP)")
    assert omp["delivery"] == "injected" and omp["error"].startswith("PermissionError") and "sha256" not in omp


# --- The run's record ---


def test_a_runs_record_names_the_files_its_runtime_was_given(tmp_path: Path, agent_db: Any) -> None:
    (tmp_path / "CLAUDE.md").write_text("WORK CLAUDE.md\n")
    for runtime, delivery in (("omp", "injected"), ("codex", "native")):
        recorder = ClaudeRunRecorder(agent_id="worker-1", task_id=None, command=[runtime], cwd=str(tmp_path),
                                     input_text="go", launch=_launch(runtime))
        asyncio.run(recorder.start())
        (entry,) = agent_db.start_record.await_args.kwargs["metadata"]["project_instructions"]
        assert (entry["path"], entry["delivery"]) == ("CLAUDE.md", delivery)
