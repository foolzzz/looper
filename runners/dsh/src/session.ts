/** Agent-scoped sessions in one composition; credentials live only in route closures. */
import { randomUUID } from 'node:crypto';
import { lstat, stat, realpath, readdir, rm } from 'node:fs/promises';
import { isAbsolute, join, relative, sep } from 'node:path';
import { addSecret, diagnostic, onShutdown, RpcError, scrub } from '../../common/rpc.ts';
import { ctx, dsh, environment } from './sdk.ts';

export type Session = {
  id: string; key: string; role: string; cwd: string; roots: string[]; env: Record<string, string>;
  home: string; limits: { tool_timeout_s: number; max_requests_per_turn: number };
  accountId: string; model: string; wireModel: string; route: string; routeOwner: any;
  handle: any; agent: any; agentCtx: any; builtin: string[]; readOnly: boolean;
  ephemeral: boolean; allowed: Set<string>; turn: null; closing: boolean;
};
export const sessions = new Map<string, Session>();
const opening = new Set<string>(), loading = new Set<string>();
const closing = new Map<string, Promise<void>>();
const builtins = new Set(['read', 'glob', 'grep', 'write', 'edit', 'bash']);
let stateRoot: string;
export function configureSessions(root: string): void { stateRoot = root; }
function nonempty(value: unknown): value is string { return typeof value === 'string' && value.trim().length > 0; }
async function directory(path: unknown, noLink = false): Promise<boolean> {
  if (typeof path !== 'string' || !isAbsolute(path)) return false;
  try { return (await (noLink ? lstat(path) : stat(path))).isDirectory(); } catch { return false; }
}
async function validate(params: Record<string, any>): Promise<{ roots: string[]; ephemeral: boolean }> {
  const provider = params.provider, prompt = params.system_prompt, tools = params.tools, limits = params.limits;
  // Even a rejected credential must be scrubbed before any SDK work or diagnostic.
  if (typeof provider?.api_key === 'string') addSecret(provider.api_key);
  if (!nonempty(params.session_key) || !/^[A-Za-z0-9_-]+$/.test(params.session_key)
      || !['manager', 'planner', 'worker', 'reviewer'].includes(params.role)
      || !await directory(params.cwd) || !await directory(params.home, true) || !await directory(params.state_dir, true)
      || !Array.isArray(params.add_dirs) || !(await Promise.all(params.add_dirs.map((path: unknown) => directory(path)))).every(Boolean)
      || !params.env || typeof params.env !== 'object' || Array.isArray(params.env)
      || !Object.entries(params.env).every(([key, value]) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && typeof value === 'string')
      || !provider || provider.protocol !== 'anthropic-messages'
      || !['base_url', 'model', 'api_key', 'account_id'].every(key => nonempty(provider[key]))
      || ![null, 'low', 'high', 'max'].includes(provider.effort)
      || !(provider.context_window === null || (Number.isInteger(provider.context_window) && provider.context_window > 0))
      || !prompt || typeof prompt.text !== 'string'
      || !tools || typeof tools.read_only !== 'boolean' || !Array.isArray(tools.platform)
      || !Array.isArray(tools.builtin) || !tools.builtin.includes('read')
      || !tools.builtin.every((name: unknown) => typeof name === 'string' && builtins.has(name))
      || !limits || !Number.isInteger(limits.tool_timeout_s) || limits.tool_timeout_s <= 0
      || !Number.isInteger(limits.max_requests_per_turn) || limits.max_requests_per_turn < 0) {
    throw new RpcError(-32602, 'invalid session parameters');
  }
  if (prompt.mode !== 'append') throw new RpcError(-32008, 'DSH requires an appended system prompt');
  if (tools.read_only && tools.builtin.some((name: string) => ['write', 'edit', 'bash'].includes(name))) {
    throw new RpcError(-32008, 'read-only sessions cannot enable write, edit or bash');
  }
  if (tools.platform.length) throw new RpcError(-32007, 'platform tools arrive with each prompt');
  if ('fork_from' in params) throw new RpcError(-32007, 'session fork is not supported yet');
  const roots = await Promise.all([params.cwd, ...params.add_dirs].map(path => realpath(path)));
  const state = await realpath(params.state_dir);
  const leasePath = relative(join(await realpath(process.cwd()), 'sessions'), state);
  return { roots, ephemeral: leasePath !== '' && leasePath !== '..' && !leasePath.startsWith('..' + sep) && !isAbsolute(leasePath) };
}
export function getSession(params: Record<string, unknown>): Session {
  const record = sessions.get(params.session_id as string);
  if (!record || record.closing) throw new RpcError(-32001, 'session not found');
  return record;
}
export function newSession(params: Record<string, any>): Promise<object> { return compose(params, false); }
export function loadSession(params: Record<string, any>): Promise<object> { return compose(params, true); }
async function compose(params: Record<string, any>, resumed: boolean): Promise<object> {
  const validated = await validate(params);
  if (resumed && !nonempty(params.session_id)) throw new RpcError(-32602, 'invalid session id');
  // Reserve after all filesystem awaits so concurrent keys and loads cannot race admission.
  if (resumed && (sessions.has(params.session_id) || loading.has(params.session_id))) {
    throw new RpcError(-32002, 'session is already owned');
  }
  if (opening.has(params.session_key) || [...sessions.values()].some(record => record.key === params.session_key)) {
    throw new RpcError(-32002, 'session key is already owned');
  }
  if (sessions.size + opening.size >= 20) throw new RpcError(-32006, 'session capacity reached');
  opening.add(params.session_key);
  if (resumed) loading.add(params.session_id);
  const provider = params.provider, wireModel = provider.model.replace(/\[1m\]$/, '');
  const record: Session = { id: resumed ? params.session_id : randomUUID(), key: params.session_key,
    role: params.role, cwd: params.cwd, ...validated, env: { ...params.env }, home: params.home,
    limits: { ...params.limits }, accountId: provider.account_id, model: provider.model, wireModel,
    route: 'looper-' + params.session_key, routeOwner: undefined, handle: undefined, agent: undefined,
    agentCtx: undefined, builtin: [...params.tools.builtin], readOnly: params.tools.read_only,
    allowed: new Set(params.tools.builtin), turn: null, closing: false };
  let started = false;
  try {
    let connection: any;
    try {
      connection = dsh.deepseek.resolveAdapterOptions({ baseURL: provider.base_url,
        reasoningEffort: provider.effort ?? 'high', models: [{ id: wireModel,
          ...(provider.context_window ? { contextWindow: provider.context_window } : {}) }] }, environment);
    } catch { throw new RpcError(-32602, 'invalid provider endpoint'); }
    const key = provider.api_key;
    const setup = (agentCtx: any, agent: any) => {
      record.agentCtx = agentCtx;
      record.agent = agent;
      dsh.deepseek.registerDeepSeekProvider(agentCtx, record.route, { providerName: 'DeepSeek',
        options: () => connection,
        discoverModels: async (p: string) => connection.models.map((m: any) => dsh.deepseek.catalogModelInfo(p, m)),
        resolveAuth: async () => ({ headers: { 'x-api-key': key } }) });
      agentCtx.systemPrompt.section({ name: 'looper', order: 20000, text: params.system_prompt.text, interpolate: false });
      agentCtx.tools.restrict({ allow: record.builtin.filter(name => name !== 'bash') });
      agentCtx.tools.guard((e: { name: string }) => record.allowed.has(e.name) ? undefined
        : record.readOnly ? 'LOOPER_READ_ONLY: tool is not allowed in a read-only run'
          : 'LOOPER_TOOL: ' + e.name + ' is not enabled for this session');
    };
    const agentOptions = { provider: record.route, model: wireModel, reasoningEffort: provider.effort ?? 'high' };
    started = true;
    record.handle = resumed
      ? await ctx!.agents.resume({ resumeSessionId: record.id, agentOptions, setup })
      : await ctx!.agents.create({ sessionId: record.id, meta: { cwd: record.cwd }, agentOptions, setup });
    if (resumed && record.handle.agent.session.header.cwd !== record.cwd) {
      throw new RpcError(-32001, 'saved session belongs to another directory');
    }
    if (!resumed) await ctx!.sessionPersistence.flush();
    sessions.set(record.id, record);
    return { session_id: record.id, ...(resumed ? { resumed: true } : {}) };
  } catch (error) {
    let disposed = false;
    try { await record.handle?.dispose(); disposed = true; }
    catch { diagnostic('failed to dispose rejected session'); }
    finally { await record.routeOwner?.dispose(); }
    // A failed new/setup may have materialized its header before throwing.
    if (started && disposed && !resumed) await removeStored(record.id);
    if (error instanceof RpcError) throw error;
    if (error instanceof dsh.persistence.SessionPersistenceNotFoundError) throw new RpcError(-32001, 'session not found');
    if (error instanceof dsh.persistence.SessionAlreadyOwnedError) throw new RpcError(-32002, 'session is already owned');
    const message = scrub(error instanceof Error ? error.message : 'session composition failed').replace(/[\r\n]+/g, ' ').slice(0, 512);
    if (resumed && error instanceof Error && ['SessionPersistenceCorruptionError', 'SessionFormatUnsupportedError',
      'SessionFormatError', 'SessionFormatUnsupportedMigrationError'].includes(error.name)) {
      throw new RpcError(-32001, message);
    }
    throw new RpcError(-32603, message);
  } finally { opening.delete(params.session_key); if (resumed) loading.delete(params.session_id); }
}
async function removeStored(id: string): Promise<void> {
  try {
    const matches: string[] = [];
    for (const project of await readdir(stateRoot)) {
      const parent = join(stateRoot, project);
      if (!(await lstat(parent)).isDirectory()) continue;
      const candidate = join(parent, id);
      try { await lstat(candidate); matches.push(candidate); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    }
    if (matches.length === 1 && (await lstat(matches[0])).isDirectory()) {
      await rm(matches[0], { recursive: true });
    } else diagnostic('stored session cleanup refused: expected one real directory');
  } catch { diagnostic('stored session cleanup failed'); }
}
export async function closeSession(params: Record<string, unknown>): Promise<object> {
  const record = getSession(params);
  record.closing = true;
  const operation = (async () => {
    await record.handle.dispose();
    await record.routeOwner?.dispose();
    if (record.ephemeral) await removeStored(record.id);
    sessions.delete(record.id);
  })();
  closing.set(record.id, operation);
  try {
    await operation;
  } catch (error) { record.closing = false; throw error; }
  finally { closing.delete(record.id); }
  return {};
}
onShutdown(async () => {
  await Promise.all([...sessions.values()].map(async record => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      // EOF can arrive while an explicit close is still draining persistence.
      await Promise.race([closing.get(record.id) ?? closeSession({ session_id: record.id }), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('session close timed out')), 3000);
      })]);
    } catch { diagnostic('session shutdown failed'); }
    finally { clearTimeout(timer); }
  }));
  try { await ctx?.sessionPersistence.flush(); }
  finally { await ctx?.fiber.dispose(); }
});
