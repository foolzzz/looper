/** Import only after initialize; the installed composition is pinned, never vendored. */
import { access, mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { RpcError } from '../../common/rpc.ts';

const VERSION = '0.2.1-rc.2';
const packages = ['dsh-launch-environment', 'dsh-llm', 'dsh-llm-deepseek', 'dsh-session',
  'dsh-session-persistence', 'dsh-session-persistence-jsonl', 'dsh-agent', 'dsh-agent-loop',
  'dsh-tools', 'dsh-tool-fs', 'dsh-tool-fs-search', 'dsh-system-prompt', 'cordis'] as const;
type Handle = (...args: any[]) => any;
export type Dsh = {
  version: string;
  profile: { runProfile: Handle };
  environment: { createLaunchEnvironmentSnapshot: Handle };
  llm: { createUserMessage: Handle; isAgentLoopRequest: Handle; LlmError: new (...args: any[]) => Error;
    CONTEXT_WINDOW_EXCEEDED_CODE: string };
  deepseek: { registerDeepSeekProvider: Handle; resolveAdapterOptions: Handle; catalogModelInfo: Handle; DEFAULT_MAX_TOKENS?: number };
  session: { buildForkSeed: Handle };
  systemPrompt: { renderPrompt: Handle };
  persistence: { SessionPersistenceNotFoundError: new (...args: any[]) => Error; SessionAlreadyOwnedError: new (...args: any[]) => Error };
};
export let dsh: Dsh;
type Context = { agents: any; llm: any; sessionPersistence: any; tools: any; systemPrompt: any;
  on: (...args: any[]) => any;
  fiber: { dispose: () => Promise<void> } };
export let ctx: Context | undefined;
export let environment: any;
type Manifest = { name: string; version: string; main?: string; exports?: { '.': string | { default?: string } } };
async function manifest(root: string): Promise<Manifest> {
  return JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
}
function requireExports(module: Record<string, unknown>, names: readonly string[], name: string): void {
  const missing = names.filter(key => typeof module[key] !== 'function');
  if (missing.length) throw new RpcError(-32009, `${name} missing exports: ${missing.join(', ')}`);
}
export async function load(executable: string): Promise<Dsh> {
  let operation = 'check Node version';
  try {
    const [major, minor] = process.versions.node.split('.').map(Number);
    if (major < 22 || (major === 22 && minor < 19)) {
      throw new RpcError(-32009, `Node version ${process.versions.node} is unsupported; require 22.19.0+`);
    }
    operation = 'resolve dsh executable';
    let root = dirname(await realpath(executable));
    let pkg: Manifest | undefined;
    for (let depth = 0; depth <= 8; depth++) {
      try { const found = await manifest(root); if (found.name === '@deepseek-ai/dsh') { pkg = found; break; } }
      catch { /* Executables may live below directories without a manifest. */ }
      const parent = dirname(root);
      if (parent === root) break;
      root = parent;
    }
    if (!pkg) throw new RpcError(-32009, 'dsh executable is not inside @deepseek-ai/dsh');
    const gate = (metadata: Manifest, name: string) => {
      if (name === '@deepseek-ai/cordis' ? !/^4\.0\.\d+$/.test(metadata.version) : metadata.version !== VERSION) {
        throw new RpcError(-32009, `${name} version ${metadata.version} is unsupported; require ${name === '@deepseek-ai/cordis' ? '4.0.x' : VERSION}`);
      }
    };
    gate(pkg, '@deepseek-ai/dsh');
    const require = createRequire(join(root, 'package.json'));
    const roots = new Map<string, { root: string; pkg: Manifest }>();
    for (const short of packages) {
      const name = `@deepseek-ai/${short}`;
      let found: string | undefined;
      for (const path of require.resolve.paths(name) ?? []) {
        const candidate = join(path, name);
        try { await access(join(candidate, 'package.json')); found = candidate; break; }
        catch { /* Next Node lookup entry. */ }
      }
      if (!found) throw new RpcError(-32009, `${name} package is missing`);
      operation = `read ${name} package version`;
      const metadata = await manifest(found);
      gate(metadata, name);
      roots.set(short, { root: found, pkg: metadata });
    }
    const importFile = (root: string, file: string, name: string) => {
      operation = `import ${name}`;
      return import(pathToFileURL(join(root, file)).href);
    };
    const exports = async (short: string, names: readonly string[]) => {
      const { root, pkg } = roots.get(short)!;
      const entry = pkg.exports?.['.'];
      const file = (typeof entry === 'string' ? entry : entry?.default) ?? pkg.main;
      const name = `@deepseek-ai/${short}`;
      if (!file) throw new RpcError(-32009, `${name} entry is missing`);
      const module = await importFile(root, file, name);
      requireExports(module, names, name);
      return module;
    };
    const profile = await importFile(root, 'lib/profile-boot.js', '@deepseek-ai/dsh profile-boot');
    requireExports(profile, ['runProfile'], '@deepseek-ai/dsh');
    dsh = { version: VERSION, profile,
      environment: await exports('dsh-launch-environment', ['createLaunchEnvironmentSnapshot']),
      llm: await exports('dsh-llm', ['createUserMessage', 'isAgentLoopRequest']),
      deepseek: await exports('dsh-llm-deepseek', ['registerDeepSeekProvider', 'resolveAdapterOptions', 'catalogModelInfo']),
      session: await exports('dsh-session', ['buildForkSeed']),
      systemPrompt: await exports('dsh-system-prompt', ['renderPrompt']),
      persistence: await exports('dsh-session-persistence', ['SessionPersistenceNotFoundError', 'SessionAlreadyOwnedError']),
    };
    return dsh;
  } catch (error) {
    if (error instanceof RpcError) throw error;
    // Filesystem/import exceptions carry paths and possibly credentials.
    throw new RpcError(-32009, `${operation} failed`);
  }
}

// Exact ids from the pinned headless tree; disabling a parent service can
// silently remove the runner's required agents, LLM, persistence or tools.
const disabled = ['hmr', 'settings', 'config-editor', 'plugin-manager', 'tool-plugin-manager',
  'credentials', 'authorization', 'deepseek-account', 'llm-deepseek-account', 'llm-pi-ai', 'otel',
  'session-telemetry-otel', 'session-log-deepseek', 'plugin-package-inventory-deepseek',
  'session-title', 'session-title-llm', 'agent-instructions', 'headless-startup', 'headless-runner',
  // Looper caps tool output; a second spill policy would rewrite the host's result.
  'llm-deepseek', 'llm-retry', 'spill-policy', 'tool-bash', 'tool-pwsh', 'tool-jobs', 'jobs',
  'subagent', 'subagent-spawn-in-process', 'subagent-fork-in-process', 'tool-subagent-control',
  'tool-subagent-list-agents', 'tool-subagent', 'tool-subagent-fork', 'ptc-runtime', 'workflow-ptc', 'tool-workflow',
  'web', 'web-search-deepseek', 'web-deepseek-api', 'web-fetch-http', 'tool-web',
  'skill', 'skill-filesystem', 'tool-skill', 'goal', 'goal-round-driver', 'command-goal', 'tool-goal'];
type ConsultDefaults = { text: string; tools: any[] };
let defaults: Promise<any> | undefined;
/** Copy model-visible CLI defaults, never its executors or live services. */
async function captureConsultDefaults(): Promise<any> {
  const root = join(process.cwd(), 'consult-defaults');
  await mkdir(root, { recursive: true, mode: 0o700 });
  const overlay = join(root, 'overlay.json');
  const templateDisabled = ['hmr', 'settings', 'config-editor', 'plugin-manager', 'tool-plugin-manager',
    'credentials', 'authorization', 'deepseek-account', 'llm-deepseek-account', 'llm-pi-ai', 'llm-deepseek',
    'otel', 'session-telemetry-otel', 'session-log-deepseek', 'plugin-package-inventory-deepseek',
    'session-title', 'session-title-llm', 'agent-instructions', 'headless-startup', 'headless-runner',
    'web-search-deepseek', 'web-deepseek-api', 'goal-round-driver', 'command-goal',
    'compaction-basic', 'command-compact'];
  const config = {
    tools: { mode: 'native' },
    'sandbox-policy': { mode: 'danger-full-access', workspaceRoot: process.cwd() },
    approval: { policy: 'never' },
    'session-persistence-jsonl': { root: join(root, 'sessions') },
    'skill-filesystem': { includeDefaultRoots: false, customSkillDirs: [], watch: false },
    'tool-web': { fetch: true, search: false, searchTimeoutMs: 60000, fetchTimeoutMs: 30000,
      fetchMaxOutputChars: 200000, searchMaxQueries: 4, searchMaxResults: 8 },
  };
  await writeFile(overlay, JSON.stringify([
    ...templateDisabled.map(id => ({ id, disabled: true })),
    ...Object.entries(config).map(([id, config]) => ({ id, config })),
  ]), { mode: 0o600 });
  let template: Context | undefined;
  const signals = ['SIGINT', 'SIGTERM'] as const;
  const listeners = new Map(signals.map(signal => [signal, process.listeners(signal)]));
  try {
    // This composition only assembles declarations. No agent or turn is created,
    // and inherited proxies are unnecessary for this entirely local snapshot.
    const values = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(?:https?|all|no)_proxy$/i.test(name)));
    const snapshot = dsh.environment.createLaunchEnvironmentSnapshot([{ source: 'process', values }]);
    ({ ctx: template } = await dsh.profile.runProfile({ environment: snapshot, profile: 'headless', patchFiles: [overlay], args: [] }));
    if (!template?.systemPrompt || !template.tools) throw new Error();
    return structuredClone(await template.systemPrompt.assemble());
  } finally {
    // runProfile installs process shutdown hooks in addition to its fiber.
    // The template must never exit the process before the live runner flushes.
    for (const signal of signals) for (const listener of process.listeners(signal)) {
      if (!listeners.get(signal)!.includes(listener)) process.removeListener(signal, listener);
    }
    await template?.fiber.dispose();
  }
}
export async function consultDefaults(model: string, provider: string): Promise<ConsultDefaults> {
  try {
    const assembly = await (defaults ??= captureConsultDefaults());
    return { text: dsh.systemPrompt.renderPrompt({ ...assembly,
      variables: { ...assembly.variables, model, provider } }), tools: structuredClone(assembly.tools) };
  } catch {
    throw new RpcError(-32603, 'DSH consult defaults failed to load');
  }
}
let booting: Promise<Context> | undefined;
export function boot(stateRoot: string): Promise<Context> {
  return booting ??= (async () => {
    try {
      // Dispose the declaration-only profile before booting the live composition.
      await (defaults ??= captureConsultDefaults());
      const config = {
        tools: { mode: 'native' },
        'sandbox-policy': { mode: 'danger-full-access', workspaceRoot: process.cwd() },
        approval: { policy: 'never' },
        'session-persistence-jsonl': { root: stateRoot },
        'session-telemetry-otel': { mode: 'DISABLED' },
        'compaction-basic': { auto: true, thresholdRatio: .8, headroomTokens: 65536,
          retainRatio: .16, maxTokens: 65536, compactionRetries: 1, maxOverflowRetries: 1, modelPolicies: [] },
      };
      const overlay = join(process.cwd(), 'looper-overlay.json');
      await writeFile(overlay, JSON.stringify([
        ...disabled.map(id => ({ id, disabled: true })),
        ...Object.entries(config).map(([id, config]) => ({ id, config })),
      ]), { mode: 0o600 });
      environment = dsh.environment.createLaunchEnvironmentSnapshot([{ source: 'process', values: { ...process.env } }]);
      ({ ctx } = await dsh.profile.runProfile({ environment, profile: 'headless', patchFiles: [overlay], args: [] }));
      if (!ctx?.agents || !ctx.llm || !ctx.sessionPersistence || !ctx.tools || !ctx.systemPrompt) throw new Error();
      return ctx;
    } catch {
      try { await ctx?.fiber?.dispose(); } catch { /* Preserve the boot error. */ }
      throw new RpcError(-32603, 'DSH composition failed to boot');
    }
  })();
}
