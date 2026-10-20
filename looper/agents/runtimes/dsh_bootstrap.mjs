import { realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

// This required runner wrapper is the single startup boundary for Looper's contributions.
// Later MCP setup must finish here before delegating to the official runner.
export async function createBootstrap({ executable, text }) {
  const require = createRequire(realpathSync(executable));
  const runner = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-headless')).href);
  if (runner.name !== 'headless-runner' || !runner.Config ||
      !Array.isArray(runner.inject) || typeof runner.apply !== 'function') {
    throw new Error('Unsupported DSH headless exports: expected Config, inject and apply');
  }
  return {
    name: runner.name,
    Config: runner.Config,
    inject: [...runner.inject, 'systemPrompt'],
    apply(ctx, config) {
      ctx.systemPrompt.section({ name: 'looper', order: 20000, text, interpolate: false });
      return runner.apply(ctx, config);
    },
  };
}
