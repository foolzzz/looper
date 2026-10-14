"""More ways into the catalog against a real PostgreSQL: upload, git and Settings, each through the HTTP API.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own. Git tests clone local file:// fixture
repositories (the server itself accepts https:// only); nothing reads or
writes the operator's ~/.claude.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_db import DSN, _call, _import, _projects, _run

pytestmark = [
    pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
]

SKILL = "---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n\n{body}\n"


def _skill(name: str = "stamp", body: str = "Reply STAMP-OK.", description: str = "Stamps releases.") -> str:
    return SKILL.format(name=name, description=description, body=body)


def _b64(data: bytes | str) -> str:
    return base64.b64encode(data.encode("utf-8") if isinstance(data, str) else data).decode("ascii")


def _zip(entries: dict[str, str | bytes], *, links: dict[str, str] | None = None,
         modes: dict[str, int] | None = None) -> bytes:
    """A zip of ``entries`` (path to content); ``links`` adds symbolic links, ``modes`` sets permission bits."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFREG | (modes or {}).get(path, 0o644)) << 16
            archive.writestr(info, content)
        for path, target in (links or {}).items():
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


async def _upload(filename: str, data: bytes | str, name: str | None = None) -> tuple[int, Any]:
    body: dict[str, Any] = {"filename": filename, "content_base64": _b64(data)}
    if name is not None:
        body["name"] = name
    return await _call("POST", "/api/catalog/upload", body)


async def _items() -> list[dict[str, Any]]:
    _, catalog = await _call("GET", "/api/catalog")
    return catalog["items"]


async def _skill_md(item_id: str) -> str:
    _, detail = await _call("GET", f"/api/catalog/{item_id}")
    return detail["skill_md"]


def _git(cwd: Path, *args: str) -> str:
    """Run git for a fixture, without the operator's git configuration."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _repo(root: Path, files: dict[str, str | bytes], *, executable: tuple[str, ...] = ()) -> Path:
    """A git repository at ``root`` with ``files`` committed."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _commit(root, files, executable=executable)
    return root


def _commit(root: Path, files: dict[str, str | bytes], *, executable: tuple[str, ...] = ()) -> str:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        if relative in executable:
            path.chmod(0o755)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return _git(root, "rev-parse", "HEAD")


REPO_FILES: dict[str, str | bytes] = {
    "README.md": "# skills\n",
    "skills/alpha/SKILL.md": _skill("alpha", "Reply ALPHA-OK."),
    "skills/alpha/scripts/run.sh": "#!/bin/sh\necho alpha\n",
    "skills/alpha/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
    "skills/beta/SKILL.md": _skill("beta", "Reply BETA-OK."),
    "skills/template/SKILL.md": _skill("template-skill", "Fill me in."),
    "skills/broken/SKILL.md": "no frontmatter here\n",
    "plugins/demo/.claude-plugin/plugin.json": json.dumps({"name": "demo", "version": "1.0.0",
                                                            "description": "A demo plugin."}),
    "plugins/demo/skills/hello/SKILL.md": _skill("hello", "Say hello."),
    "plugins/demo/commands/greet.md": "Greet.\n",
}


def _clones(scratch: Path) -> list[str]:
    """The clone directories left in ``scratch``."""
    return [p.name for p in scratch.iterdir() if p.name.startswith("loopx-git-")]


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary directories (the clones) go here, so a test can see that none is left behind; file:// URLs
    are allowed."""
    from looper.agents import catalog_sources

    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(directory))
    monkeypatch.setattr(catalog_sources, "ALLOW_FILE_URLS", True)
    return directory


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path / "origin", REPO_FILES, executable=("skills/alpha/scripts/run.sh",))
    os.symlink("/etc/hosts", root / "skills" / "beta" / "hosts")  # a link out of the item
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "link")
    return root


# --- Upload ---


def test_an_uploaded_skill_enters_the_catalog_installed_nowhere_and_loads_where_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents.claude_cli import library_args
    from looper.agents.library import load_installed

    async def _test(pool: Any) -> None:
        status, body = await _upload("SKILL.md", _skill())
        assert status == 201, body
        item = body["item"]
        assert (item["kind"], item["name"], item["origin"], item["qualified_name"]) == (
            "skill", "stamp", "upload", "personal:stamp")
        assert (item["description"], item["source"], item["installs"], item["modified"]) == (
            "Stamps releases.", "SKILL.md", [], False)
        zipped = _zip({"tool/SKILL.md": _skill("tool", "Run scripts/go.sh."), "tool/scripts/go.sh": "echo go\n"},
                      modes={"tool/scripts/go.sh": 0o755})
        status, body = await _upload("tool.zip", zipped)
        assert status == 201 and body["item"]["file_count"] == 2, body
        tool = body["item"]["item_id"]
        root = tmp_path / "runtime"
        assert (await load_installed(root, "worker")).items == ()  # in the catalog, installed nowhere
        status, _ = await _call("POST", "/api/installs", {"item_id": tool, "scope": "global", "roles": ["worker"]})
        assert status == 201
        worker = await load_installed(root, "worker")
        (plugin,) = library_args(worker)[1::2]
        assert library_args(worker) == ["--plugin-dir", plugin] and plugin.endswith("/personal")
        script = Path(plugin) / "skills" / "tool" / "scripts" / "go.sh"
        assert script.read_text() == "echo go\n" and os.access(script, os.X_OK)
        assert (Path(plugin) / "skills" / "tool" / "SKILL.md").read_text().endswith("Run scripts/go.sh.\n")
        assert (await load_installed(root, "planner")).items == ()

    _run(_test, monkeypatch)


def test_an_uploaded_plugin_zip_enters_whole_and_its_binary_files_are_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        zipped = _zip({
            "demo/.claude-plugin/plugin.json": json.dumps({"name": "demo", "version": "2.0.0",
                                                           "description": "Demo\nplugin."}),
            "demo/skills/hello/SKILL.md": _skill("hello"),
            "demo/commands/greet.md": "Greet.\n",
            "demo/assets/logo.png": b"\x89PNG\r\n\x1a\n\x00binary",
            "__MACOSX/demo/._greet.md": b"\x00\x05",
            "demo/.DS_Store": b"\x00",
        })
        status, body = await _upload("demo.zip", zipped)
        assert status == 201, body
        item = body["item"]
        assert (item["kind"], item["name"], item["version"], item["description"], item["file_count"]) == (
            "plugin", "demo", "2.0.0", "Demo plugin.", 3)
        assert body["skipped"] == [{"item": "demo.zip", "path": "assets/logo.png", "reason": "binary",
                                    "size_bytes": 15}]
        _, detail = await _call("GET", f"/api/catalog/{item['item_id']}")
        assert [f["path"] for f in detail["files"]] == [
            ".claude-plugin/plugin.json", "commands/greet.md", "skills/hello/SKILL.md"]

    _run(_test, monkeypatch)


def test_a_name_taken_outside_projects_is_refused_with_409_and_an_upload_can_be_renamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        await _import(pool, "skill:stamp", "plugin:demo")
        calc, = await _projects("calc")
        assert (await _call("POST", f"/api/projects/{calc}/skills", {
            "name": "notes", "description": "A project's own.", "content": "Body."}))[0] == 201
        status, body = await _upload("SKILL.md", _skill())
        assert status == 409
        assert body["error"] == ("the catalog already has a skill named 'stamp' (imported from this machine); "
                                 "give this one another name")
        status, body = await _upload("SKILL.md", _skill(), name="stamp-two")
        assert status == 201 and body["item"]["qualified_name"] == "personal:stamp-two"
        assert "\nname: stamp-two\n" in await _skill_md(body["item"]["item_id"])
        assert "license: MIT" in await _skill_md(body["item"]["item_id"])  # other frontmatter stays
        # A project's own skill loads as project:<name>; an upload of its name does not clash with it.
        assert (await _upload("SKILL.md", _skill("notes")))[0] == 201
        plugin = _zip({".claude-plugin/plugin.json": json.dumps({"name": "demo"}), "README.md": "x\n"})
        assert (await _upload("demo.zip", plugin))[0] == 409
        status, body = await _upload("demo.zip", plugin, name="demo-two")
        assert status == 201
        _, detail = await _call("GET", f"/api/catalog/{body['item']['item_id']}")
        assert detail["item"]["qualified_name"] == "demo-two"
        for bad, reason in (("Bad Name", "must be lowercase letters"), ("personal", "a plugin LoopX generates")):
            status, body = await _upload("demo.zip", plugin, name=bad)
            assert status == 400 and reason in body["error"], body

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("entries", "links", "reason"), [
    ({"/etc/SKILL.md": _skill()}, None, "absolute"),
    ({"C:\\skill\\SKILL.md": _skill()}, None, "absolute or Windows path"),
    ({"skill/../../SKILL.md": _skill()}, None, "leaves its directory"),
    ({"SKILL.md": _skill()}, {"secret": "/etc/passwd"}, "symbolic link"),
    ({"SKILL.md": _skill(), "big.txt": "x" * 2048}, None, "a file has at most 1024"),
    ({"SKILL.md": _skill(), **{f"f{i}.md": "x" for i in range(5)}}, None, "more than 4 files"),
    ({"SKILL.md": _skill(), "a.txt": "x" * 900, "b.txt": "x" * 900}, None, "more than 1536 bytes"),
    ({"notes/readme.md": "hello\n"}, None, "neither SKILL.md (a skill) nor .claude-plugin/plugin.json"),
    ({"SKILL.md": "no frontmatter\n"}, None, "has no frontmatter"),
    ({"SKILL.md": "---\nname: x\n---\nBody.\n"}, None, "no description in its frontmatter"),
    ({"SKILL.md": b"\xff\xfe\x00binary"}, None, "has no SKILL.md (a text file at its top)"),
    ({".claude-plugin/plugin.json": "{not json"}, None, "is not valid JSON"),
    ({".claude-plugin/plugin.json": json.dumps({"version": "1"})}, None, "not a JSON object with a name"),
])
def test_an_unsafe_or_invalid_zip_is_refused_whole_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, entries: dict[str, str | bytes], links: dict[str, str] | None, reason: str,
) -> None:
    from looper.agents import catalog_sources

    monkeypatch.setattr(catalog_sources, "FILE_MAX_BYTES", 1024)
    monkeypatch.setattr(catalog_sources, "ITEM_MAX_FILES", 4)
    monkeypatch.setattr(catalog_sources, "ITEM_MAX_BYTES", 1536)

    async def _test(pool: Any) -> None:
        status, body = await _upload("skill.zip", _zip(entries, links=links))
        assert status == 400 and reason in body["error"], body
        assert await _items() == []

    _run(_test, monkeypatch)


def test_an_upload_that_is_not_a_skill_md_or_zip_or_over_its_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import catalog_sources
    from looper.agents.http_server import REQUEST_BODY_MAX_BYTES

    # The largest upload, as base64 in its JSON body, stays under the server's request cap.
    assert 4 * -(-catalog_sources.UPLOAD_MAX_BYTES // 3) + 1024 < REQUEST_BODY_MAX_BYTES
    monkeypatch.setattr(catalog_sources, "UPLOAD_MAX_BYTES", 100)

    async def _test(pool: Any) -> None:
        cases = [
            (await _upload("skill.zip", b"PK\x03\x04 not really"), "not a valid zip file"),
            (await _upload("skill.tar", b"x"), "upload a SKILL.md or a .zip"),
            (await _upload("SKILL.md", "---\ndescription: d\n---\nBody.\n"), "has no name in its frontmatter"),
            (await _upload("SKILL.md", b"\xff\xfe binary"), "is not UTF-8 text"),
            (await _upload("SKILL.md", "x" * 101), "an upload has at most 100 bytes"),
            (await _call("POST", "/api/catalog/upload", {"filename": "SKILL.md", "content_base64": "%%%"}),
             "is not valid base64"),
            (await _call("POST", "/api/catalog/upload", {"filename": "SKILL.md", "content_base64": "eA==",
                                                         "extra": 1}), "unknown field 'extra'"),
        ]
        for (status, body), reason in cases:
            assert status == 400 and reason in body["error"], (reason, body)
        assert await _items() == []

    _run(_test, monkeypatch)


# --- Git ---


def test_a_git_preview_lists_what_the_repository_holds_and_leaves_no_clone_behind(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, repo: Path,
) -> None:
    async def _test(pool: Any) -> None:
        await _import(pool, "skill:beta")
        status, body = await _call("POST", "/api/catalog/git/preview", {"url": repo.as_uri()})
        assert status == 200, body
        assert body["commit"] == _git(repo, "rev-parse", "HEAD") and (body["ref"], body["subpath"]) == (None, "")
        assert [(i["path"], i["kind"], i["name"], i["status"]) for i in body["items"]] == [
            ("plugins/demo", "plugin", "demo", "new"),
            ("skills/alpha", "skill", "alpha", "new"),
            ("skills/beta", "skill", "beta", "conflict"),
            ("skills/template", "skill", "template-skill", "new"),  # the name its SKILL.md gives
        ]
        assert body["items"][2]["reason"] == (
            "the catalog already has a skill named 'beta' (imported from this machine); give this one another name")
        assert {(s["item"], s["path"], s["reason"]) for s in body["skipped"]} == {
            ("skills/alpha", "logo.png", "binary"), ("skills/beta", "hosts", "link out of the item"),
            ("skills/broken", "", "the SKILL.md of skills/broken has no frontmatter: it starts with a --- line, "
                                  "then name: and description:, then ---")}
        status, body = await _call("POST", "/api/catalog/git/preview", {"url": repo.as_uri(),
                                                                        "subpath": "skills/alpha"})
        assert status == 200 and [(i["path"], i["name"]) for i in body["items"]] == [(".", "alpha")]
        assert _clones(scratch) == []
        assert [i["name"] for i in await _items()] == ["beta"]  # a preview adds nothing

    _run(_test, monkeypatch)


def test_a_git_import_adds_the_chosen_items_and_importing_again_updates_them_in_place(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, repo: Path,
) -> None:
    async def _test(pool: Any) -> None:
        url = repo.as_uri()
        commit = _git(repo, "rev-parse", "HEAD")
        status, body = await _call("POST", "/api/catalog/git/import", {
            "url": url + "/", "commit": commit,
            "items": [{"path": "skills/alpha"}, {"path": "plugins/demo", "name": "demo-kit"}]})
        assert status == 200, body
        assert (body["added"], body["updated"], body["unchanged"]) == (["skill:alpha", "plugin:demo-kit"], [], [])
        alpha, kit = body["items"]
        assert (alpha["origin"], alpha["source"], alpha["installs"]) == ("git", f"{url}@{commit}", [])
        assert alpha["file_count"] == 2 and kit["qualified_name"] == "demo-kit" and kit["version"] == "1.0.0"
        _, detail = await _call("GET", f"/api/catalog/{alpha['item_id']}")
        assert {f["path"]: f["executable"] for f in detail["files"]} == {"SKILL.md": False, "scripts/run.sh": True}
        _, detail = await _call("GET", f"/api/catalog/{kit['item_id']}")
        assert detail["item"]["name"] == "demo-kit"
        install = (await _call("POST", "/api/installs", {"item_id": alpha["item_id"], "scope": "global"}))[1]

        # A new commit changes alpha; an edit since the last import is marked and then replaced.
        assert (await _call("PUT", f"/api/catalog/{alpha['item_id']}", {"content": "Edited."}))[1]["item"][
            "modified"] is True
        newer = _commit(repo, {"skills/alpha/SKILL.md": _skill("alpha", "Reply ALPHA-2.")})
        _, preview = await _call("POST", "/api/catalog/git/preview", {"url": url})
        statuses = {i["path"]: (i["status"], i.get("modified")) for i in preview["items"]}
        assert statuses["skills/alpha"] == ("update", True) and statuses["plugins/demo"] == ("new", None)
        status, body = await _call("POST", "/api/catalog/git/import", {
            "url": url, "commit": newer, "items": [{"path": "skills/alpha"}, {"path": "skills/beta"}]})
        assert status == 200, body
        assert (body["added"], body["updated"]) == (["skill:beta"], ["skill:alpha"])
        again = body["items"][0]
        assert again["item_id"] == alpha["item_id"] and again["modified"] is False
        assert again["source"] == f"{url}@{newer}" and again["installs"] == [install["install"]]
        assert (await _skill_md(alpha["item_id"])).endswith("Reply ALPHA-2.\n")
        _, preview = await _call("POST", "/api/catalog/git/preview", {"url": url})
        assert {i["path"]: i["status"] for i in preview["items"]}["skills/alpha"] == "unchanged"
        assert _clones(scratch) == []

    _run(_test, monkeypatch)


def test_a_git_import_refuses_a_clash_a_moved_ref_and_an_unknown_path(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, repo: Path,
) -> None:
    async def _test(pool: Any) -> None:
        url = repo.as_uri()
        commit = _git(repo, "rev-parse", "HEAD")
        assert (await _upload("SKILL.md", _skill("alpha")))[0] == 201
        status, body = await _call("POST", "/api/catalog/git/import", {
            "url": url, "items": [{"path": "skills/alpha"}, {"path": "skills/beta"}]})
        assert status == 409
        assert body["error"] == "the catalog already has a skill named 'alpha' (uploaded); give this one another name"
        status, body = await _call("POST", "/api/catalog/git/import", {
            "url": url, "items": [{"path": "skills/alpha", "name": "beta"}, {"path": "skills/beta"}]})
        assert status == 409 and body["error"] == "two of the chosen items are both named skill 'beta'; give one " \
                                                  "another name"
        assert [i["name"] for i in await _items()] == ["alpha"]  # nothing of a refused import is added
        _commit(repo, {"README.md": "# moved\n"})
        status, body = await _call("POST", "/api/catalog/git/import", {
            "url": url, "commit": commit, "items": [{"path": "skills/beta"}]})
        assert status == 409 and "moved from" in body["error"] and "preview it again" in body["error"]
        for path, reason in (("skills/broken", "has no frontmatter"),
                             ("skills/nowhere", "the repository has no plugin or skill at skills/nowhere")):
            status, body = await _call("POST", "/api/catalog/git/import", {"url": url, "items": [{"path": path}]})
            assert status == 400 and reason in body["error"], body
        status, body = await _call("POST", "/api/catalog/git/import", {
            "url": url, "items": [{"path": "skills/alpha", "name": "alpha-from-git"}]})
        assert status == 200 and body["added"] == ["skill:alpha-from-git"]
        assert _clones(scratch) == []

    _run(_test, monkeypatch)


@pytest.mark.parametrize(("fields", "reason"), [
    ({"url": "http://github.com/anthropics/skills"}, "must start with https://"),
    ({"url": "git@github.com:anthropics/skills.git"}, "must start with https://"),
    ({"url": "ssh://git@github.com/anthropics/skills"}, "must start with https://"),
    ({"url": "/tmp/skills"}, "must start with https://"),
    ({"url": "https://user:token@github.com/anthropics/skills"}, "put no credentials"),
    ({"url": "https://github.com/anthropics/skills?x=1"}, "query or a fragment"),
    ({"url": "https://github.com/a b"}, "holds whitespace"),
    ({"url": "https://github.com/anthropics/skills", "ref": "--upload-pack=touch"}, "ref is a branch or tag"),
    ({"url": "https://github.com/anthropics/skills", "ref": "main/../x"}, "ref is a branch or tag"),
    ({"url": "https://github.com/anthropics/skills", "subpath": "skills/../../etc"}, "without . or .. parts"),
])
def test_a_repository_that_is_not_plain_https_is_refused_before_anything_is_cloned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fields: dict[str, Any], reason: str,
) -> None:
    from looper.agents import catalog_sources

    async def _never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("nothing is cloned")

    monkeypatch.setattr(catalog_sources, "_clone", _never)

    async def _test(pool: Any) -> None:
        for path, body in (("/api/catalog/git/preview", fields),
                           ("/api/catalog/git/import", {**fields, "items": [{"path": "."}]})):
            status, answer = await _call("POST", path, body)
            assert status == 400 and reason in answer["error"], answer

    _run(_test, monkeypatch)
    # A file:// URL is accepted only where tests allow it.
    assert catalog_sources.ALLOW_FILE_URLS is False
    with pytest.raises(catalog_sources.CatalogError, match="must start with https://"):
        catalog_sources.git_source({"url": (tmp_path / "repo").as_uri()})


def test_a_clone_runs_no_hooks_fetches_no_submodules_and_reads_only_the_tree(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, repo: Path, tmp_path: Path,
) -> None:
    # A submodule holding a skill.
    _repo(tmp_path / "inner", {"SKILL.md": _skill("inner")})
    _git(repo, "submodule", "add", "-q", (tmp_path / "inner").as_uri(), "vendor/inner")
    _git(repo, "commit", "-q", "-m", "submodule")
    # The operator's own git configuration runs a post-checkout hook on every clone: a plain clone runs it.
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    (hooks / "post-checkout").chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\thooksPath = {hooks}\n[protocol \"file\"]\n\tallow = always\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    subprocess.run(["git", "clone", "-q", repo.as_uri(), str(tmp_path / "plain")], check=True)
    assert marker.exists()
    marker.unlink()

    async def _test(pool: Any) -> None:
        status, body = await _call("POST", "/api/catalog/git/preview", {"url": repo.as_uri()})
        assert status == 200, body
        assert not marker.exists()
        assert "inner" not in {i["name"] for i in body["items"]}
        assert _clones(scratch) == []

    _run(_test, monkeypatch)


def test_a_clone_over_its_size_or_time_is_stopped_and_removed(
    monkeypatch: pytest.MonkeyPatch, scratch: Path, repo: Path, tmp_path: Path,
) -> None:
    from looper.agents import catalog_sources

    async def _test(pool: Any) -> None:
        monkeypatch.setattr(catalog_sources, "GIT_CLONE_MAX_BYTES", 1024)
        status, body = await _call("POST", "/api/catalog/git/preview", {"url": repo.as_uri()})
        assert status == 400 and "when cloned; the clone was stopped" in body["error"], body
        assert _clones(scratch) == []
        monkeypatch.setattr(catalog_sources, "GIT_CLONE_MAX_BYTES", 64 * 1024 * 1024)
        # A git that never finishes.
        slow = tmp_path / "bin"
        slow.mkdir()
        (slow / "git").write_text("#!/bin/sh\nexec sleep 30\n")
        (slow / "git").chmod(0o755)
        monkeypatch.setenv("PATH", f"{slow}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(catalog_sources, "GIT_CLONE_TIMEOUT_SECONDS", 1)
        status, body = await _call("POST", "/api/catalog/git/preview", {"url": repo.as_uri()})
        assert status == 400 and "took more than 1 seconds and was stopped" in body["error"], body
        assert _clones(scratch) == []
        assert subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True).returncode != 0

    _run(_test, monkeypatch)


# --- Settings: write, change and delete ---


def test_a_skill_written_in_settings_is_created_edited_and_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _test(pool: Any) -> None:
        status, body = await _call("POST", "/api/catalog/skills", {
            "name": "release-notes", "description": 'Drafts notes: "short"', "content": "Write them."})
        assert status == 201, body
        item = body["item"]
        assert (item["origin"], item["qualified_name"], item["installs"]) == ("ui", "personal:release-notes", [])
        assert await _skill_md(item["item_id"]) == (
            '---\nname: release-notes\ndescription: "Drafts notes: \\"short\\""\n---\n\nWrite them.\n')
        install = (await _call("POST", "/api/installs", {"item_id": item["item_id"], "scope": "global"}))[1]
        status, body = await _call("PUT", f"/api/catalog/{item['item_id']}", {"name": "notes", "content": "New."})
        assert status == 200, body
        assert (body["item"]["name"], body["item"]["modified"]) == ("notes", False)
        assert body["item"]["installs"] == [install["install"]]
        _, detail = await _call("GET", f"/api/catalog/{item['item_id']}")
        assert detail["content"] == "New.\n" and detail["skill_md"].startswith("---\nname: notes\n")
        assert (await _call("POST", "/api/catalog/skills", {"name": "notes", "description": "d",
                                                             "content": "c"}))[0] == 409
        for fields, reason in (({"name": "Bad Name"}, "must be lowercase"),
                               ({"description": "two\nlines"}, "is one line"),
                               ({"content": "---\nname: x\n---\n"}, "without frontmatter"),
                               ({}, "give at least one of"), ({"enabled": True}, "unknown field")):
            status, answer = await _call("PUT", f"/api/catalog/{item['item_id']}", fields)
            assert status == 400 and reason in answer["error"], answer
        status, body = await _call("DELETE", f"/api/catalog/{item['item_id']}")
        assert (status, body) == (200, {"deleted": item["item_id"]})
        assert await _items() == [] and await pool.fetchval("SELECT count(*) FROM library_installs") == 0
        assert (await _call("DELETE", f"/api/catalog/{item['item_id']}"))[0] == 404

    _run(_test, monkeypatch)


def test_editing_an_uploaded_skill_keeps_its_other_files_and_marks_it_modified_and_others_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _test(pool: Any) -> None:
        ids = await _import(pool, "skill:mine", "plugin:kit")
        calc, = await _projects("calc")
        _, own = await _call("POST", f"/api/projects/{calc}/skills",
                             {"name": "own", "description": "Own.", "content": "Body."})
        zipped = _zip({"SKILL.md": SKILL.format(name="tool", description="|-\n  Two\n  lines.", body="Old."),
                       "scripts/go.sh": "echo go\n"})
        _, body = await _upload("tool.zip", zipped)
        tool = body["item"]
        assert tool["description"] == "Two lines."
        status, body = await _call("PUT", f"/api/catalog/{tool['item_id']}",
                                   {"description": "One line now.", "content": "New body."})
        assert status == 200 and body["item"]["modified"] is True, body
        assert await _skill_md(tool["item_id"]) == (
            '---\nname: tool\ndescription: "One line now."\nlicense: MIT\n---\n\nNew body.\n')
        _, detail = await _call("GET", f"/api/catalog/{tool['item_id']}")
        assert sorted(f["path"] for f in detail["files"]) == ["SKILL.md", "scripts/go.sh"]
        plugin = (await _upload("p.zip", _zip({".claude-plugin/plugin.json": '{"name": "p"}'})))[1]["item"]
        refusals = [
            (ids["mine"], "PUT", "not edited here: they follow its Claude Code configuration"),
            (own["skill"]["skill_id"], "PUT", "edited in its project's Skills tab"),
            (own["skill"]["skill_id"], "DELETE", "deleted from its project's Skills tab"),
            (plugin["item_id"], "PUT", "a plugin is not edited here"),
        ]
        for item_id, method, reason in refusals:
            status, answer = await _call(method, f"/api/catalog/{item_id}", {"content": "x"} if method == "PUT" else None)
            assert status == 400 and reason in answer["error"], answer
        assert (await _call("PUT", f"/api/catalog/{tool['item_id']}", {"name": "mine"}))[0] == 409
        assert (await _call("DELETE", f"/api/catalog/{plugin['item_id']}"))[0] == 200

    _run(_test, monkeypatch)


def test_a_machine_import_leaves_out_a_name_another_source_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from library_fixtures import fake_claude_home

    from looper.agents.library import import_from_machine

    async def _test(pool: Any) -> None:
        assert (await _upload("SKILL.md", _skill("alpha")))[0] == 201
        view = await import_from_machine(fake_claude_home(tmp_path))
        assert {(i["name"], i["origin"]) for i in view["items"] if i["name"] == "alpha"} == {("alpha", "upload")}
        assert {"item": "alpha", "path": "", "reason": "the catalog has a skill of this name from another source "
                "(upload)", "size_bytes": None} in view["last_import"]["skipped"]
        assert "skill:beta" in view["last_import"]["added"]

    _run(_test, monkeypatch)
