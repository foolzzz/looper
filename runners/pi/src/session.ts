/** Session-owned SDK composition; the runner never shares credentials or resources. */
import { stat, lstat } from 'node:fs/promises';
import { isAbsolute, join } from 'node:path';
import { addSecret, onShutdown, RpcError, scrub } from './rpc.ts';
import { sdk } from './sdk.ts';
import { authorizingStream } from './provider.ts';
import { cancelTurn, onEvent } from './turn.ts';
import type { Turn } from './turn.ts';

export type Session = {
  id: string; key: string; role: string; cwd: string; accountId: string; model: string;
  agent: any; runtime: any; turn: Turn | null; closing: boolean;
};
export const sessions = new Map<string, Session>();
const opening = new Set<string>();
export function getSession(params: Record<string, unknown>): Session {
  const record = sessions.get(params.session_id as string);
  if (!record || record.closing) throw new RpcError(-32001, 'session not found');
  return record;
}
function nonempty(value: unknown): value is string { return typeof value === 'string' && value.trim().length > 0; }
async function directory(path: unknown): Promise<boolean> {
  if (typeof path !== 'string' || !isAbsolute(path)) return false;
  try { return (await stat(path)).isDirectory(); } catch { return false; }
}
export async function newSession(params: Record<string, any>): Promise<object> {
  const provider = params.provider, prompt = params.system_prompt, tools = params.tools;
  // Register even rejected keys before any SDK diagnostic or error can expose them.
  if (typeof provider?.api_key === 'string') addSecret(provider.api_key);
  if (!nonempty(params.session_key) || !/^[A-Za-z0-9_-]+$/.test(params.session_key)
      || !['manager', 'planner'].includes(params.role)
      || !await directory(params.cwd) || !await directory(params.state_dir)
      || !provider || !['openai-responses', 'openai-chat'].includes(provider.protocol)
      || !['base_url', 'model', 'api_key', 'account_id'].every(key => nonempty(provider[key]))
      || !prompt || !['replace', 'append'].includes(prompt.mode) || typeof prompt.text !== 'string'
      || !tools || !Array.isArray(tools.builtin) || !Array.isArray(tools.platform)) {
    throw new RpcError(-32602, 'invalid session parameters');
  }
  if (tools.platform.length) throw new RpcError(-32007, 'platform tools arrive with each prompt');
  if (tools.builtin.length) throw new RpcError(-32007, 'builtin tools are not enabled in this runner');
  const agentDir = join(process.cwd(), 'sessions', params.session_key, 'agent');
  try {
    if (!(await lstat(agentDir)).isDirectory()) throw new Error('not a real directory');
  } catch { throw new RpcError(-32008, 'session agent directory must exist and be a real directory'); }
  // Reserve atomically after the last filesystem await, including concurrent new sessions.
  if (opening.has(params.session_key) || [...sessions.values()].some(r => r.key === params.session_key)) {
    throw new RpcError(-32002, 'session key is already owned');
  }
  if (sessions.size + opening.size >= 10) throw new RpcError(-32006, 'session capacity reached');
  opening.add(params.session_key);
  let agent: any, runtime: any;
  try {
    const { InMemoryCredentialStore } = sdk!.ai;
    const { ModelRuntime, SettingsManager, SessionManager, DefaultResourceLoader, createAgentSession } = sdk!.agent;
    const credentials = new (InMemoryCredentialStore as any)();
    runtime = await (ModelRuntime as any).create({ credentials, modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
    const api = provider.protocol === 'openai-chat' ? 'openai-completions' : 'openai-responses';
    // The closure is used only after composition, when prompt has installed a turn.
    let record: Session;
    runtime.registerProvider('looper', { api, baseUrl: provider.base_url,
      models: [{ id: provider.model, name: provider.model, api, input: ['text'], reasoning: provider.reasoning,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        ...(provider.context_window ? { contextWindow: provider.context_window } : {}),
        ...(api === 'openai-responses' ? { compat: { supportsStrictMode: true } } : {}) }],
      streamSimple: (model: any, context: any, options: any) => authorizingStream(record, model, context, options),
    });
    // Config apiKey values are executable references; literal keys belong only in runtime credentials.
    await runtime.setRuntimeApiKey('looper', provider.api_key);
    const settingsManager = (SettingsManager as any).inMemory({ cacheWarming: 'off', enableAnalytics: false,
      enableInstallTelemetry: false, defaultProjectTrust: 'never', packages: [], extensions: [], skills: [],
      prompts: [], themes: [], retry: { provider: { maxRetries: 0 } } });
    const sessionManager = (SessionManager as any).create(params.cwd, params.state_dir);
    const resourceLoader = new (DefaultResourceLoader as any)({ cwd: params.cwd, agentDir, settingsManager,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      ...(prompt.mode === 'replace' ? { systemPrompt: prompt.text } : { appendSystemPrompt: [prompt.text] }) });
    await resourceLoader.reload();
    ({ session: agent } = await createAgentSession({ cwd: params.cwd, agentDir, modelRuntime: runtime,
      model: runtime.getModel('looper', provider.model), thinkingLevel: provider.effort ?? undefined,
      tools: [], sessionManager, settingsManager, resourceLoader }));
    record = { id: agent.sessionId, key: params.session_key, role: params.role, cwd: params.cwd,
      accountId: provider.account_id, model: provider.model, agent, runtime, turn: null, closing: false };
    agent.subscribe((event: any) => onEvent(record, event));
    if (agent.getActiveToolNames().length) throw new RpcError(-32008, 'session contains unexpected tools');
    sessions.set(record.id, record);
    return { session_id: record.id };
  } catch (error) {
    agent?.dispose();
    runtime?.dispose?.();
    if (error instanceof RpcError) throw error;
    throw new RpcError(-32603, scrub(error instanceof Error ? error.message : 'session composition failed').slice(0, 512));
  } finally { opening.delete(params.session_key); }
}
export function release(record: Session): void {
  if (!sessions.delete(record.id)) return;
  record.agent.dispose();
  record.runtime.dispose?.();
}
export async function closeSession(params: Record<string, unknown>): Promise<object> {
  const record = getSession(params), turn = record.turn;
  if (turn && !turn.settled && params.trace_id !== turn.trace) throw new RpcError(-32005, 'trace mismatch');
  record.closing = true;
  if (turn && !turn.settled) {
    turn.cancel = 'close';
    await cancelTurn(record, turn);
  }
  release(record);
  return params.trace_id === undefined ? {} : { session_id: record.id, trace_id: params.trace_id };
}
onShutdown(async () => {
  await Promise.all([...sessions.values()].map(async record => {
    record.closing = true;
    if (record.turn && !record.turn.settled) {
      record.turn.cancel = 'close';
      // Leave time for disposal before the transport's shared four-second exit deadline.
      await cancelTurn(record, record.turn, 3000);
    }
    release(record);
  }));
});
