/** Session-owned shell groups and synchronous native-file boundaries. */
import { fork, spawn, type ChildProcess } from 'node:child_process';
import { lstatSync, realpathSync } from 'node:fs';
import { basename, dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Session } from './session.ts';

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
    child.once('error', error => process.send!({ error: error.message }, () => stop()));
    child.once('close', (code, signal) => {
      const reap = () => {
        try { if (pid !== undefined) process.kill(-pid, 0); else return stop(); }
        catch (error) { if ((error as NodeJS.ErrnoException).code === 'ESRCH') { pid = undefined; return stop(); } }
        setTimeout(reap, 100);
      };
      // Flush the result before releasing the executor of a completed group.
      process.send!({ code, signal }, error => { if (error) stop(); else setTimeout(reap, 100); });
    });
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
export function registerBash(record: Session): void {
  if (!record.builtin.includes('bash')) return;
  const owned = new Set<ChildProcess>();
  executors.set(record, owned);
  record.agentCtx.tools.register({ name: 'bash',
    description: 'Run a bash command in the task directory and return its combined output and exit code.',
    parameters: { type: 'object', properties: { description: { type: 'string' }, command: { type: 'string' },
      timeoutMs: { type: 'number' }, workdir: { type: 'string' } }, required: ['description', 'command'] },
    output: { schema: { type: 'string' }, render: (_args: any, value: string) => [{ type: 'text', text: value }] },
    isConcurrencySafe: () => false,
    execute: (args: any, exec: { signal: AbortSignal }) => {
      const cwd = checkedPath(record, args.workdir ?? '.');
      const seconds = args.timeoutMs > 0 ? Math.min(args.timeoutMs / 1000, record.limits.tool_timeout_s) : record.limits.tool_timeout_s;
      if (exec.signal.aborted) throw new DOMException('bash aborted', 'AbortError');
      return new Promise<string>((resolve, reject) => {
        const child = fork(fileURLToPath(import.meta.url), ['--looper-bash-executor'], {
          env: {}, detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
        owned.add(child);
        let pid: number | undefined, done = false;
        let output = Buffer.alloc(0), truncated = false, timedOut = false, aborted = false;
        let escalation: ReturnType<typeof setTimeout> | undefined;
        const collect = (data: Buffer) => {
          output = Buffer.concat([output, data]);
          if (output.length > 100 * 1024) { output = output.subarray(output.length - 100 * 1024); truncated = true; }
        };
        const stop = () => {
          if (pid === undefined || escalation) return;
          signalGroup(pid, 'SIGTERM');
          escalation = setTimeout(() => signalGroup(pid, 'SIGKILL'), 2000);
        };
        const abort = () => { aborted = true; stop(); };
        const timeout = setTimeout(() => { timedOut = true; stop(); }, seconds * 1000);
        exec.signal.addEventListener('abort', abort, { once: true });
        const cleanup = () => {
          clearTimeout(timeout);
          exec.signal.removeEventListener('abort', abort);
          // Keep escalation alive if a child closed its pipes but ignored TERM.
          if (escalation) escalation.unref();
          if (pid !== undefined) trackGroup(record, pid, () => clearTimeout(escalation));
        };
        child.on('message', (message: any) => {
          if (message.pid !== undefined) {
            pid = message.pid;
            record.processes.add(pid!);
            if (aborted || timedOut) stop();
          } else if (message.output !== undefined) collect(Buffer.from(message.output, 'base64'));
          else {
            done = true;
            cleanup();
            if (aborted) { reject(new DOMException('bash aborted', 'AbortError')); return; }
            if (message.error) { reject(new Error(message.error)); return; }
            const status = timedOut ? `timed out after ${seconds} s`
              : message.signal ? `killed by ${message.signal}` : `exit code ${message.code}`;
            resolve(`${truncated ? '[earlier output truncated]\n' : ''}${output.toString('utf8')}\n${status}`);
          }
        });
        child.once('error', error => { cleanup(); reject(error); });
        child.once('close', () => {
          owned.delete(child);
          if (done) return;
          if (pid !== undefined) signalGroup(pid, 'SIGKILL');
          cleanup();
          reject(aborted ? new DOMException('bash aborted', 'AbortError') : new Error('bash executor stopped'));
        });
        child.send({ cwd, env: { ...record.env, HOME: record.home }, command: args.command });
      });
    } });
}
