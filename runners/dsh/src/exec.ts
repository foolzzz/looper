/** Session-owned shell groups and synchronous native-file boundaries. */
import { fork, spawn, type ChildProcess } from 'node:child_process';
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { diagnostic } from '../../common/rpc.ts';
import { basename, dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Session } from './session.ts';

const TOOL_DEADLINES = JSON.parse(readFileSync(new URL('./tool-deadlines.json', import.meta.url), 'utf8'));
const KILL_GRACE_MS = 2000, DRAIN_MS = 500;
const executors = new WeakMap<Session, Set<ChildProcess>>();
// A detached group can outlive the runner before the supervisor's first tree
// snapshot. Its spawning owner therefore survives the runner and owns cleanup
// on IPC EOF, including a runner killed before the pid acknowledgement arrives.
if (process.argv[2] === '--looper-bash-executor' && process.send) {
  let pid: number | undefined;
  const stop = () => { if (pid !== undefined) signalGroup(pid, 'SIGKILL'); process.exit(0); };
  process.on('disconnect', stop);
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
  process.on('SIGHUP', stop);
  process.once('message', (input: any) => {
    if (!process.connected) return;
    const child = spawn('/bin/bash', ['--noprofile', '--norc', '-c', input.command], {
      cwd: input.cwd, env: input.env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    pid = child.pid;
    if (pid !== undefined) process.send!({ pid }, error => { if (error) stop(); });
    // One ordered IPC stream keeps the final output ahead of the exit result.
    for (const stream of [child.stdout, child.stderr]) stream.on('data', (data: Buffer) => {
      stream.pause();
      process.send!({ output: data.toString('base64') }, error => { if (error) stop(); else stream.resume(); });
    });
    let finished = false, drain: ReturnType<typeof setTimeout> | undefined;
    const finish = (result: object) => {
      if (finished) return;
      finished = true;
      clearTimeout(drain);
      // Stop collecting even if a descendant escaped the process group. Closing
      // these handles lets the call return without waiting for pipe EOF.
      child.stdout.destroy();
      child.stderr.destroy();
      const reap = () => {
        try { if (pid !== undefined) process.kill(-pid, 0); else return stop(); }
        catch (error) { if ((error as NodeJS.ErrnoException).code === 'ESRCH') { pid = undefined; return stop(); } }
        setTimeout(reap, 100);
      };
      process.send!(result, error => { if (error) stop(); else setTimeout(reap, 100); });
    };
    child.once('error', error => finish({ error: error.message }));
    child.once('exit', (code, signal) => {
      process.send!({ phase: 'shell_exit', code, signal }, error => { if (error) stop(); });
      drain = setTimeout(() => finish({ code, signal, background_output: true }), DRAIN_MS);
    });
    child.once('close', (code, signal) => finish({ code, signal }));
  });
}

function canonical(path: string): string {
  try { return realpathSync.native(path); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    try { if (lstatSync(path).isSymbolicLink()) throw new Error('dangling symlink'); }
    catch (e) { if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e; }
    const parent = dirname(path);
    if (parent === path || basename(path) === '..') throw error;
    return join(canonical(parent), basename(path));
  }
}
export function checkedPath(record: Session, path: string): string {
  const denied = () => new Error(`LOOPER_PATH: ${path} is outside the task directory and its added directories`);
  if (typeof path !== 'string' || path.startsWith('~') || !path.trim()) throw denied();
  // dsh-tool-fs uses dsh-fs-local.localDisplayPath/resolveLocalTarget:
  // preserve physical symlink/.. traversal, then realpath the longest existing prefix.
  const raw = isAbsolute(path) ? path : `${record.cwd}${sep}${path}`;
  const display = /(?:^|[\\/])\.\.(?:[\\/]|$)/u.test(raw) ? raw : resolve(record.cwd, path);
  let target: string;
  try { target = canonical(display); } catch { throw denied(); }
  if (!record.roots.some(root => target === root || target.startsWith(root.endsWith(sep) ? root : root + sep))) throw denied();
  return target;
}
export function pathGuard(record: Session, exec: { name: string; arguments: any }): string | undefined {
  if (!['read', 'write', 'edit', 'glob', 'grep'].includes(exec.name)) return;
  try {
    const args = exec.arguments;
    checkedPath(record, ['glob', 'grep'].includes(exec.name) ? args.path ?? '.' : args.file_path);
    const pattern = exec.name === 'glob' ? args.pattern : exec.name === 'grep' ? args.include : undefined;
    if (typeof pattern === 'string' && (isAbsolute(pattern) || pattern.split(/[\\/]/).includes('..'))) {
      return `LOOPER_PATH: ${pattern} is outside the task directory and its added directories`;
    }
  } catch (error) { return (error as Error).message; }
}
function signalGroup(pid: number, signal: NodeJS.Signals): void {
  try { process.kill(-pid, signal); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error; }
}
function trackGroup(record: Session, pid: number, onGone: () => void): void {
  try { process.kill(-pid, 0); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ESRCH') { record.processes.delete(pid); onGone(); return; }
  }
  // A shell may exit while a redirected background child still owns its group.
  setTimeout(() => trackGroup(record, pid, onGone), 100).unref();
}
export function killProcesses(record: Session): void {
  for (const pid of record.processes) signalGroup(pid, 'SIGKILL');
  for (const executor of executors.get(record) ?? []) {
    if (executor.connected) executor.disconnect();
  }
}
export function registerBash(record: Session, createExecutor: () => ChildProcess = () =>
  fork(fileURLToPath(import.meta.url), ['--looper-bash-executor'], {
    env: {}, detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] })): void {
  if (!record.builtin.includes('bash')) return;
  const owned = new Set<ChildProcess>();
  executors.set(record, owned);
  record.agentCtx.tools.register({ name: 'bash',
    description: 'Run a bash command in the task directory and return its combined output and exit code.',
    parameters: { type: 'object', properties: { description: { type: 'string' }, command: { type: 'string' },
      timeoutMs: { type: 'number' }, workdir: { type: 'string' } }, required: ['description', 'command'] },
    output: { schema: { type: 'string' }, render: (_args: any, value: string) => [{ type: 'text', text: value }] },
    isConcurrencySafe: () => false,
    execute: (args: any, exec: { signal: AbortSignal; callId: string }) => {
      const cwd = checkedPath(record, args.workdir ?? '.');
      const ceiling = Math.min(record.limits.tool_timeout_s, TOOL_DEADLINES.max_s) * 1000;
      const timeoutMs = Math.min(Number.isFinite(args.timeoutMs) && args.timeoutMs > 0
        ? args.timeoutMs : ceiling, ceiling);
      const startedAt = Date.now(), started = performance.now();
      const span = { session_id: record.id, call_id: exec.callId, tool: 'bash',
        started_at: new Date(startedAt).toISOString(), deadline_at: new Date(startedAt + timeoutMs).toISOString(),
        timeout_ms: timeoutMs, command_hash: createHash('sha256').update(args.command).digest('hex') };
      const log = (phase: string) => diagnostic('dsh.tool ' + JSON.stringify({ ...span,
        elapsed_ms: Math.round(performance.now() - started), phase }));
      if (exec.signal.aborted) { log('result'); throw new DOMException('bash aborted', 'AbortError'); }
      return new Promise<string>((resolve, reject) => {
        const child = createExecutor();
        owned.add(child);
        let pid: number | undefined, done = false;
        let output = Buffer.alloc(0), truncated = false;
        // The first stop reason wins, even if abort and deadline race with EOF.
        let stopping: 'timeout' | 'abort' | undefined, killed = false;
        let escalation: ReturnType<typeof setTimeout> | undefined, drain: ReturnType<typeof setTimeout> | undefined;
        const collect = (data: Buffer) => {
          output = Buffer.concat([output, data]);
          if (output.length > 100 * 1024) { output = output.subarray(output.length - 100 * 1024); truncated = true; }
        };
        const cleanup = () => {
          clearTimeout(timeout); clearTimeout(escalation); clearTimeout(drain);
          exec.signal.removeEventListener('abort', abort);
          if (pid !== undefined) trackGroup(record, pid, () => {});
        };
        const finish = (message: any = {}, error?: Error) => {
          if (done) return;
          done = true;
          cleanup();
          if (stopping || error || message.error) {
            if (pid !== undefined) signalGroup(pid, 'SIGKILL');
            // Neither IPC acknowledgement nor shell/stdio close is required to
            // settle. End this call's executor after the bounded drain as well.
            if (child.connected) child.disconnect();
            child.kill('SIGKILL');
          }
          log('result');
          if (stopping === 'abort') { reject(new DOMException('bash aborted', 'AbortError')); return; }
          if (error && !stopping) { reject(error); return; }
          if (message.error && !stopping) { reject(new Error(message.error)); return; }
          const status = stopping === 'timeout' ? `timed out after ${timeoutMs / 1000} s`
            : message.signal ? `killed by ${message.signal}` : `exit code ${message.code}`;
          const note = message.background_output
            ? '\n[command exited while a background process still holds its output; output drain stopped]' : '';
          resolve(`${truncated ? '[earlier output truncated]\n' : ''}${output.toString('utf8')}\n${status}${note}`);
        };
        const stop = (reason: 'timeout' | 'abort') => {
          if (done || stopping) return;
          stopping = reason;
          log('term');
          if (pid !== undefined) signalGroup(pid, 'SIGTERM');
          escalation = setTimeout(() => {
            killed = true;
            log('kill');
            if (pid !== undefined) signalGroup(pid, 'SIGKILL');
            // Give the executor a bounded chance to clean up an unacknowledged
            // shell before forcibly closing it. It owns that shell on IPC EOF.
            if (pid === undefined) child.kill('SIGTERM');
            log('output_drain');
            drain = setTimeout(() => finish(), DRAIN_MS);
          }, KILL_GRACE_MS);
        };
        const abort = () => stop('abort');
        const timeout = setTimeout(() => stop('timeout'), timeoutMs);
        exec.signal.addEventListener('abort', abort, { once: true });
        log('executor_spawn');
        child.on('message', (message: any) => {
          if (done) return;
          if (message.pid !== undefined) {
            pid = message.pid;
            record.processes.add(pid!);
            log('executor_ack');
            if (stopping) signalGroup(pid!, killed ? 'SIGKILL' : 'SIGTERM');
          } else if (message.output !== undefined) collect(Buffer.from(message.output, 'base64'));
          else if (message.phase === 'shell_exit') log('shell_exit');
          else {
            log(message.background_output ? 'output_drain' : 'stdio_close');
            if (!stopping) finish(message);
          }
        });
        child.once('error', error => { if (!stopping) finish({}, error); });
        child.once('close', () => {
          owned.delete(child);
          if (done || stopping) return;
          if (pid !== undefined) signalGroup(pid, 'SIGKILL');
          finish({}, new Error('bash executor stopped'));
        });
        child.send({ cwd, env: { ...record.env, HOME: record.home }, command: args.command }, error => {
          if (error && !stopping) finish({}, error);
        });
        if (exec.signal.aborted) abort();
      });
    } });
}
