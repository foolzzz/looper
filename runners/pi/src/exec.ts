/** Builtin tools retain Pi's execution backend with session-owned boundaries. */
import { lstat, realpath } from 'node:fs/promises';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { RpcError } from '../../common/rpc.ts';
import { sdk } from './sdk.ts';

const factories = {
  read: 'createReadToolDefinition', grep: 'createGrepToolDefinition', find: 'createFindToolDefinition',
  ls: 'createLsToolDefinition', edit: 'createEditToolDefinition', write: 'createWriteToolDefinition',
  bash: 'createBashToolDefinition',
} as const;
const writes = new Set(['bash', 'edit', 'write']);
export function validateBuiltin(names: unknown[], readOnly: boolean): void {
  if (names.some(name => typeof name !== 'string' || !Object.hasOwn(factories, name))) {
    throw new RpcError(-32602, 'unknown builtin tool');
  }
  if (readOnly && names.some(name => writes.has(name as string))) {
    throw new RpcError(-32008, 'read-only sessions cannot use write tools');
  }
}
let running = 0;
export function toolProcesses(): number { return running; }
async function canonical(path: string): Promise<string> {
  try { return await realpath(path); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    // A dangling symlink is not a new file beneath a trusted ancestor.
    try { if ((await lstat(path)).isSymbolicLink()) throw new Error('dangling symlink'); }
    catch (e) { if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e; }
    const parent = dirname(path);
    if (parent === path) throw error;
    return join(await canonical(parent), basename(path));
  }
}
export async function builtinTools(params: { cwd: string; add_dirs: string[]; home: string;
    env: Record<string, string>; tools: { builtin: string[] }; limits: { tool_timeout_s: number } }): Promise<any[]> {
  const roots = await Promise.all([params.cwd, ...params.add_dirs].map(path => realpath(path)));
  return params.tools.builtin.map(name => {
    const factory = sdk!.agent[factories[name as keyof typeof factories]];
    const tool = factory(params.cwd, name === 'bash' ? {
      spawnHook: (context: any) => ({ ...context, env: { ...params.env, HOME: params.home } }),
    } : undefined);
    const execute = tool.execute;
    tool.execute = async (id: string, input: any, signal: AbortSignal, onUpdate: any, ctx: any) => {
      if (name === 'bash') {
        // Pi treats absent/nonpositive timeouts as unlimited; always enforce the host ceiling.
        const timeout = input.timeout > 0 ? Math.min(input.timeout, params.limits.tool_timeout_s) : params.limits.tool_timeout_s;
        running++;
        try { return await execute(id, { ...input, timeout }, signal, onUpdate, ctx); }
        finally { running--; }
      }
      const path = input.path ?? '.';
      const blocked = () => new Error(`Looper blocked ${name}: ${path} is outside the task directory and its added directories`);
      if (path.startsWith('~')) throw blocked();
      const target = await canonical(resolve(params.cwd, path));
      const allowed = (path: string) => roots.some(root => path === root || path.startsWith(root.endsWith(sep) ? root : root + sep));
      if (!allowed(target)) throw blocked();
      // Pi normalizes even absolute paths (Unicode spaces and read filename variants).
      // Check what its resolver will actually access, using the same input as execute.
      const actual = name === 'read'
        ? await sdk!.paths.resolveReadPathAsync(target, params.cwd)
        : sdk!.paths.resolveToCwd(target, params.cwd);
      if (!allowed(await canonical(actual))) throw blocked();
      // Execute the checked absolute path, so Pi cannot reinterpret @ or file: prefixes.
      return execute(id, { ...input, path: target }, signal, onUpdate, ctx);
    };
    return tool;
  });
}
