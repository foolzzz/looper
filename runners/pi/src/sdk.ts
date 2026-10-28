/** Import only after initialize; never use the global pi-ai provider registry. */
import { readFile, realpath, access } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { RpcError } from './rpc.ts';

const agentExports = ['createAgentSession', 'ModelRuntime', 'SessionManager', 'SettingsManager',
  'DefaultResourceLoader', 'createReadToolDefinition', 'createGrepToolDefinition', 'createFindToolDefinition',
  'createLsToolDefinition', 'createEditToolDefinition', 'createWriteToolDefinition', 'createBashToolDefinition'] as const;
type Handle = (...args: any[]) => any;
export type Sdk = {
  version: string;
  agent: Record<typeof agentExports[number], Handle>;
  ai: { InMemoryCredentialStore: Handle };
  responses: { streamSimple: Handle };
  completions: { streamSimple: Handle };
};
export let sdk: Sdk | undefined;
async function manifest(root: string): Promise<{ name: string; version: string }> {
  return JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
}
function gate(version: string, name: string): void {
  if (typeof version !== 'string' || !/^1\.3\.\d+$/.test(version)) throw new RpcError(-32009, `${name} version ${version} is unsupported; require 1.3.x`);
}
function requireExports(module: Record<string, unknown>, names: readonly string[], name: string): void {
  const missing = names.filter(key => typeof module[key] !== 'function');
  if (missing.length) throw new RpcError(-32009, `${name} missing exports: ${missing.join(', ')}`);
}
export async function load(pi: string): Promise<Sdk> {
  let operation = 'check Node version';
  try {
    const [major, minor] = process.versions.node.split('.').map(Number);
    if (major < 22 || (major === 22 && minor < 19)) {
      throw new RpcError(-32009, `Node version ${process.versions.node} is unsupported; require 22.19.0+`);
    }
    operation = 'resolve Pi executable';
    let root = dirname(await realpath(pi));
    let pkg: { name: string; version: string } | undefined;
    for (let depth = 0; depth <= 8; depth++) {
      try { const found = await manifest(root); if (found.name === '@earendil-works/pi-coding-agent') { pkg = found; break; } }
      catch { /* Executables may live below directories without a manifest. */ }
      const parent = dirname(root);
      if (parent === root) break;
      root = parent;
    }
    if (!pkg) throw new RpcError(-32009, 'Pi executable is not inside @earendil-works/pi-coding-agent');
    gate(pkg.version, pkg.name);
    const paths = createRequire(join(root, 'package.json')).resolve.paths('@earendil-works/pi-ai') ?? [];
    let aiRoot: string | undefined;
    for (const path of paths) {
      const candidate = join(path, '@earendil-works/pi-ai');
      try { await access(join(candidate, 'package.json')); aiRoot = candidate; break; } catch { /* Next Node lookup entry. */ }
    }
    if (!aiRoot) throw new RpcError(-32009, '@earendil-works/pi-ai package is missing');
    operation = 'read @earendil-works/pi-ai package version';
    const aiPkg = await manifest(aiRoot);
    gate(aiPkg.version, '@earendil-works/pi-ai');
    const importFile = (root: string, file: string, name: string) => {
      operation = `import ${name}/${file}`;
      return import(pathToFileURL(join(root, file)).href);
    };
    const agent = await importFile(root, 'dist/index.js', pkg.name);
    requireExports(agent, agentExports, pkg.name);
    const ai = await importFile(aiRoot, 'dist/index.js', '@earendil-works/pi-ai');
    requireExports(ai, ['InMemoryCredentialStore'], '@earendil-works/pi-ai');
    const responses = await importFile(aiRoot, 'dist/api/openai-responses.js', '@earendil-works/pi-ai');
    const completions = await importFile(aiRoot, 'dist/api/openai-completions.js', '@earendil-works/pi-ai');
    requireExports(responses, ['streamSimple'], 'openai-responses');
    requireExports(completions, ['streamSimple'], 'openai-completions');
    sdk = { version: pkg.version, agent, ai, responses, completions };
    return sdk;
  } catch (error) {
    if (error instanceof RpcError) throw error;
    // Filesystem/import exceptions may contain credentials or paths, never forward their payloads.
    throw new RpcError(-32009, `${operation} failed`);
  }
}
