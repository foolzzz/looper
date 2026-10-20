import { realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

// The required runner is the single startup boundary for Looper's contributions.
export async function createBootstrap({ executable, text, mcp = [] }) {
  const require = createRequire(realpathSync(executable));
  const load = name => import(pathToFileURL(require.resolve(`@deepseek-ai/${name}`)).href);
  const runner = await load('dsh-headless');
  if (runner.name !== 'headless-runner' || !runner.Config ||
      !Array.isArray(runner.inject) || typeof runner.apply !== 'function') {
    throw new Error('Unsupported DSH headless exports: expected Config, inject and apply');
  }
  const client = mcp.length ? await load('dsh-mcp-client') : undefined;
  if (client && (!client.Config || !Array.isArray(client.inject) || typeof client.apply !== 'function')) {
    throw new Error('Unsupported DSH MCP exports: expected Config, inject and apply');
  }
  // Export the registration difference, never a prefix-based permission. The guard consumes this exact set.
  const platformTools = new Set();
  const diagnostics = [];
  const registerPrompt = ctx => ctx.systemPrompt.section({ name: 'looper', order: 20000, text, interpolate: false });
  const resolveRefs = refs => Object.fromEntries(Object.entries(refs ?? {}).map(([key, reference]) => {
    if (!Object.hasOwn(process.env, reference)) throw new Error('Missing run MCP environment reference');
    return [key, process.env[reference]];
  }));
  return {
    name: runner.name,
    Config: runner.Config,
    inject: [...new Set([...runner.inject, ...(client?.inject ?? []), 'systemPrompt'])],
    platformTools,
    diagnostics,
    apply: client ? async function apply(ctx, config) {
      platformTools.clear();
      diagnostics.length = 0;
      const names = new Set();
      for (const server of mcp) {
        if (names.has(server.serverName)) throw new Error('Duplicate MCP serverName');
        names.add(server.serverName);
      }
      if (!names.has('looper')) throw new Error('Required Looper MCP bridge is missing');
      // Mount the platform first so its exact registration difference cannot include optional catalog tools.
      for (const server of [...mcp].sort((a, b) => Number(b.serverName === 'looper') - Number(a.serverName === 'looper'))) {
        const { envRefs, headerRefs, requiredTools, expectedTools, toolGroups, ...options } = server;
        const own = server.serverName === 'looper';
        try {
          const before = new Set(ctx.tools.view().visible.keys());
          const resolved = client.Config({ ...options, env: resolveRefs(envRefs), headers: resolveRefs(headerRefs),
            failOnStartupError: true });
          // Do not mount an optional Cordis entry: its rejected activation would not fail the required runner.
          await client.apply(ctx, resolved);
          if (own) {
            const added = [...ctx.tools.view().visible.keys()].filter(name => !before.has(name)).sort();
            const expected = new Set(expectedTools.map(name => `mcp__looper__${name}`));
            const has = name => added.includes(`mcp__looper__${name}`);
            if (!added.length || added.some(name => !expected.has(name)) ||
                requiredTools.some(name => !has(name)) ||
                toolGroups.some(group => group.any.some(has) && !group.all.every(has))) {
              throw new Error('Looper MCP tool set does not match the platform contract');
            }
            for (const name of added) platformTools.add(name);
          }
        } catch {
          if (own) throw new Error('Required Looper MCP bridge failed to become ready');
          diagnostics.push({ serverName: server.serverName, status: 'failed' });
          ctx.logger.warn(`Optional MCP server ${server.serverName} failed to become ready`);
        }
      }
      registerPrompt(ctx);
      return runner.apply(ctx, config);
    } : function apply(ctx, config) {
      registerPrompt(ctx);
      return runner.apply(ctx, config);
    },
  };
}
