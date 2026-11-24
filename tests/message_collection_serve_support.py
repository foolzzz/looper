"""Isolated public-CLI service, local accounts and a read-only collector wire fixture."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

from evals.prompt_regression.runner import Api, EvalError, free_port
from looper import ops_config
from looper.agents import operator_auth
from tests.fake_claude import fake_claude
from lark_cli_stand_in import _STAND_IN, BOT_OPEN_ID

ROOT = Path(__file__).resolve().parents[1]
_COLLECTOR = r'''
if name == "collector-synthetic":
    state = world["collector"]
    if args == ["auth", "status", "--json"]:
        while state.get("pause_auth") and os.path.exists(state["pause_auth"]):
            time.sleep(.05)
        answer({"state": state["auth"], "user_open_id": state["identity"], "refresh_expires_at": None})
    if state["auth"] not in ("ready", "needs_refresh"):
        refuse("fixture forbids reads while auth is not ready")
    rows = [dict(raw, chat_id=chat) for chat, items in state["messages"].items() for raw in items]
    if args[:4] == ["im", "+messages-search", "--as", "user"]:
        from datetime import datetime
        start = datetime.fromisoformat(args[args.index("--start")+1]).timestamp()*1000
        end = datetime.fromisoformat(args[args.index("--end")+1]).timestamp()*1000
        rows = [r for r in rows if start <= int(r["create_time"]) < end and r["message_id"] not in state.get("omit_search", [])]
        answer({"data": {"messages": rows, "has_more": False, "page_token": ""}})
    if args[:6] == ["api", "GET", "/open-apis/im/v1/messages", "--as", "user", "--params"]:
        params = json.loads(args[6]); container = params["container_id"]
        if params["container_id_type"] == "chat":
            rows = [r for r in rows if r["chat_id"] == container and not r.get("parent_id")]
            rows = [r for r in rows if int(params["start_time"])*1000 <= int(r["create_time"]) < int(params["end_time"])*1000]
        else:
            rows = [r for r in rows if r.get("thread_id") == container]
        rows = sorted(rows, key=lambda r: (int(r["create_time"]), r["message_id"]), reverse=params["sort_type"] == "ByCreateTimeDesc")
        answer({"data": {"items": rows, "has_more": False, "page_token": ""}})
    if args[:4] == ["im", "+chat-list", "--as", "user"]:
        answer({"data": {"chats": [{"chat_id": c, "chat_mode": "group", "name": "Synthetic group"} for c in state["messages"]], "has_more": False, "page_token": ""}})
    if args[:4] == ["contact", "+search-user", "--as", "user"]:
        answer({"data": {"users": [{"open_id": value, "name": "Synthetic person"} for value in args[args.index("--user-ids")+1].split(",")]}})
    refuse("collector command outside the local wire contract")
'''

_MODEL = r'''
import json, sys
if '--version' in sys.argv:
    print('2.1.329 (offline runtime)'); sys.exit(0)
if sys.argv[1:] == ['auth', '--help']:
    print('Usage: claude auth status'); sys.exit(0)
if sys.argv[1:3] == ['auth', 'status']:
    print('{"loggedIn":true}'); sys.exit(0)
if '--output-format' not in sys.argv:
    sys.exit(76)
print(json.dumps({'type':'system','subtype':'init','session_id':'collection-local-model',
                  'tools':[],'mcp_servers':[]}), flush=True)
for line in sys.stdin:
    turn = json.loads(line)
    if turn.get('type') != 'user':
        continue
    print(json.dumps({'type':'user','message':turn.get('message'),'uuid':turn.get('uuid')}), flush=True)
    print(json.dumps({'type':'assistant','session_id':'collection-local-model','message':{'id':'reply-synthetic',
        'role':'assistant','content':[{'type':'text','text':'Synthetic local response.'}]}}),flush=True)
    print(json.dumps({'type':'result','subtype':'success','session_id':'collection-local-model',
        'result':'Synthetic local response.','is_error':False,'total_cost_usd':0.0,'num_turns':1,
        'usage':{'input_tokens':1,'output_tokens':1}}),flush=True)
'''


def install_fixtures(root, world):
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = _STAND_IN.format(world=str(world), log=str(root / "lark-calls.jsonl"), bot=BOT_OPEN_ID)
    script = script.replace('key = None\n', _COLLECTOR + '\nkey = None\n', 1)
    fake_claude(bin_dir / "lark-cli", script)
    fake_claude(bin_dir / "claude", _MODEL)
    for name in ("codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "telegram"):
        fake_claude(bin_dir / name, "import sys\nsys.exit(76)\n")
    return bin_dir


def assert_target(config, runtime, api):
    parsed = ops_config.load(ops_config.Flags(config=config, runtime_root=runtime))
    assert parsed.host == "127.0.0.1" and parsed.port != 8767
    assert urlsplit(api.base_url).port == parsed.port
    return parsed


@contextmanager
def serving(root, database_url, world, *, port=None):
    parsed = urlsplit(database_url)
    assert parsed.hostname in ("127.0.0.1", "localhost") and parsed.port in (54331, 5432)
    assert unquote(parsed.path.lstrip("/")) not in ("looper", "loopx", "postgres")
    runtime, home = root / "runtime", root / "home"
    for directory in (runtime, home):
        directory.mkdir(parents=True, exist_ok=True)
    bin_dir = install_fixtures(root, world)
    port = port or free_port()
    assert port != 8767
    ops = ops_config.OpsConfig(runtime_root=runtime, path=runtime / ops_config.FILE,
        database_url=database_url, host="127.0.0.1", port=port, operator_notes_root=str(home / "notes"))
    ops_config.write_private(ops.path, ops_config.render(ops))
    assert ops.path.stat().st_mode & 0o777 == 0o600
    env = {"PATH": os.pathsep.join((str(bin_dir), "/usr/bin", "/bin", "/usr/sbin", "/sbin")),
           "HOME": str(home), "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1",
           "UV_PYTHON": sys.executable, "UV_NO_SYNC": "1", "CLAUDE_CONFIG_DIR": str(home / ".claude")}
    uv = shutil.which("uv")
    assert uv is not None
    log_path = root / "serve.log"
    with log_path.open("a") as log:
        process = subprocess.Popen([uv, "run", "--no-sync", "--python", sys.executable, "looper", "serve",
            "--config", str(ops.path), "--runtime-root", str(runtime), "--skip-backup"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        api = Api(f"http://127.0.0.1:{port}", timeout=10)
        try:
            deadline = time.monotonic() + 60
            while True:
                assert_target(ops.path, runtime, api)
                if process.poll() is not None:
                    raise AssertionError("isolated service exited: " + log_path.read_text()[-3000:])
                try:
                    api.token = operator_auth.read_token(runtime)
                    if not api.token:
                        raise EvalError("awaiting local credential")
                    api.post("/api/v1/system/status", {})
                    break
                except (OSError, EvalError):
                    assert time.monotonic() < deadline, log_path.read_text()[-3000:]
                    time.sleep(.1)
            yield api, ops
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                    raise AssertionError("isolated service failed to drain")


def until(check, *, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        assert time.monotonic() < deadline, "isolated service did not reach the expected state"
        time.sleep(.1)
