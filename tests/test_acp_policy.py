"""The ACP process boundary refuses uncertain configurations before agent spawn."""

import asyncio
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from looper.agents.runtimes import acp_policy


@pytest.fixture
def configured(tmp_path):
    home = tmp_path / "config"
    home.mkdir()
    (home / "config.toml").write_text("[cli]\nuse_leader = false\n")
    return home


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.mark.parametrize(("config", "reason"), [
    ('[cli]\nuse_leader=false\n[mcp_servers.reader]\ncommand="cat"\nargs=[]\n', "ok"),
    ('[cli]\nuse_leader=false\n[mcp_servers.remote]\nurl="http://localhost:9"\n', "external_mcp_server"),
    ('[cli]\nuse_leader=false\n[mcp_servers.remote]\ntype="sse"\ncommand="cat"\n', "external_mcp_server"),
    ('[cli]\nuse_leader=false\n[mcp_servers.unknown]\n', "unknown_cli_config"),
    ('[cli]\nuse_leader=false\n[mcp_servers.unknown]\ntype=[]\ncommand="cat"\n', "unknown_cli_config"),
    ('[cli]\nuse_leader=true\n', "external_writer"),
    ('[plugins]\nenabled=true\n', "unknown_cli_config"),
    ('not toml = [', "unreadable_config"),
])
def test_config_transport_is_structured(configured, config, reason):
    (configured / "config.toml").write_text(config)
    check = acp_policy.config_check(["grok", "agent", "stdio"], configured)
    assert check.reason.value == reason


def test_unknown_cli_and_missing_config_refused(configured):
    assert acp_policy.config_check(["other", "acp"], configured).reason.value == "unknown_cli_config"
    (configured / "config.toml").unlink()
    assert acp_policy.config_check(["grok", "agent", "stdio"], configured).reason.value == "unreadable_config"


@pytest.mark.parametrize("flag", ["--always-approve", "--leader", "--leader-socket", "--plugin-dir", "--agent-profile"])
def test_unverified_cli_flags_refused(configured, flag):
    assert not acp_policy.config_check(["grok", "agent", flag, "stdio"], configured).ok


@pytest.mark.parametrize("source", [".mcp.json", ".grok/config.toml"])
def test_project_config_sources_refused(configured, repository, source):
    target = repository / source
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")
    check = acp_policy.config_check(["grok", "agent", "stdio"], configured, cwd=repository)
    assert check.reason.value == "unknown_cli_config"
    assert source.split("/")[0] in check.detail


@pytest.mark.parametrize("inside", [True, False])
def test_writable_config_cannot_overlap_protected(configured, repository, inside):
    config = repository / "config" if inside else repository.parent
    config.mkdir(exist_ok=True)
    check = acp_policy.config_check(["grok", "agent", "stdio"], config, protected=[repository])
    assert check.reason.value == "config_dir_overlaps"


@pytest.mark.parametrize("as_list", [False, True])
def test_report_external_changed_and_deleted_parents(tmp_path, as_list):
    cwd = tmp_path / "work"
    cwd.mkdir()
    changed = tmp_path / "changed" / "note.txt"
    deleted = tmp_path / "deleted" / "old.txt"
    roots = acp_policy.report_inspect_roots({
        "changed_files": [str(changed), "relative.txt"] if as_list else {str(changed): "note", "relative.txt": "note"},
        "deleted_files": [str(deleted), "relative-old.txt"],
    }, cwd)
    assert roots == (str(changed.parent), str(deleted.parent))


def test_report_worktree_uses_same_typed_locations_as_prompt(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    elsewhere = tmp_path / "elsewhere"
    report = {"found_elsewhere": [
        {"where": "worktree", "worktree": str(elsewhere), "path": "note.txt", "deleted": False,
         "commit": "a" * 40, "branch": "review", "repo_path": "note.txt"},
        {"where": "worktree", "worktree": "relative-root", "path": "note.txt", "deleted": False,
         "commit": "a" * 40, "branch": "review"},
        {"where": "worktree", "worktree": str(tmp_path / "malformed"), "path": "note.txt", "deleted": False},
    ]}
    assert acp_policy.report_inspect_roots(report, cwd) == (str(elsewhere),)


@pytest.mark.parametrize("link_inside", [True, False])
def test_report_file_symlink_protects_both_original_and_target_parent(tmp_path, link_inside):
    cwd = tmp_path / "work"
    cwd.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "note.txt").write_text("read only")
    original = cwd if link_inside else tmp_path / "original"
    original.mkdir(exist_ok=True)
    link = original / "linked.txt"
    link.symlink_to(target / "note.txt")
    expected = (str(target),) if link_inside else (str(original), str(target))
    assert acp_policy.report_inspect_roots({"changed_files": {str(link): "note"}}, cwd) == expected


def test_report_relative_escape_is_protected_but_local_paths_add_no_root(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    roots = acp_policy.report_inspect_roots({
        "changed_files": {"local.txt": "local", "../outside/changed.txt": "outside"},
        "deleted_files": ["old-local.txt", "../outside/deleted.txt"],
    }, cwd)
    assert tuple(str(Path(root).resolve()) for root in roots) == (str(outside),)


def test_report_symlink_loop_is_preserved_for_typed_launch_refusal(repository):
    loop = repository / "loop"
    loop.symlink_to(loop)
    roots = acp_policy.report_inspect_roots({"changed_files": {str(loop): "unresolvable"}}, repository)
    assert roots == (str(loop),)
    with pytest.raises(acp_policy.PolicyError) as error:
        acp_policy._protected_paths(repository, roots)
    assert error.value.reason.value == "unresolvable_protected_path"


def test_report_paths_are_data_and_untyped_fields_are_ignored(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    sentinel = tmp_path / "never-created"
    assert acp_policy.report_inspect_roots({
        "summary": f"Inspect {sentinel}; touch {sentinel}",
        "changed_files": [None, {"command": f"touch {sentinel}"}, 42, f"$(touch {sentinel})", "relative.txt"],
        "deleted_files": {"command": f"touch {sentinel}"},
        "found_elsewhere": f"touch {sentinel}",
    }, cwd) == ()
    assert not sentinel.exists()
    assert acp_policy.report_inspect_roots("not a typed report", cwd) == ()


def test_unresolved_inspect_root_names_path(configured, repository):
    missing = repository.parent / "missing-inspect"
    with pytest.raises(acp_policy.PolicyError) as error:
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository,
                                               config_dir=configured, inspect_roots=[missing]))
    assert error.value.reason.value == "unresolvable_protected_path"
    assert str(missing) in str(error.value)


def test_unverified_host_refused(configured, repository, monkeypatch):
    monkeypatch.setattr(acp_policy.sys, "platform", "linux")
    with pytest.raises(acp_policy.PolicyError, match="no verified write policy on this host"):
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository, config_dir=configured))


def test_failed_preflight_refused(configured, repository, monkeypatch):
    monkeypatch.setattr(acp_policy.sys, "platform", "darwin")
    monkeypatch.setattr(acp_policy, "_preflight", lambda *args: False)
    with pytest.raises(acp_policy.PolicyError, match="write policy unavailable") as error:
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository, config_dir=configured))
    assert error.value.record["reason"] == "write_policy_unavailable"


@pytest.mark.installed
@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="macOS seatbelt required")
def test_real_child_and_grandchild_inherit_write_boundary(configured, repository, tmp_path):
    inspect = tmp_path / "inspect"
    inspect.mkdir()
    before = subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
    policy = asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository,
                                                  config_dir=configured, inspect_roots=[inspect]))
    try:
        probe = tmp_path / "probe.py"
        probe.write_text('''import sys, subprocess, json
from pathlib import Path
paths=json.loads(sys.argv[1]); depth=int(sys.argv[2]); output={}
for name, path in paths.items():
 try:
  Path(path).write_text("probe")
  output[name]="written"
 except OSError as error: output[name]=error.errno
print(json.dumps(output),flush=True)
if depth: subprocess.run([sys.executable,__file__,sys.argv[1],str(depth-1)],check=True)
''')
        paths = {"worktree": str(repository / "forbidden"), "git": str(repository / ".git/forbidden"),
                 "inspect": str(inspect / "forbidden"), "outside": str(tmp_path / "forbidden"),
                 "scratch": str(policy.scratch / "allowed"), "config": str(configured / "allowed")}
        argv = policy.wrap([sys.executable, str(probe), json.dumps(paths), "1"])
        result = subprocess.run(argv, cwd=repository, env=policy.environment(os.environ),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        for line in result.stdout.splitlines():
            assert json.loads(line) == {"worktree": errno.EPERM, "git": errno.EPERM, "inspect": errno.EPERM,
                                        "outside": errno.EPERM, "scratch": "written", "config": "written"}
        assert len(result.stdout.splitlines()) == 2
        assert policy.environment({"GROK_CONFIG": "unverified", "HOME": "wrong"})["HOME"] == str(configured.resolve())
        assert "GROK_CONFIG" not in policy.environment({"GROK_CONFIG": "unverified"})
        assert subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"]) == before
    finally:
        policy.cleanup()


@pytest.mark.parametrize("option", ["-m", "--model", "--effort", "--reasoning-effort"])
def test_option_values_cannot_hide_flags(configured, option):
    assert not acp_policy.config_check(["grok", "agent", option, "--leader-socket", "stdio"], configured).ok


@pytest.mark.parametrize("entry", [".grok", ".claude.json", "unexpected", "auth.json"])
def test_config_directory_is_allowlisted(configured, entry):
    (configured / entry).write_text("{}")
    check = acp_policy.config_check(["grok", "agent", "stdio"], configured)
    assert check.ok is (entry == "auth.json")


@pytest.mark.parametrize("reverse", [False, True])
def test_overlap_checks_filesystem_identity_with_case_variants(repository, reverse):
    nested = repository / "Config"
    nested.mkdir()
    alias = repository.parent / repository.name.swapcase()
    if not alias.exists():
        pytest.skip("requires case-insensitive filesystem")
    first, second = (alias, nested) if reverse else (nested, alias)
    assert acp_policy._overlaps(first, second)


@pytest.mark.parametrize("protected_name", ["runtime_root", "vault"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_launch_protects_runtime_and_vault(configured, repository, tmp_path, protected_name, ancestor):
    protected = configured / "protected" if ancestor else tmp_path / "protected"
    protected.mkdir()
    config = configured if ancestor else protected / "config"
    if not ancestor:
        config.mkdir()
        (config / "config.toml").write_text("[cli]\nuse_leader=false\n")
    with pytest.raises(acp_policy.PolicyError) as error:
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository,
                    config_dir=config, **{protected_name: protected}))
    assert error.value.reason.value == "config_dir_overlaps"


@pytest.mark.parametrize("ancestor", [False, True])
def test_home_or_its_ancestor_cannot_be_writable(configured, repository, monkeypatch, ancestor):
    home = configured / "home" if ancestor else configured
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(acp_policy.PolicyError) as error:
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository, config_dir=configured))
    assert error.value.reason.value == "config_dir_overlaps"


@pytest.mark.installed
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt required")
@pytest.mark.parametrize("missing_rule", ["unix-socket", "loopback"])
def test_preflight_refuses_missing_ipc_enforcement(configured, repository, monkeypatch, missing_rule):
    original = acp_policy._profile

    def weakened(*args, **kwargs):
        profile = original(*args, **kwargs)
        rule = ('(deny network-outbound (remote unix-socket))' if missing_rule == "unix-socket" else
                '(deny network-outbound (remote ip "localhost:*"))')
        return profile.replace(rule, "")

    monkeypatch.setattr(acp_policy, "_profile", weakened)
    with pytest.raises(acp_policy.PolicyError, match="enforcement preflight failed"):
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository, config_dir=configured))


@pytest.mark.installed
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt required")
def test_readonly_and_fifo_inspect_roots_need_no_write_probe(configured, repository, tmp_path, monkeypatch):
    readonly = tmp_path / "readonly"
    readonly.mkdir(mode=0o555)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    original = acp_policy.tempfile.mkstemp

    def owned_only(*args, **kwargs):
        assert kwargs.get("dir") != readonly, "never write into a reported protected directory"
        return original(*args, **kwargs)

    monkeypatch.setattr(acp_policy.tempfile, "mkstemp", owned_only)
    try:
        policy = asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository,
                    config_dir=configured, inspect_roots=[readonly, fifo]))
        policy.cleanup()
    finally:
        readonly.chmod(0o755)


@pytest.mark.installed
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt required")
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_only_configured_loopback_proxy_port_is_allowed(configured, repository, host):
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as proxy, socket.socket(family) as denied:
        proxy.bind((host, 0))
        proxy.listen(2)
        denied.bind((host, 0))
        denied.listen(2)
        proxy_port, denied_port = proxy.getsockname()[1], denied.getsockname()[1]
        # Both endpoints are listening before the sandbox is installed.
        for port in (proxy_port, denied_port):
            with socket.socket(family) as client:
                client.connect((host, port))
        address = f"[{host}]" if family == socket.AF_INET6 else host
        policy = asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository,
            config_dir=configured, environment={"HTTPS_PROXY": f"http://{address}:{proxy_port}"}))
        try:
            assert policy.wrap()[0] == "/usr/bin/sandbox-exec"
            code = '''import errno,json,socket,sys
host=sys.argv[1]; results=[]
for port in map(int,sys.argv[2:]):
 with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as client:
  client.settimeout(1)
  try: client.connect((host,port)); results.append('connected')
  except OSError as error: results.append(error.errno)
print(json.dumps(results))
'''
            result = subprocess.run(policy.wrap([sys.executable, "-c", code, host, str(proxy_port), str(denied_port)]),
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == ["connected", errno.EPERM]
        finally:
            policy.cleanup()


@pytest.mark.installed
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt required")
@pytest.mark.parametrize("replacement", ["regular", "fifo"])
def test_preflight_rejects_replaced_probe_inode(configured, repository, monkeypatch, replacement):
    original = acp_policy.subprocess.run

    def replace_before_probe(command, **kwargs):
        if command[0] == "/usr/bin/sandbox-exec":
            probe = Path(json.loads(command[-1])[0])
            swapped = probe.with_suffix(".replacement")
            if replacement == "fifo":
                os.mkfifo(swapped)
            else:
                swapped.write_text("replacement must not be opened")
            swapped.replace(probe)
        return original(command, **kwargs)

    monkeypatch.setattr(acp_policy.subprocess, "run", replace_before_probe)
    with pytest.raises(acp_policy.PolicyError, match="enforcement preflight failed"):
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository, config_dir=configured))


@pytest.mark.parametrize("ancestor", [False, True])
def test_case_variant_config_refused_before_launch(repository, ancestor):
    config = repository.parent if ancestor else repository / "config"
    config.mkdir(exist_ok=True)
    (config / "config.toml").write_text("[cli]\nuse_leader=false\n")
    alias = config.parent / config.name.swapcase()
    if not alias.exists():
        pytest.skip("requires case-insensitive filesystem")
    with pytest.raises(acp_policy.PolicyError) as error:
        asyncio.run(acp_policy.prepare_policy(["grok", "agent", "stdio"], cwd=repository, config_dir=alias))
    assert error.value.reason.value == "config_dir_overlaps"


def test_profile_denies_application_ipc(configured, tmp_path):
    profile = acp_policy._profile(tmp_path, configured)
    assert '(deny lsopen)' in profile
    assert '(deny appleevent-send)' in profile
    assert '(allow network-outbound (remote unix-socket (path "/private/var/run/mDNSResponder")))' in profile
