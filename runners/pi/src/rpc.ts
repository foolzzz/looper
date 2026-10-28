/** The stdio transport owns all protocol I/O, including SDK callback writes. */
import { once } from 'node:events';

type Params = Record<string, unknown>;
type Handler = (params: Params) => unknown | Promise<unknown>;
type Pending = { resolve: (value: unknown) => void; reject: (error: Error) => void; cleanup: () => void };
export class RpcError extends Error {
  code: number;
  constructor(code: number, message: string) { super(message); this.code = code; }
}
const secrets = new Set<string>();
export function addSecret(secret: string): void { if (secret) secrets.add(secret); }
export function scrub(text: string): string {
  for (const secret of [...secrets].sort((a, b) => b.length - a.length)) text = text.replaceAll(secret, '[redacted]');
  return text;
}
export function diagnostic(text: string): void { process.stderr.write(scrub(text).replace(/[\r\n]+/g, ' ') + '\n'); }
function fatal(message: string, code = 2): never { diagnostic(message); process.exit(code); }
const methods = new Map<string, { handler: Handler; session: boolean }>();
const chains = new Map<string, Promise<void>>();
const received = new Set<string>();
const pending = new Map<string, Pending>();
const hooks: Array<() => unknown | Promise<unknown>> = [];
let initialized = false, attempted = false, counter = 0, stopped = false;
let output = Promise.resolve();
let buffer = Buffer.alloc(0);
const MAX_FRAME = 4 * 1024 * 1024;
export function method(name: string, handler: Handler, options: { session?: boolean } = {}): void {
  if (methods.has(name)) throw new Error('duplicate runner method');
  methods.set(name, { handler, session: options.session ?? false });
}
export function onShutdown(fn: () => unknown | Promise<unknown>): void { hooks.push(fn); }
export async function shutdown(): Promise<void> {
  if (stopped) return;
  stopped = true;
  process.stdin.pause();
  process.stdin.removeListener('data', read);
  const deadline = setTimeout(() => process.exit(0), 4000);
  try {
    await Promise.all(hooks.map(async hook => { try { await hook(); } catch { diagnostic('runner shutdown hook failed'); } }));
    await output;
  } finally { clearTimeout(deadline); process.exit(0); }
}
function write(message: object): Promise<void> {
  const frame = JSON.stringify(message) + '\n';
  output = output.then(async () => {
    if (process.stdout.destroyed || process.stdout.writableEnded) fatal('runner stdout closed', 1);
    if (!process.stdout.write(frame)) await once(process.stdout, 'drain');
  }).catch(() => fatal('runner stdout unavailable', 1));
  return output;
}
export function notify(method: string, params: Params): Promise<void> { return write({ jsonrpc: '2.0', method, params }); }
export function update(sessionId: string, traceId: string, kind: string, fields: Params = {}): Promise<void> {
  return notify('session/update', { ...fields, session_id: sessionId, trace_id: traceId, kind });
}
export function request(method: string, params: Params, options: { signal?: AbortSignal } = {}): Promise<unknown> {
  const id = `r-${++counter}`;
  return new Promise((resolve, reject) => {
    const signal = options.signal;
    const abort = () => reject(new RpcError(-32603, 'request aborted'));
    // Keep an aborted id until its reply arrives: an authorized late reply is harmless.
    pending.set(id, { resolve, reject, cleanup: () => signal?.removeEventListener('abort', abort) });
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    void write({ jsonrpc: '2.0', id, method, params }).catch(reject);
  });
}
function object(value: unknown): value is Params { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function dispatch(value: unknown): void {
  if (!object(value) || value.jsonrpc !== '2.0' || typeof value.id !== 'string') fatal('runner protocol violation: invalid JSON-RPC envelope');
  const id = value.id;
  if ('method' in value) {
    if (!/^l-\d+$/.test(id) || typeof value.method !== 'string' || !value.method || !object(value.params)
        || 'result' in value || 'error' in value || received.has(id)) fatal('runner protocol violation: invalid request');
    received.add(id);
    const name = value.method, params = value.params;
    const registered = methods.get(name);
    // Admission is synchronous, before any promise can yield to another frame.
    let refusal: RpcError | undefined;
    if (name === 'initialize') {
      if (attempted) refusal = new RpcError(-32600, 'initialize may only run once');
      attempted = true;
    } else if (!initialized) refusal = new RpcError(-32600, 'initialize is required');
    const execute = async () => {
      try {
        if (refusal) throw refusal;
        if (!registered) throw new RpcError(-32601, 'method not found');
        const result = await registered.handler(params);
        if (!object(result)) throw new RpcError(-32603, 'runner handler returned an invalid result');
        if (name === 'initialize') initialized = true;
        await write({ jsonrpc: '2.0', id, result });
      } catch (error) {
        const data: Params = {};
        for (const key of ['session_id', 'trace_id']) if (key in params) data[key] = params[key];
        await write({ jsonrpc: '2.0', id, error: {
          code: error instanceof RpcError ? error.code : -32603,
          message: scrub(error instanceof RpcError ? error.message : 'runner handler failed').slice(0, 512), data,
        } });
      }
    };
    const sessionKey = name === 'session/new' ? params.session_key : params.session_id;
    if (registered?.session && typeof sessionKey === 'string') {
      const sid = `${name === 'session/new' ? 'key' : 'id'}:${sessionKey}`;
      const next = (chains.get(sid) ?? Promise.resolve()).then(execute);
      chains.set(sid, next);
      void next.finally(() => { if (chains.get(sid) === next) chains.delete(sid); });
    } else void execute();
    return;
  }
  if (!/^r-\d+$/.test(id) || 'params' in value || ('result' in value) === ('error' in value)
      || ('result' in value && !object(value.result))) fatal('runner protocol violation: invalid response');
  const waiter = pending.get(id);
  if (!waiter) fatal('runner protocol violation: response id was never issued');
  if ('error' in value && (!object(value.error) || !Number.isInteger(value.error.code) || typeof value.error.message !== 'string'
      || ('data' in value.error && !object(value.error.data)))) {
    fatal('runner protocol violation: invalid error');
  }
  pending.delete(id);
  waiter.cleanup();
  if ('error' in value) {
    const error = value.error as { code: number; message: string };
    waiter.reject(new RpcError(error.code, scrub(error.message).slice(0, 512)));
  } else waiter.resolve(value.result);
}
function frame(bytes: Buffer): void {
  if (bytes.length > MAX_FRAME) fatal('runner protocol violation: frame exceeds 4 MiB');
  let decoded: unknown;
  try { decoded = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
  catch { fatal('runner protocol violation: invalid UTF-8 or JSON'); }
  dispatch(decoded);
}
function read(chunk: Buffer): void {
  let start = 0;
  for (let end = chunk.indexOf(10); end !== -1; end = chunk.indexOf(10, start)) {
    frame(buffer.length ? Buffer.concat([buffer, chunk.subarray(start, end)]) : chunk.subarray(start, end));
    buffer = Buffer.alloc(0);
    start = end + 1;
  }
  if (buffer.length + chunk.length - start > MAX_FRAME) fatal('runner protocol violation: frame exceeds 4 MiB');
  if (start < chunk.length) buffer = Buffer.concat([buffer, chunk.subarray(start)]);
}
export function start(): void {
  process.stdout.on('error', () => fatal('runner stdout unavailable', 1));
  process.stdout.on('close', () => fatal('runner stdout closed', 1));
  process.stdin.on('error', () => fatal('runner stdin unavailable'));
  process.stdin.on('data', read);
  process.stdin.on('end', () => {
    if (buffer.length) fatal('runner protocol violation: incomplete frame');
    void shutdown();
  });
}
