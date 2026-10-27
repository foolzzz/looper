"""Test-only runner registration and isolated real-service launcher.

Nothing registers on import. The child bootstrap alone installs the adapter;
the operator's service and the production runtime registry remain untouched.
"""
# ruff: noqa: E402 -- the bootstrap resolves this worktree before importing it.
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.prompt_regression.runner import Api, EvalError, free_port
from looper import ops_config
from looper.agents import operator_auth, proc_tree


def _disposable_url(database_url: str) -> None:
    url = urlsplit(database_url)
    if (url.scheme != "postgresql" or url.hostname != "127.0.0.1" or url.port != 54329
            or url.username != "postgres" or url.password != "postgres"
            or not url.path.startswith("/looper_eval_") or url.query or url.fragment):
        raise ValueError("runner serve tests require their own disposable local database")


def _register(supervisor) -> None:
    from looper.agents.runtimes import ADAPTERS, Probe, PromptBlock, RuntimeUnsupported
    from looper.agents.runtimes.api.adapter import ApiRuntime
    from looper.agents.runtimes.runner.session import Descriptor, RunnerSession

    class TestRunner(ApiRuntime):
        type = "test-runner"
        label = "Test runner"
        default_executable = ""  # Explicit DB registration, never auto-detection.
        roles = frozenset({"manager", "planner"})
        in_process = False
        platform_tools_in_process = True
        isolation_id = "test-runner-v1"

        def probe(self, executable):
            return Probe("0.1.0", self.capabilities)

        def prompt_blocks(self, launch, looper_prompt, *, cwd=None):
            return [PromptBlock("Looper instructions", "looper", looper_prompt, "the request's instructions")]

        def resident_command(self, executable, *, cwd, prompt_file, launch, max_turns=0):
            if launch.role is None or launch.role.value not in self.roles:
                raise RuntimeUnsupported("the test runner supports Manager and Planner only")
            provider = launch.profile.provider
            self.bind_provider(provider, launch.model, launch.effort)
            return Descriptor(kind="fake", role=launch.role.value, provider_id=provider.id,
                protocol=next(iter(provider.protocols)), base_url=provider.base_url,
                model=launch.model, effort=launch.effort,
                context_window=provider.catalog[0].context_window,
                system_prompt_file=str(Path(prompt_file).absolute()),
                add_dirs=(*launch.knowledge.add_dirs, *launch.read_dirs),
                read_only=launch.read_only).to_argv()

        def resident_session(self, label):
            return RunnerSession(label, supervisor, "fake")

        def run_command(self, *args, **kwargs):
            raise RuntimeUnsupported("the test runner has no Worker or Reviewer")

    ADAPTERS[TestRunner.type] = TestRunner()


async def _bootstrap(port: int, runtime_root: Path, work_dir: Path) -> None:
    import looper
    from looper.agents import serve
    from looper.agents.runtimes.runner.bridge import RunnerBridge
    from looper.agents.runtimes.runner.supervisor import RunnerSupervisor

    print(f"runner test sys.executable={sys.executable}", flush=True)
    print(f"runner test looper.__file__={looper.__file__}", flush=True)
    assert Path(looper.__file__).resolve().parent == ROOT / "looper"

    async def connect(process, kind):
        print("runner test runner=" + json.dumps({"pid": process.pid,
              "started": proc_tree._start_time(process.pid)}), flush=True)
        bridge = RunnerBridge(process.stdout, process.stdin, kind, control_timeout=10)
        try:
            await bridge.initialize(permitted=("turn",))
        except BaseException:
            await bridge.close()
            raise
        return bridge

    supervisor = RunnerSupervisor(runtime_root,
        {"fake": [sys.executable, "-u", str(ROOT / "runners/fake/main.py")]}, connect=connect)
    _register(supervisor)

    async def no_bots():
        return []

    async def no_scheduler(self):
        pass

    # Test-local isolation only: no external channels or scheduled work.
    serve._enabled_bot_rows = no_bots
    serve.Scheduler.start = no_scheduler
    ops = ops_config.load(ops_config.Flags(runtime_root=runtime_root, host="127.0.0.1", port=port))
    _disposable_url(ops.database_url)
    serve._configure_logging("info")
    await serve._run_agent_system(ops=ops, runtime_root=runtime_root,
                                 work_dir=work_dir, skip_backup=True, runner_supervisor=supervisor)


@contextmanager
def serving(*, database_url: str, runtime_root: Path, home: Path, work_dir: Path,
            log_path: Path, startup_timeout: float = 45):
    """Run the real serve system with exactly one test-owned runner supervisor."""
    _disposable_url(database_url)
    runtime_root, home, work_dir = (Path(p).absolute() for p in (runtime_root, home, work_dir))
    for directory in (runtime_root, home, work_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    port = free_port()
    assert port != 8767
    ops = ops_config.OpsConfig(runtime_root=runtime_root, path=runtime_root / ops_config.FILE,
        database_url=database_url, host="127.0.0.1", port=port,
        operator_notes_root=str(home / "notes"))
    ops_config.write_private(ops.path, ops_config.render(ops))
    guard = ROOT / "tests/runtime_root_guard"
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
           "PYTHONPATH": os.pathsep.join((str(guard), str(ROOT))), "PYTHONUNBUFFERED": "1",
           **{k: v for k, v in os.environ.items() if k.startswith("PYTEST_LOOPER_")}}
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()),
            str(port), str(runtime_root), str(work_dir)], cwd=work_dir, env=env,
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        api = Api(f"http://127.0.0.1:{port}", timeout=10)
        try:
            deadline = time.monotonic() + startup_timeout
            while True:
                if process.poll() is not None:
                    raise EvalError(f"runner test service exited ({process.returncode}); see {log_path.name}")
                try:
                    api.token = operator_auth.read_token(runtime_root)
                    if not api.token:
                        raise EvalError("waiting for operator credential")
                    api.post("/api/v1/system/status", {})["result"]
                    break
                except (OSError, EvalError):
                    if time.monotonic() >= deadline:
                        raise EvalError("runner test service startup timed out") from None
                    time.sleep(0.1)
            yield api
        finally:
            descendants = proc_tree._tree(process, proc_tree._snapshot(), {})
            identities = proc_tree._start_times(descendants)
            identity_checks = identities is not None
            if identities is None:
                identities = {}
            runners = [json.loads(line.removeprefix("runner test runner="))
                       for line in log_path.read_text(encoding="utf-8").splitlines()
                       if line.startswith("runner test runner=")]
            identities.update({row["pid"]: row["started"] for row in runners if row["started"]})

            def live_owned():
                nonlocal identity_checks
                live = proc_tree._snapshot()
                current = proc_tree._start_times(identities)
                if current is None:
                    identity_checks = False
                    return set()
                return {pid for pid, started in identities.items()
                        if pid in live and current.get(pid) == started}

            normal_stop = True
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    normal_stop = False
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            stopped_after_sigint = live_owned()
            remaining = set(stopped_after_sigint)
            for pid in remaining:
                if proc_tree._start_time(pid) != identities[pid]:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 5
            while remaining and time.monotonic() < deadline:
                time.sleep(0.05)
                remaining = live_owned()
            log.write("runner test cleanup=" + json.dumps({"service_pid": process.pid,
                "runners": runners, "owned_pids": sorted(identities),
                "normal_stop": normal_stop, "stopped_after_sigint": sorted(stopped_after_sigint),
                "after_cleanup": sorted(remaining), "identity_checks": identity_checks}) + "\n")
            log.flush()
            assert process.poll() is not None, "runner test service was not reaped"
            assert identity_checks, "could not verify runner test process identities"
            assert not remaining, f"runner test descendants survived cleanup: {sorted(remaining)}"
            assert normal_stop and not stopped_after_sigint, (
                f"runner test shutdown required forced cleanup: {sorted(stopped_after_sigint)}")


if __name__ == "__main__":
    asyncio.run(_bootstrap(int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
