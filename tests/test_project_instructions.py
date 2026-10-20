"""Which of a project's own instruction files, CLAUDE.md and AGENTS.md, each runtime is given
(``looper/agents/runtimes/instructions.py``).

What each runtime reads itself was found with Claude Code 2.1.285, Codex
0.162.0 and omp 18.4.3 under Looper's isolation (the multi-runtime RFC,
section 9), and with Grok 1.0.62 (the Grok runtime RFC, section 2); these tests check Looper's side: the files each runtime is said to
be given, the Codex config that makes it read CLAUDE.md, the text Looper adds
to OMP's and pi's system prompts, and the run's record of them.
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


def _injected(path: str, text: str) -> dict[str, Any]:
    data = text.encode()
    return {"path": path, "delivery": "injected",
            "sha256": hashlib.sha256(data[:INJECTED_MAX_BYTES].decode().strip().encode()).hexdigest(),
            "bytes": len(data), "clipped_bytes": max(len(data) - INJECTED_MAX_BYTES, 0)}


def _runtime_prompt(runtime: str, cwd: Path, root: Path) -> str:
    """The system prompt file an OMP or pi run in ``cwd`` is started with."""
    command = adapter(runtime).run_command(runtime, cwd=cwd, prompt_file=SystemPrompt(LOOPER_PROMPT).write_file(root),
                                           launch=_launch(runtime))
    return Path(command[command.index("--append-system-prompt") + 1]).read_text(encoding="utf-8")


# --- In a repository: each runtime reads its own file, and the other where the project has only that ---


@pytest.mark.parametrize("files, expected", [
    ({"CLAUDE.md": "c"},
     {"claude-code": ["CLAUDE.md"], "codex": ["CLAUDE.md"], "omp": ["CLAUDE.md"], "grok": ["CLAUDE.md"]}),
    ({"AGENTS.md": "a"},
     {"claude-code": ["AGENTS.md"], "codex": ["AGENTS.md"], "omp": ["AGENTS.md"], "grok": ["AGENTS.md"]}),
    # Grok reads every name it knows in a directory: both.
    ({"CLAUDE.md": "c", "AGENTS.md": "a"},
     {"claude-code": ["CLAUDE.md"], "codex": ["AGENTS.md"], "omp": ["AGENTS.md"], "grok": ["CLAUDE.md", "AGENTS.md"]}),
    ({}, {"claude-code": [], "codex": [], "omp": [], "grok": []}),
], ids=["only CLAUDE.md", "only AGENTS.md", "both", "neither"])
def test_each_runtime_is_given_one_file_of_the_repository_read_by_itself(
    tmp_path: Path, files: dict[str, str], expected: dict[str, list[str]],
) -> None:
    repo = _repository(tmp_path, **files)
    expected_records = {runtime: _native(*paths) for runtime, paths in expected.items()}
    expected_records["pi"] = [_injected(name, files[name]) for name in ("AGENTS.md", "CLAUDE.md") if name in files][:1]
    assert _given(repo) == expected_records


def test_below_the_root_claude_code_reads_the_projects_claude_md_and_the_others_one_file_a_directory(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    (repo / "CLAUDE.md").write_text("root\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "AGENTS.md").write_text("pkg\n")
    assert _given(repo / "pkg") == {"claude-code": _native("../CLAUDE.md"),
                                    "codex": _native("../CLAUDE.md", "AGENTS.md"),
                                    "omp": _native("../CLAUDE.md", "AGENTS.md"),
                                    "grok": _native("../CLAUDE.md", "AGENTS.md"),
                                    "pi": [_injected("../CLAUDE.md", "root\n"), _injected("AGENTS.md", "pkg\n")]}


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
    prompt = _runtime_prompt("omp", repo, tmp_path / "rt")
    assert "REPOSITORY CLAUDE.md" not in prompt and "# Project instructions" not in prompt
    assert prompt.startswith("# Runtime notes (OMP)") and prompt.endswith(LOOPER_PROMPT)


def test_pi_injects_one_file_per_directory_in_order_and_stops_at_repository_root(tmp_path: Path) -> None:
    repo = _repository(tmp_path, **{"AGENTS.md": "ROOT RULES", "CLAUDE.md": "IGNORED ROOT RULES"})
    package = repo / "pkg"
    work = package / "src"
    work.mkdir(parents=True)
    (package / "CLAUDE.md").write_text("PACKAGE RULES")
    (work / "AGENTS.md").write_text("WORK RULES")
    (work / "CLAUDE.md").write_text("IGNORED WORK RULES")
    (work / "AGENTS.override.md").write_text("IGNORED OVERRIDE")
    (work / ".pi").mkdir()
    for name in ("SYSTEM.md", "APPEND_SYSTEM.md"):
        (work / ".pi" / name).write_text(f"IGNORED {name}")
    command = adapter("pi").run_command("pi", cwd=work, launch=_launch("pi"),
                                         prompt_file=SystemPrompt(LOOPER_PROMPT).write_file(tmp_path / "rt"))
    prompt = Path(command[command.index("--append-system-prompt") + 1]).read_text()
    assert "--no-context-files" in command and "--no-approve" in command
    assert "IGNORED" not in prompt and "ABOVE THE REPOSITORY" not in prompt
    parts = [prompt.index(text) for text in ("ROOT RULES", "PACKAGE RULES", "WORK RULES",
                                           "# Runtime notes (pi)", LOOPER_PROMPT)]
    assert parts == sorted(parts)
    assert record(adapter("pi").instruction_files(work), work) == [
        _injected("../../AGENTS.md", "ROOT RULES"), _injected("../CLAUDE.md", "PACKAGE RULES"),
        _injected("AGENTS.md", "WORK RULES"),
    ]


def test_pi_clips_each_repository_instruction_file_independently(tmp_path: Path) -> None:
    root_text = "ROOT " + "r" * INJECTED_MAX_BYTES + "ROOT END"
    repo = _repository(tmp_path, **{"AGENTS.md": root_text})
    work = repo / "pkg"
    work.mkdir()
    work_text = "WORK " + "w" * INJECTED_MAX_BYTES + "WORK END"
    (work / "CLAUDE.md").write_text(work_text)
    prompt = _runtime_prompt("pi", work, tmp_path / "rt")
    assert "ROOT END" not in prompt and "WORK END" not in prompt
    assert "r" * (INJECTED_MAX_BYTES - 5) in prompt
    assert "w" * (INJECTED_MAX_BYTES - 5) in prompt
    assert prompt.count("[clipped:") == 2
    assert record(adapter("pi").instruction_files(work), work) == [
        _injected("../AGENTS.md", root_text), _injected("CLAUDE.md", work_text),
    ]


# --- Outside a repository: omp's discovery is off, so Looper gives it the work directory's file ---


@pytest.mark.parametrize("files, given", [
    ({"CLAUDE.md": "WORK CLAUDE.md\n"}, "CLAUDE.md"),
    ({"AGENTS.md": "WORK AGENTS.md\n"}, "AGENTS.md"),
    ({"CLAUDE.md": "WORK CLAUDE.md\n", "AGENTS.md": "WORK AGENTS.md\n"}, "AGENTS.md"),
], ids=["only CLAUDE.md", "only AGENTS.md", "both"])
@pytest.mark.parametrize("runtime,label", [("omp", "OMP"), ("pi", "pi")])
def test_outside_a_repository_runtime_gets_the_work_directorys_file_in_its_system_prompt(
    tmp_path: Path, files: dict[str, str], given: str, runtime: str, label: str,
) -> None:
    (tmp_path / "CLAUDE.md").write_text("ABOVE THE WORK DIRECTORY\n")
    work = tmp_path / "work"
    work.mkdir()
    for name, text in files.items():
        (work / name).write_text(text)
    prompt = _runtime_prompt(runtime, work, tmp_path / "rt")

    # The file comes first, then the runtime notes and Looper's prompt, last.
    assert prompt.startswith(f"# Project instructions: {given}\n") and str(work / given) in prompt
    at = [prompt.index(part) for part in (f"WORK {given}", f"# Runtime notes ({label})", LOOPER_PROMPT)]
    assert at == sorted(at) and prompt.endswith(LOOPER_PROMPT)
    assert "ABOVE THE WORK DIRECTORY" not in prompt
    assert [text for name, text in files.items() if name != given and text.strip() in prompt] == []
    # The other runtimes read it themselves; the record says how each was given it.
    entries = _given(work)
    assert entries["claude-code"] == _native("CLAUDE.md" if "CLAUDE.md" in files else "AGENTS.md")
    assert entries["codex"] == _native(given)
    assert entries["grok"] == _native(*(name for name in ("CLAUDE.md", "AGENTS.md") if name in files))
    (entry,) = entries[runtime]
    text = files[given].strip()
    assert entry == {"path": given, "delivery": "injected", "sha256": hashlib.sha256(text.encode()).hexdigest(),
                     "bytes": len(files[given].encode()), "clipped_bytes": 0}
    assert entries["pi"] == entries["omp"]


@pytest.mark.parametrize("runtime,label", [("omp", "OMP"), ("pi", "pi")])
def test_outside_a_repository_without_either_file_runtime_gets_no_project_section(
    tmp_path: Path, runtime: str, label: str,
) -> None:
    prompt = _runtime_prompt(runtime, tmp_path, tmp_path / "rt")
    assert prompt.startswith(f"# Runtime notes ({label})") and _given(tmp_path)[runtime] == []


@pytest.mark.parametrize("runtime", ["omp", "pi"])
@pytest.mark.parametrize("character", ["r", "规"])
def test_an_oversized_file_is_clipped_and_the_record_says_by_how_much(
    tmp_path: Path, runtime: str, character: str,
) -> None:
    text = "# Rules\n" + character * INJECTED_MAX_BYTES + "THE END\n"
    (tmp_path / "AGENTS.md").write_text(text)
    prompt = _runtime_prompt(runtime, tmp_path, tmp_path / "rt")

    clipped = len(text.encode()) - INJECTED_MAX_BYTES
    assert "THE END" not in prompt and f"[clipped: {clipped} more bytes; read the file for the rest]" in prompt
    (entry,) = _given(tmp_path)[runtime]
    assert entry["bytes"] == len(text.encode()) and entry["clipped_bytes"] == clipped
    expected = text.encode()[:INJECTED_MAX_BYTES].decode("utf-8", errors="replace").strip()
    assert entry["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert expected in prompt


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("runtime,label", [("omp", "OMP"), ("pi", "pi")])
def test_a_file_that_cannot_be_read_is_left_out_and_the_record_says_why(
    tmp_path: Path, runtime: str, label: str,
) -> None:
    (tmp_path / "CLAUDE.md").write_text("UNREADABLE\n")
    os.chmod(tmp_path / "CLAUDE.md", 0)
    try:
        prompt = _runtime_prompt(runtime, tmp_path, tmp_path / "rt")
        (entry,) = _given(tmp_path)[runtime]
    finally:
        os.chmod(tmp_path / "CLAUDE.md", 0o600)
    assert prompt.startswith(f"# Runtime notes ({label})")
    assert entry["delivery"] == "injected" and entry["error"].startswith("PermissionError") and "sha256" not in entry


# --- The run's record ---


def test_a_runs_record_names_the_files_its_runtime_was_given(tmp_path: Path, agent_db: Any) -> None:
    (tmp_path / "CLAUDE.md").write_text("WORK CLAUDE.md\n")
    for runtime, delivery in (("omp", "injected"), ("codex", "native"), ("pi", "injected")):
        recorder = ClaudeRunRecorder(agent_id="worker-1", task_id=None, command=[runtime], cwd=str(tmp_path),
                                     input_text="go", launch=_launch(runtime))
        asyncio.run(recorder.start())
        (entry,) = agent_db.start_record.await_args.kwargs["metadata"]["project_instructions"]
        assert (entry["path"], entry["delivery"]) == ("CLAUDE.md", delivery)
