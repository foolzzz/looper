"""Component manifests and the upgrade decision's observable safety branches."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from looper.components import (
    Manifest,
    check_coverage,
    manifest_from_directory,
    manifest_from_git,
    manifest_from_wheel,
    record_web_source,
    runtime_fingerprint,
)
from looper.upgrade import Keeper, UpgradeKind, decide


ROOT = Path(__file__).resolve().parents[1]


def _manifest(*, changed: tuple[str, ...] = (), adoption_change: bool = False,
              protocols: tuple[str, ...] = ()) -> Manifest:
    return Manifest(
        version="1.0.0", loaded_at="2026-10-30T00:00:00+00:00",
        components={name: "b" * 64 if name in changed else "a" * 64
                    for name in ("web", "control_plane", "keeper", "runners")},
        adoption={"run_manifest_version": 1 if adoption_change else 0,
                  "work_contract": "c" * 64, "review_contract": "d" * 64,
                  "seat_tools": {"worker": "e" * 64, "reviewer": "f" * 64},
                  "keeper_protocols": list(protocols)},
        verification={},
    )


@pytest.mark.parametrize(
    ("changed", "keeper_mode", "protocols", "keepers", "adoption_change", "expected"),
    [
        ((), False, (), (), False, UpgradeKind.NOOP),
        (("web",), False, (), (), False, UpgradeKind.WEB_ONLY),
        (("control_plane",), False, (), (), False, UpgradeKind.FULL),
        (("web", "control_plane"), False, (), (), False, UpgradeKind.FULL),
        (("control_plane",), True, (), ("1.0",), False, UpgradeKind.FULL),
        (("control_plane",), True, ("1.0",), ("1.0",), True, UpgradeKind.FULL),
        ((), True, (), (), True, UpgradeKind.FULL),
        (("control_plane",), True, ("1.0",), ("1.0",), False, UpgradeKind.CONTROL_PLANE),
        (("keeper", "control_plane"), False, (), (), False, UpgradeKind.FULL),
        (("keeper", "control_plane"), True, (), (), False, UpgradeKind.CONTROL_PLANE),
        (("runners", "control_plane"), False, (), (), False, UpgradeKind.FULL),
    ],
    ids=["unchanged", "web-only", "keeper-off", "web-and-control-plane", "protocol-mismatch",
         "adoption-mismatch", "adoption-only-mismatch", "adoptable", "keeper-s0-overlap", "keeper-compatible",
         "runners-s0-overlap"],
)
def test_upgrade_decision_branches(changed, keeper_mode, protocols, keepers, adoption_change, expected):
    running = _manifest(protocols=protocols)
    target = _manifest(changed=changed, protocols=protocols, adoption_change=adoption_change)

    plan = decide(target, running, [Keeper(protocol=protocol) for protocol in keepers], keeper_mode)

    assert plan.kind is expected
    assert set(plan.changed_components) == set(changed)
    assert plan.reasons and all(isinstance(reason, str) and reason for reason in plan.reasons)


def test_manifest_round_trip_preserves_all_fingerprints():
    manifest = _manifest(protocols=("1.0",))
    assert Manifest.from_dict(manifest.to_dict()) == manifest


@pytest.mark.parametrize("tracked", [False, True], ids=["generated-lock", "distributed-lock"])
def test_runtime_fingerprint_with_an_isolated_path(component_tree, tmp_path, monkeypatch, tracked):
    before = runtime_fingerprint(component_tree)
    (component_tree / "uv.lock").write_text("version = 1\n")
    if tracked:
        _git(component_tree, "add", "uv.lock")
    expected = runtime_fingerprint(component_tree)
    assert (expected != before) is tracked
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    assert runtime_fingerprint(component_tree) == expected


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=10,
    ).stdout.strip()


@pytest.fixture
def component_tree(tmp_path: Path) -> Path:
    """A complete package for the target's isolated contract-schema probe."""
    root = tmp_path / "source"
    root.mkdir()
    sources = [ROOT / "looper/components.toml", *(ROOT / "looper").rglob("*.py")]
    for source in sources:
        target = root / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    files = {
        "looper/__init__.py": '__version__ = "1.0.0"\n',
        "docs/guide.md": "Ignored documentation\n",
        "pyproject.toml": '[project]\ndependencies = ["httpx>=0.27,<1"]\nrequires-python = ">=3.11"\n',
    }
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Component test")
    _git(root, "config", "user.email", "component-test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "looper", "docs", "pyproject.toml")
    _git(root, "commit", "-q", "-m", "component fixture")
    return root


def test_same_files_have_equal_git_wheel_and_directory_fingerprints(component_tree: Path, tmp_path: Path):
    wheel = tmp_path / "looper-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in sorted((component_tree / "looper").rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(component_tree).as_posix())
        archive.writestr("looper-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: looper\nVersion: 1.0.0\n")
        archive.writestr("looper/.runtime-fingerprint", runtime_fingerprint(component_tree))

    git = manifest_from_git(component_tree, "HEAD")
    installed = manifest_from_directory(component_tree)
    packaged = manifest_from_wheel(wheel)

    assert git.components == installed.components == packaged.components
    assert git.adoption == installed.adoption == packaged.adoption
    assert git.version == installed.version == packaged.version == "1.0.0"
    assert len(git.components["control_plane"]) == 64
    assert git.adoption["run_manifest_version"] == 0
    assert git.adoption["keeper_protocols"] == []


def test_git_manifest_reads_the_requested_tree_without_checkout(component_tree: Path):
    original = manifest_from_git(component_tree, "HEAD")
    planner = component_tree / "looper/agents/tiers/planner.py"
    planner.write_text(planner.read_text() + "\n# committed target\n")
    _git(component_tree, "add", "looper/agents/tiers/planner.py")
    _git(component_tree, "commit", "-q", "-m", "target planner")
    # The working tree deliberately differs from both commits.
    uncommitted = "raise RuntimeError('planning must not execute target services')\n"
    planner.write_text(uncommitted)

    assert manifest_from_git(component_tree, "HEAD~1").components == original.components
    target = manifest_from_git(component_tree, "HEAD")
    assert target.components["control_plane"] != original.components["control_plane"]
    assert target.components != manifest_from_directory(component_tree).components
    assert planner.read_text() == uncommitted


def test_web_build_verification_is_separate_and_matches_wheel_assets(component_tree: Path, tmp_path: Path):
    source = component_tree / "web/src/App.tsx"
    source.parent.mkdir(parents=True)
    source.write_text("export default 'dashboard';\n")
    built = component_tree / "web/dist/index.html"
    built.parent.mkdir(parents=True)
    built.write_text("<html>dashboard</html>\n")
    record_web_source(component_tree, built.parent)
    _git(component_tree, "add", "web")
    _git(component_tree, "commit", "-q", "-m", "dashboard fixture")
    target = manifest_from_git(component_tree, "HEAD")

    wheel = tmp_path / "dashboard.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in (component_tree / "looper").rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(component_tree).as_posix())
        archive.write(built, "looper/web/dashboard/index.html")
        archive.write(built.parent / ".source-fingerprint", "looper/web/dashboard/.source-fingerprint")
        archive.writestr("looper/.runtime-fingerprint", runtime_fingerprint(component_tree))
    packaged = manifest_from_wheel(wheel)
    assert target.verification["web"] == packaged.verification["web"]
    assert target.components["web"] == packaged.components["web"]
    assert decide(target, packaged, [], False).kind is UpgradeKind.NOOP
    assert target.components["web"] != target.verification["web"]

    # Rebuilding assets must not change the source-based upgrade plan.
    built.write_text("<html>rebuilt dashboard</html>\n")
    rebuilt = manifest_from_directory(component_tree)
    assert target.components["web"] == rebuilt.components["web"]
    assert target.verification["web"] != rebuilt.verification["web"]
    assert target.components["control_plane"] == packaged.components["control_plane"]


@pytest.mark.parametrize("contract, heading", [
    ("work_contract", "Your step and its report"),
    ("review_contract", "Your review and its verdict"),
])
def test_report_contract_changes_are_detected_from_target_sources(component_tree: Path, contract: str, heading: str):
    running = manifest_from_directory(component_tree)
    source = component_tree / "looper/agents/system_prompt.py"
    text = source.read_text()
    assert heading in text
    source.write_text(text.replace(heading, heading + " (updated)", 1))
    target = manifest_from_directory(component_tree)

    assert target.adoption[contract] != running.adoption[contract]
    assert target.adoption["seat_tools"] == running.adoption["seat_tools"]
    plan = decide(target, running, [], True)
    assert plan.kind is UpgradeKind.FULL


def test_seat_tool_schema_changes_are_detected_from_target_sources(component_tree: Path):
    running = manifest_from_directory(component_tree)
    source = component_tree / "looper/agents/ops/room.py"
    text = source.read_text()
    needle = '"maximum": 120'
    assert needle in text
    source.write_text(text.replace(needle, '"maximum": 119', 1))
    target = manifest_from_directory(component_tree)

    assert target.adoption["seat_tools"]["worker"] != running.adoption["seat_tools"]["worker"]
    assert target.adoption["seat_tools"]["reviewer"] != running.adoption["seat_tools"]["reviewer"]
    assert target.adoption["work_contract"] == running.adoption["work_contract"]
    assert decide(target, running, [], True).kind is UpgradeKind.FULL


@pytest.mark.parametrize("relative, expected", [
    ("docs/guide.md", UpgradeKind.NOOP),
    ("web/src/App.tsx", UpgradeKind.WEB_ONLY),
    ("web/index.html", UpgradeKind.WEB_ONLY),
    ("web/public/icon.svg", UpgradeKind.WEB_ONLY),
    ("web/package.json", UpgradeKind.WEB_ONLY),
    ("web/vite.config.ts", UpgradeKind.WEB_ONLY),
    ("web/tsconfig.app.json", UpgradeKind.WEB_ONLY),
    ("web/tailwind.config.js", UpgradeKind.WEB_ONLY),
    ("web/postcss.config.js", UpgradeKind.WEB_ONLY),
    ("pyproject.toml:dependencies", UpgradeKind.FULL),
    ("pyproject.toml:tools", UpgradeKind.NOOP),
    ("uv.lock", UpgradeKind.FULL),
    ("looper/agents/tiers/planner.py", UpgradeKind.FULL),
    ("same-version-wheel", UpgradeKind.NOOP),
])
def test_upgrade_rfc_examples_from_git_trees(component_tree: Path, relative: str, expected: UpgradeKind):
    running = manifest_from_git(component_tree, "HEAD")
    if relative == "same-version-wheel":
        source = component_tree / "web/src/App.tsx"
        source.parent.mkdir(parents=True)
        source.write_text("export default 'same wheel and git source';\n")
        dist = component_tree / "web/dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>same version</html>")
        record_web_source(component_tree, dist)
        _git(component_tree, "add", "web")
        _git(component_tree, "commit", "-q", "-m", "built dashboard")
        running = manifest_from_git(component_tree, "HEAD")
        wheel = component_tree / "looper.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            for path in (component_tree / "looper").rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(component_tree).as_posix())
            archive.writestr("looper/.runtime-fingerprint", runtime_fingerprint(component_tree))
            for path in dist.iterdir():
                archive.write(path, "looper/web/dashboard/" + path.name)
        assert decide(manifest_from_wheel(wheel), running, [], False).kind is expected
        return
    change = relative.partition(":")[2]
    relative = relative.partition(":")[0]
    target_file = component_tree / relative
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if change == "dependencies":
        target_file.write_text(target_file.read_text().replace("httpx>=0.27,<1", "httpx>=0.28,<1"))
    elif change == "tools":
        target_file.write_text(target_file.read_text() + '\n[tool.example]\nlevel = "debug"\n')
    else:
        target_file.write_text(target_file.read_text() + "\n# change\n" if target_file.exists() else "// changed\n")
    _git(component_tree, "add", relative)
    _git(component_tree, "commit", "-q", "-m", "upgrade candidate")
    target = manifest_from_git(component_tree, "HEAD")

    assert decide(target, running, [], False).kind is expected


@pytest.mark.parametrize("relative, components", [
    ("looper/agents/proc_tree.py", {"keeper", "control_plane"}),
    ("looper/child_env.py", {"keeper", "control_plane"}),
    ("looper/agents/keeper/main.py", {"keeper", "control_plane"}),
    ("runners/example/index.ts", {"runners", "control_plane"}),
])
def test_keeper_and_runner_files_also_invalidate_control_plane(component_tree: Path, relative: str, components: set[str]):
    before = manifest_from_directory(component_tree)
    target = component_tree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((target.read_text() if target.exists() else "") + "\n# changed component\n")
    after = manifest_from_directory(component_tree)
    changed = {name for name in before.components if before.components[name] != after.components[name]}
    assert changed == components


def test_all_tracked_files_are_classified():
    tracked = _git(ROOT, "ls-files", "-z").split("\0")
    check_coverage(path for path in tracked if path)


def test_unclassified_top_level_directory_fails_with_its_name():
    # A positive control proves that the test exercises the normal configuration.
    check_coverage(["docs/guide.md", "looper/agents/tiers/planner.py", "web/src/App.tsx"])
    with pytest.raises(ValueError, match="unclassified_component"):
        check_coverage(["unclassified_component/runtime.py"])


def test_adoption_seat_fingerprints_match_the_runtime_tool_listings():
    import hashlib
    import json

    from looper.agents.ops.room import tools_for
    from looper.agents.ops.vault import TOOLS as vault_tools
    from looper.agents.room import MemberKind

    adoption = manifest_from_directory(ROOT).adoption
    for role, member in (("worker", MemberKind.WORK), ("reviewer", MemberKind.REVIEW)):
        listing = [tool.listing() for tool in (*tools_for(member), *vault_tools)]
        expected = hashlib.sha256(json.dumps(listing, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert adoption["seat_tools"][role] == expected


def test_packaged_runner_aliases_have_the_same_fingerprints(component_tree: Path, tmp_path: Path):
    runner = component_tree / "runners/pi/src/main.ts"
    runner.parent.mkdir(parents=True)
    runner.write_text("export const runner = 'pi';\n")
    _git(component_tree, "add", "runners/pi/src/main.ts")
    _git(component_tree, "commit", "-q", "-m", "runner component")
    wheel = tmp_path / "looper.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in (component_tree / "looper").rglob("*.py"):
            archive.write(path, path.relative_to(component_tree).as_posix())
        archive.write(component_tree / "looper/components.toml", "looper/components.toml")
        archive.write(runner, "looper/agents/runtimes/runner/pi_runner/src/main.ts")
        archive.writestr("looper/.runtime-fingerprint", runtime_fingerprint(component_tree))
    assert manifest_from_git(component_tree, "HEAD").components == manifest_from_wheel(wheel).components


def test_broken_runner_alias_is_not_silently_ignored(component_tree: Path):
    alias = component_tree / "looper/agents/runtimes/runner/pi_runner"
    alias.symlink_to("../../../../runners/wrong")
    with pytest.raises(ValueError, match="invalid component alias target.*pi_runner"):
        manifest_from_directory(component_tree)
    _git(component_tree, "add", "looper/agents/runtimes/runner/pi_runner")
    _git(component_tree, "commit", "-q", "-m", "broken runner alias")
    with pytest.raises(ValueError, match="invalid component alias target.*pi_runner"):
        manifest_from_git(component_tree, "HEAD")


@pytest.mark.parametrize("relative", ["looper/cli.py", "looper/__init__.py", "looper/skill/looper-skill/SKILL.md"])
def test_cli_and_version_only_changes_do_not_restart(component_tree: Path, relative: str):
    before = manifest_from_directory(component_tree)
    target = component_tree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((target.read_text() if target.exists() else "") + "\n# CLI or version metadata\n")
    after = manifest_from_directory(component_tree)
    assert decide(after, before, [], False).kind is UpgradeKind.NOOP
