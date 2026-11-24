"""Measure DSH CLI/runner startup and RSS with a loopback fake model by default."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

# Also support `python evals/dsh_density.py` from a source checkout.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from looper.agents import proc_tree  # noqa: E402
from looper.agents.agent_settings import AgentSettings, AgentType, Launch  # noqa: E402
from looper.agents.profile_registry import (  # noqa: E402
    Endpoint, ModelSpec, ProviderSpec, ResolvedAgentProfile, ResolvedModel, RuntimeSpec,
)
from looper.agents.runtimes import ApiProtocol, AuthKind, VendorType  # noqa: E402
from looper.agents.runtimes import dsh  # noqa: E402
from looper.agents.runtimes.api.loop import ToolOutcome  # noqa: E402
from looper.agents.runtimes.api.adapter import KEY_ENV as RUNNER_KEY_ENV  # noqa: E402
from looper.agents.runtimes.dsh_runner import READ_ONLY_TOOLS, SESSION_CAP  # noqa: E402
from looper.agents.runtimes.pricing import BUILTIN_PRICES  # noqa: E402
from looper.agents.runtimes.runner import dsh_command  # noqa: E402
from looper.agents.runtimes.runner.session import Descriptor, RunnerSession  # noqa: E402
from looper.agents.runtimes.runner.supervisor import MAX_RUNNERS_PER_KIND, RunnerSupervisor  # noqa: E402
from tests.dsh_installed_stand_in import MessagesServer, NETWORK_GUARD, Reply  # noqa: E402

MIB = 1024 * 1024
PROMPT = 'Reply with exactly density-ok. Do not use tools.\n'
SYSTEM = 'This is a process density measurement. Return the requested literal response.'
KEY = 'synthetic-density-key'
# EMPTY_LIBRARY alone still enables CLI web fetch. Disable its services explicitly,
# together with independent model callers, for the same no-catalog/no-MCP workload.
DISABLED = ('web', 'web-search-deepseek', 'web-deepseek-api', 'web-fetch-http', 'tool-web',
            'skill', 'skill-filesystem', 'tool-skill', 'subagent', 'subagent-spawn-in-process',
            'subagent-fork-in-process', 'tool-subagent', 'tool-subagent-fork',
            'tool-subagent-control', 'tool-subagent-list-agents', 'ptc-runtime', 'workflow-ptc',
            'tool-workflow', 'goal', 'goal-round-driver', 'command-goal', 'tool-goal',
            'compaction-basic', 'command-compact', 'tool-bash', 'tool-pwsh', 'tool-jobs',
            'jobs', 'tool-todo', 'plan-mode', 'tool-working-directory')


class MeasurementError(RuntimeError):
    """Public-safe failure; child output and credentials never enter the report."""


class SampleWindow(threading.Event):
    """Keep a fake request in flight long enough to sample; startup uses init events."""

    def wait(self, timeout=None):
        super().wait(.25)
        return True


@dataclass
class ProcessRow:
    pid: int
    ppid: int
    rss_bytes: int
    name: str


def process_table() -> dict[int, ProcessRow]:
    result = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,rss=,comm='],
                            capture_output=True, text=True, timeout=5, check=True)
    rows = {}
    for line in result.stdout.splitlines():
        pid, parent, rss, name = line.strip().split(None, 3)
        rows[int(pid)] = ProcessRow(int(pid), int(parent), int(rss) * 1024, Path(name).name)
    if os.getpid() not in rows:
        raise MeasurementError('RSS process listing failed its positive control')
    return rows


def tree_sample(table: dict[int, ProcessRow], roots: set[int]) -> dict:
    selected = roots & table.keys()
    while added := {pid for pid, row in table.items() if row.ppid in selected} - selected:
        selected |= added
    processes = [dict(pid=pid, name=table[pid].name, rss_bytes=table[pid].rss_bytes,
                      category='dsh' if pid in roots else 'helper/platform-tool') for pid in sorted(selected)]
    root_rss = sum(row['rss_bytes'] for row in processes if row['category'] == 'dsh')
    helpers = sum(row['rss_bytes'] for row in processes if row['category'] != 'dsh')
    return dict(dsh_rss_bytes=root_rss, helper_rss_bytes=helpers,
                tree_rss_bytes=root_rss + helpers, processes=processes)


class Sampler:
    def __init__(self, interval: float):
        self.interval, self.roots, self.samples = interval, set(), []

    async def sample(self) -> dict:
        table = await asyncio.to_thread(process_table)
        row = tree_sample(table, set(self.roots))
        row['at_s'] = time.monotonic() - self.started
        self.samples.append(row)
        return row

    async def __aenter__(self):
        self.started = time.monotonic()
        async def watch():
            while True:
                await self.sample()
                await asyncio.sleep(self.interval)
        self.task = asyncio.create_task(watch())
        return self

    async def __aexit__(self, *unused):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task

    def summary(self, start: int = 0) -> dict:
        rows = self.samples[start:]
        if not rows or not any(row['tree_rss_bytes'] > 0 for row in rows):
            raise MeasurementError('No positive RSS samples for DSH')
        peak = max(rows, key=lambda row: row['tree_rss_bytes'])
        return dict(samples=rows, peak_tree_rss_bytes=peak['tree_rss_bytes'],
                    peak_dsh_rss_bytes=max(row['dsh_rss_bytes'] for row in rows),
                    peak_helper_rss_bytes=max(row['helper_rss_bytes'] for row in rows),
                    helpers=sorted({p['name'] for row in rows for p in row['processes']
                                    if p['category'] != 'dsh'}))


def isolated_env(root: Path, port: int, node: str) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    guard = root / 'network-guard.mjs'
    guard.write_text(NETWORK_GUARD)
    env = {'PATH': os.pathsep.join((str(Path(node).parent), os.defpath)),
           'LANG': 'en_US.UTF-8', 'NO_COLOR': '1', 'NODE_OPTIONS': f'--import={guard.as_uri()}',
           'LOCAL_NETWORK_LOG': str(root / 'network.jsonl'), 'LOCAL_MESSAGES_PORT': str(port),
           dsh.KEY_ENV: KEY}
    for key, path in dsh.DshHome(root / 'private', root).paths.items():
        Path(path).mkdir(parents=True, exist_ok=True)
        env[key] = path
    return env


def launch_for(executable: str, endpoint: str, model: str, effort: str) -> Launch:
    protocol = ApiProtocol.ANTHROPIC_MESSAGES
    account = ProviderSpec('density', 'Density', VendorType.CUSTOM, AuthKind.API_KEY, api_key=KEY)
    selected = ModelSpec('density-model', account.id, model, protocol)
    profile = ResolvedAgentProfile('density', 'Density',
        RuntimeSpec('density-dsh', 'DSH', 'dsh', executable, dsh.DshRuntime.capabilities), selected.id, effort,
        resolved_model=ResolvedModel(selected, account, Endpoint(account.id, protocol, endpoint)))
    return Launch(AgentSettings(profile=profile), role=AgentType.WORKER)


def cli_command(root: Path, executable: str, launch: Launch, *, workspace: Path | None = None) -> list[str]:
    prompt = root / '.local/prompts/system.md'
    prompt.parent.mkdir(parents=True)
    prompt.write_text(SYSTEM)
    command = dsh.DshRuntime().run_command(executable, cwd=workspace or root, prompt_file=prompt,
                                         launch=launch, session_dir=root / 'owner')
    patch = Path(command[command.index('--patch') + 1])
    overlay = json.loads(patch.read_text())
    overlay.extend({'id': name, 'disabled': True} for name in DISABLED)
    overlay.append({'id': 'tool-fs', 'config': {'read': True, 'readImage': False, 'write': False, 'edit': False}})
    patch.write_text(json.dumps(overlay))
    return command


class NoPlatformTools:
    async def specs(self):
        return []

    async def call(self, call):
        return ToolOutcome('No platform tools in density measurements', True, False)


class RunnerPool:
    def __init__(self, root, executable, node, env, sampler):
        self.sampler, self.spawns = sampler, []
        async def connect(process, kind):
            # Both modes start this clock as soon as spawn returns its process handle.
            record = dict(pid=process.pid, spawned_at=time.monotonic())
            self.spawns.append(record)
            sampler.roots.add(process.pid)
            bridge = await dsh_command.connect(process, kind)
            record['initialize_ms'] = (time.monotonic() - record['spawned_at']) * 1000
            record['baseline'] = await sampler.sample()
            return bridge
        safe = {k: v for k, v in env.items() if k not in {'PATH', 'LANG', 'HOME'}}
        self.supervisor = RunnerSupervisor(root, {'dsh': dsh_command.command(
            node, executable, dsh_command.state_root(root))}, session_cap=SESSION_CAP,
            env={'dsh': safe}, connect=connect)

    async def stop(self):
        await self.supervisor.stop()


def descriptor(prompt_file: Path, args, endpoint: str) -> Descriptor:
    return Descriptor(kind='dsh', role='worker', provider_id='density',
        protocol=ApiProtocol.ANTHROPIC_MESSAGES, base_url=endpoint, model=args.model, effort=args.effort,
        context_window=None, system_prompt_file=str(prompt_file), system_prompt_mode='append',
        reasoning=True, builtin=READ_ONLY_TOOLS, max_requests=1)


async def cli_step(root, args, executable, launch, env, sampler, index, *, idle=False):
    directory = root / f'cli-{index}'
    directory.mkdir()
    workspace = root.parent / 'workspace'
    command = cli_command(directory, executable, launch, workspace=workspace)
    start = time.monotonic()
    spawning = asyncio.create_task(proc_tree.spawn(*command, cwd=workspace, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        process = await spawning
        await proc_tree.end(process, label='cancelled density CLI spawn')
        raise
    spawned = time.monotonic()
    sampler.roots.add(process.pid)
    known = {}
    async def stderr():
        while await process.stderr.read(8192):
            pass
    drain = asyncio.create_task(stderr())
    async def finish():
        known.update(proc_tree.tree_of(process))
        try:
            await proc_tree.end(process, label='density CLI', known=known)
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            sampler.roots.discard(process.pid)
    if idle:
        return process, finish
    try:
        await dsh.DshRuntime().write_turn(process.stdin, PROMPT)
        first = None
        result = None
        async for event in dsh.DshRuntime().parse_process(process, model=args.model, launch=launch):
            if first is None:
                first = time.monotonic()
            if event.get('type') == 'result':
                result = event
            known.update(proc_tree.tree_of(process))
        if result is None or result.get('is_error') or process.returncode != 0:
            raise MeasurementError('CLI step failed; no successful result')
        return dict(step=index, startup='cold', step_to_first_event_ms=(first-start)*1000,
                    spawn_to_first_event_ms=(first-spawned)*1000)
    finally:
        await finish()


async def runner_step(pool, root, args, endpoint, env, index, *, idle=False):
    session = RunnerSession(f'density-{index}', pool.supervisor, 'dsh', owned=False)
    start = time.monotonic()
    prior = len(pool.spawns)
    prompt_file = root / 'runner-system.md'
    # Use the exact same appended instructions as CLI, including runtime notes.
    await session.start(descriptor(prompt_file, args, endpoint).to_argv(), cwd=str(root.parent / 'workspace'),
                        signature=index, env={RUNNER_KEY_ENV: KEY})
    if idle:
        return session
    try:
        session.set_turn_tools(NoPlatformTools())
        async def limits():
            return {'run_cap': None, 'task_cap': None, 'task_spent': None}
        session.set_turn_budget(provider_id='density', model=args.model, price=None, limits=limits, max_requests=1)
        first = None
        def event(value):
            nonlocal first
            if first is None:
                first = time.monotonic()
        turn = await session.run_turn(PROMPT, event)
        if turn.result_event is None or turn.result_event.get('is_error'):
            raise MeasurementError('Runner step failed; no successful result')
        cold = len(pool.spawns) > prior
        # Concurrent openers may share a spawn; identify the member owning this session.
        member = next(row for row in pool.spawns if row['pid'] == session._lease._generation.process.pid)
        return dict(step=index, startup='cold' if cold else 'warm', runner_id=session.runner_id,
                    step_to_first_event_ms=(first-start)*1000,
                    spawn_to_first_event_ms=(first-member['spawned_at'])*1000 if cold else None)
    finally:
        await session.stop()


def worker_capacity(mode: str, budget: int, peak: int, workers: int, base: int = 0) -> dict:
    if peak <= 0 or workers <= 0:
        raise MeasurementError('Cannot project density without positive active RSS')
    per_worker = peak / workers if mode == 'cli' else (peak-base) / workers
    if mode == 'cli':
        capacity = int(budget // per_worker)
    elif per_worker > 0:
        capacity = max((n for n in range(SESSION_CAP * MAX_RUNNERS_PER_KIND + 1)
                        if math.ceil(n / SESSION_CAP) * base + n * per_worker <= budget), default=0)
    else:
        capacity = None  # GC or sampling noise: no positive marginal cost was measured.
    return dict(estimated_workers=capacity, measured_workers=workers, budget_bytes=budget,
                member_base_bytes=base, incremental_worker_bytes=per_worker,
                assumption='linear RSS projection from measured concurrency; not a load-tested capacity',
                max_members=MAX_RUNNERS_PER_KIND if mode == 'runner' else None)


async def measure_mode(mode, root, args, executable, node, endpoint, env, launch):
    root.mkdir()
    prompt_file = root / 'runner-system.md'
    prompt_file.write_text(dsh._prompt_text(SYSTEM, launch))
    rows, checkpoints, cleanups = [], [], []
    pool = None
    async with Sampler(args.sample_interval) as sampler:
        try:
            if mode == 'runner':
                pool = RunnerPool(root / 'active', executable, node, env, sampler)
            # A cold single step gives an unambiguous spawn clock before parallel batches.
            async def step(index):
                async with asyncio.timeout(args.timeout):
                    if pool:
                        return await runner_step(pool, root, args, endpoint, env, index)
                    return await cli_step(root, args, executable, launch, env, sampler, index)
            rows.append(await step(0))
            cold_memory = sampler.summary()
            memory_start = len(sampler.samples)
            for offset in range(1, args.steps + 1, args.workers):
                tasks = [asyncio.create_task(step(i)) for i in range(offset, min(args.steps+1, offset+args.workers))]
                try:
                    rows.extend(await asyncio.gather(*tasks))
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            active = sampler.summary(memory_start)
            base = pool.spawns[0]['baseline']['tree_rss_bytes'] if pool else 0
            spawns = [{k: v for k, v in row.items() if k != 'spawned_at'} for row in pool.spawns] if pool else []
            if pool:
                await pool.stop()
                sampler.roots.clear()
                pool = RunnerPool(root / 'idle', executable, node, env, sampler)
            idle_start = len(sampler.samples)
            for index in range(args.idle_sessions):
                async with asyncio.timeout(args.timeout):
                    if pool:
                        session = await runner_step(pool, root, args, endpoint, env, index, idle=True)
                        cleanups.append(session.stop)
                        member = session.runner_id
                    else:
                        process, finish = await cli_step(root, args, executable, launch, env,
                                                        sampler, args.steps+1+index, idle=True)
                        cleanups.append(finish)
                        member = str(process.pid)
                await asyncio.sleep(args.idle_settle)
                if pool:
                    health = await session._lease.bridge.health()
                    if health['running'] != 0 or health['inflight_requests'] != 0:
                        raise MeasurementError('Idle runner unexpectedly executing requests')
                elif process.returncode is not None:
                    raise MeasurementError('Idle CLI process exited before sampling')
                snapshot = await sampler.sample()
                previous = checkpoints[-1] if checkpoints else None
                checkpoints.append(dict(sessions=index+1, member=member,
                    new_member=previous is None or member != previous['member'],
                    incremental_rss_bytes=snapshot['tree_rss_bytes']-previous['tree_rss_bytes']
                        if previous else None, **snapshot))
            idle = sampler.summary(idle_start)
            members = [row['member'] for row in checkpoints]
            boundary = None
            if pool and len(members) >= 33:
                boundary = len(set(members[:32])) == 1 and members[32] != members[0]
                if not boundary:
                    raise MeasurementError('Session 33 did not start a second runner member')
            final = checkpoints[-1]
            marginal = [row['incremental_rss_bytes'] for row in checkpoints
                        if row['incremental_rss_bytes'] is not None and (mode == 'cli' or not row['new_member'])]
            return dict(mode=mode, steps=rows, cold_memory=cold_memory, active_memory=active, idle_memory=idle,
                runner_spawns=spawns, idle_checkpoints=checkpoints,
                idle_kind='opened sessions' if pool else 'processes waiting for stdin; no Session created',
                idle_average_bytes=final['tree_rss_bytes']/args.idle_sessions,
                idle_marginal_bytes=statistics.median(marginal) if marginal else None,
                session_33_second_member=boundary,
                capacity=worker_capacity(mode, int(args.memory_budget_mib*MIB),
                    active['peak_tree_rss_bytes'], args.workers, base))
        finally:
            try:
                results = await asyncio.gather(*(close() for close in reversed(cleanups)), return_exceptions=True)
            finally:
                if pool:
                    await pool.stop()
            if any(isinstance(result, BaseException) for result in results):
                raise MeasurementError('One or more idle processes failed to clean up')


@contextmanager
def real_server(args):
    """Optional L2 gateway: reserve a conservative per-request ceiling before forwarding.

    The ceiling uses UTF-8 request bytes as input tokens and no cache discount.
    It is a budget admission bound at supplied prices, not a vendor billing cap.
    """
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        raise MeasurementError('L2 requires DEEPSEEK_API_KEY')
    price = BUILTIN_PRICES[args.model]
    rate = max(price.input_usd_per_mtok, price.cache_read_usd_per_mtok, price.cache_write_usd_per_mtok)
    state = {'reserved_usd': 0.0, 'requests': 0, 'errors': 0}
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass
        def do_POST(self):
            try:
                if self.path != '/v1/messages':
                    self.send_error(404)
                    return
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                body['max_tokens'] = min(body.get('max_tokens', 1024), 1024)
                payload = json.dumps(body).encode()
                ceiling = (len(payload)*rate + body['max_tokens']*price.output_usd_per_mtok)/1_000_000
                with lock:
                    if state['reserved_usd'] + ceiling > args.total_cap_usd:
                        self.send_error(402)
                        return
                    state['reserved_usd'] += ceiling
                    state['requests'] += 1
                request = urllib.request.Request(dsh.DEFAULT_BASE_URL + '/v1/messages', data=payload,
                    headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                             'content-type': 'application/json'})
                # No ambient proxy, redirects, cookie jar or credential lookup.
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, *unused):
                        return None
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                with opener.open(request, timeout=args.timeout) as response:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    while chunk := response.read1(8192):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    self.close_connection = True
            except Exception:
                with lock:
                    state['errors'] += 1
                self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def measure(args, port: int) -> dict:
    executable, node = shutil.which(args.dsh), shutil.which(args.node)
    if not executable or not node:
        raise MeasurementError('Installed dsh and Node are required')
    executable, node = str(Path(executable).absolute()), str(Path(node).absolute())
    with tempfile.TemporaryDirectory(prefix='looper-dsh-density-') as temporary:
        root = Path(temporary).resolve()
        (root / 'workspace').mkdir()
        env = isolated_env(root / 'env', port, node)
        endpoint = f'http://127.0.0.1:{port}'
        launch = launch_for(executable, endpoint, args.model, args.effort)
        rows = []
        for mode in ('cli', 'runner'):
            rows.append(await measure_mode(mode, root / mode, args, executable, node, endpoint, env, launch))
        network = [json.loads(line) for line in Path(env['LOCAL_NETWORK_LOG']).read_text().splitlines()]
        if any(row['kind'] == 'connect' and not row['allowed'] for row in network):
            raise MeasurementError('A child attempted a forbidden network connection')
        return dict(schema='looper-dsh-density/v1', status='passed', level='L2' if args.real else 'L1',
            model=args.model, effort=args.effort, prompt=PROMPT,
            appended_system_sha256=hashlib.sha256(dsh._prompt_text(SYSTEM, launch).encode()).hexdigest(),
            settings=dict(catalog_skills=False, mcp=False, web_fetch=False,
                          builtin=list(READ_ONLY_TOOLS), workers=args.workers, measured_steps=args.steps,
                          idle_sessions=args.idle_sessions, idle_settle_s=args.idle_settle,
                          fake_response_delay_s=None if args.real else .25,
                          session_cap=SESSION_CAP, max_runner_members=MAX_RUNNERS_PER_KIND),
            sampling=dict(interval_s=args.sample_interval, units='bytes', metric='RSS, includes shared pages per process',
                          excludes='measurement host and model server',
                          startup_clock='spawn handle available to first runtime event; warm spawn is null'), modes=rows)


def print_table(report):
    print('| 模式 | 首步 ms | 后续步骤中位 ms | DSH RSS MiB | 辅助进程 MiB | 空闲均摊 MiB | 估算 Workers |')
    print('|---|---:|---:|---:|---:|---:|---:|')
    for row in report['modes']:
        steps, mem = row['steps'], row['active_memory']
        print(f"| {row['mode']} | {steps[0]['step_to_first_event_ms']:.1f} | "
              f"{statistics.median(s['step_to_first_event_ms'] for s in steps[1:]):.1f} | "
              f"{mem['peak_dsh_rss_bytes']/MIB:.1f} | {mem['peak_helper_rss_bytes']/MIB:.1f} | "
              f"{row['idle_average_bytes']/MIB:.1f} | {row['capacity']['estimated_workers']} |")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steps', type=int, default=4, help='measured steps after one cold step')
    parser.add_argument('--workers', type=int, default=4, help='concurrent measured steps')
    parser.add_argument('--idle-sessions', type=int, default=33)
    parser.add_argument('--memory-budget-mib', type=float, default=8192)
    parser.add_argument('--sample-interval', type=float, default=.05)
    parser.add_argument('--idle-settle', type=float, default=.2)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--model', choices=tuple(BUILTIN_PRICES.keys() & dsh.MODEL_MAP.keys()), default='deepseek-flash')
    parser.add_argument('--effort', choices=dsh.EFFORT_LEVELS, default='high')
    parser.add_argument('--dsh', default='dsh')
    parser.add_argument('--node', default='node')
    parser.add_argument('--out', type=Path, default=ROOT / 'evals/runs/dsh-density.json')
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--total-cap-usd', type=float)
    args = parser.parse_args(argv)
    if args.steps < 1 or args.workers < 1 or args.workers > args.steps or args.workers > SESSION_CAP:
        parser.error('require 1 <= workers <= steps and workers <= session cap')
    if not 1 <= args.idle_sessions <= SESSION_CAP * MAX_RUNNERS_PER_KIND:
        parser.error('idle sessions must fit the runner pool')
    for name in ('memory_budget_mib', 'sample_interval', 'idle_settle', 'timeout'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f'{name} must be finite and positive')
    if args.real != (args.total_cap_usd is not None):
        parser.error('L2 requires both --real and --total-cap-usd; L1 accepts neither')
    if args.real and (not math.isfinite(args.total_cap_usd) or args.total_cap_usd <= 0):
        parser.error('total cap must be finite and positive')
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.real:
            with real_server(args) as (port, spending):
                report = asyncio.run(measure(args, port))
            report['budget'] = dict(total_cap_usd=args.total_cap_usd, **spending,
                prices=BUILTIN_PRICES[args.model].view(), hard_billing_cap=False)
        else:
            with MessagesServer([Reply(['density-ok'], hold=SampleWindow()) for _ in range(2*(args.steps+1))]) as server:
                report = asyncio.run(measure(args, server.port))
                expected = 2*(args.steps+1)
                if len(server.requests) != expected:
                    raise MeasurementError('Fake server request count differs from prompted steps')
                if any(row['body'] != server.requests[0]['body'] for row in server.requests[1:]):
                    raise MeasurementError('CLI and runner model requests are not identical')
                report['fake_requests'] = len(server.requests)
                report['idle_model_requests'] = 0  # exact count above includes only prompted steps
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        print_table(report)
    except Exception as error:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(dict(schema='looper-dsh-density/v1', status='failed',
            level='L2' if args.real else 'L1', error=type(error).__name__,
            reason=str(error) if isinstance(error, MeasurementError) else None), indent=2) + '\n')
        print(f'DSH 密度测量失败（{type(error).__name__}）；未生成成功报告。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
