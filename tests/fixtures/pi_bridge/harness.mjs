// Loads the real pi extension with pi's own TypeScript loader and, when asked,
// validates arguments and processes tool results through pi's real agent loop.
// stdout is one JSON object. Exit 2 asks the caller to skip: node or pi's loader is missing.
import { execFileSync } from "node:child_process";
import { channel } from "node:diagnostics_channel";
import { getEventListeners } from "node:events";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";

function skip(reason) {
  process.stdout.write(`${JSON.stringify({ skip: reason })}\n`);
  process.exit(2);
}

function piPackageRoot() {
  let bin;
  try {
    bin = execFileSync("which", ["pi"], { encoding: "utf8" }).trim();
  } catch {
    return null;
  }
  if (!bin) return null;
  let dir = dirname(realpathSync(bin));
  for (let step = 0; step < 8; step += 1) {
    const packageJson = join(dir, "package.json");
    if (existsSync(packageJson)) {
      try {
        const meta = JSON.parse(readFileSync(packageJson, "utf8"));
        if (meta.name === "@earendil-works/pi-coding-agent") return dir;
      } catch {
        // A package.json that is not pi's is not the root.
      }
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

const root = piPackageRoot();
if (!root) skip("pi is not installed, so pi's extension loader did not run");

const loaderPath = join(root, "dist/core/extensions/loader.js");
const validatorPath = join(root, "node_modules/@earendil-works/pi-ai/dist/utils/validation.js");
if (!existsSync(loaderPath) || !existsSync(validatorPath)) {
  skip("pi's extension loader or tool-argument validator is not in this install");
}

let loadExtensions;
let validateToolArguments;
let createMcpToolDefinition;
let runAgentLoop;
let wrapRegisteredTool;
const processorPath = join(root, "node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js");
try {
  ({ loadExtensions } = await import(pathToFileURL(loaderPath).href));
  ({ validateToolArguments } = await import(pathToFileURL(validatorPath).href));
  ({ createMcpToolDefinition } = await import(pathToFileURL(join(root, "dist/extensions/mcp/tools.js")).href));
  ({ runAgentLoop } = await import(pathToFileURL(processorPath).href));
  ({ wrapRegisteredTool } = await import(pathToFileURL(join(root, "dist/core/extensions/wrapper.js")).href));
} catch (error) {
  if (error?.code === "ERR_MODULE_NOT_FOUND") {
    skip(`pi's runtime dependency is missing: ${error.message}`);
  }
  throw error;
}

const extensionPath = resolve(process.argv[2] ?? "");
const scenario = JSON.parse(readFileSync(process.argv[3], "utf8"));
const cwd = resolve(process.argv[4] ?? dirname(extensionPath));

const loaded = await loadExtensions([extensionPath], cwd);
if (loaded.errors.length > 0 || loaded.extensions.length !== 1) {
  process.stdout.write(`${JSON.stringify({
    ok: false,
    loaded: false,
    error: loaded.errors.map((item) => item.error).join("\n"),
    extension: null,
    loader: loaderPath,
    validator: validatorPath,
    tools: [],
    checks: [],
    calls: [],
  })}\n`);
  process.exit(0);
}

const extension = loaded.extensions[0];
const tools = [...extension.tools.values()].map((tool) => ({
  name: tool.definition.name,
  description: tool.definition.description,
  parameters: JSON.parse(JSON.stringify(tool.definition.parameters)),
}));

// Compare with the installed MCP adapter, rather than a second hand-written normalizer.
const nativeNormalization = (scenario.catalog ?? []).every((tool) => {
  const native = createMcpToolDefinition({
    server: "looper", tool, name: `mcp__looper__${tool.name}`, exposure: "direct",
    getClient: async () => { throw new Error("normalization must not call MCP"); },
  });
  return isDeepStrictEqual(extension.tools.get(native.name)?.definition.parameters, native.parameters);
});

const checks = [];
for (const check of scenario.checks ?? []) {
  const tool = extension.tools.get(check.name);
  if (!tool) {
    checks.push({ name: check.name, ok: false, error: "tool was not registered" });
    continue;
  }
  try {
    validateToolArguments(
      { name: tool.definition.name, parameters: tool.definition.parameters },
      { name: tool.definition.name, arguments: check.arguments },
    );
    checks.push({ name: check.name, ok: check.accept === true, error: null });
  } catch (error) {
    const message = error instanceof Error ? error.message.split("\n")[0] : String(error);
    checks.push({ name: check.name, ok: check.accept === false, error: message });
  }
}

const calls = [];
for (const call of scenario.calls ?? []) {
  const tool = extension.tools.get(call.name);
  if (!tool) {
    calls.push({ name: call.name, ok: false, error: "tool was not registered" });
    continue;
  }
  // Only the bridge's long deadlines use this clock. Network I/O stays real;
  // advance time (or cancel) after the server has received the POST and begun
  // its response, never on an arbitrary sleep racing the request.
  const controller = new AbortController();
  const nativeSetTimeout = globalThis.setTimeout;
  const nativeClearTimeout = globalThis.clearTimeout;
  const pending = new Map();
  const timeouts = [];
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (typeof delay !== "number" || delay < 120_000) return nativeSetTimeout(callback, delay, ...args);
    const timer = {};
    timeouts.push(delay);
    pending.set(timer, () => callback(...args));
    return timer;
  };
  globalThis.clearTimeout = (timer) => {
    if (!pending.delete(timer)) nativeClearTimeout(timer);
  };
  const onRequest = ({ request }) => {
    if (request.method !== "POST") return;
    request.once("response", (response) => response.once("data", () => {
      if (call.control === "abort") controller.abort();
      if (call.control === "timeout") {
        const deadlines = [...pending.values()];
        pending.clear();
        for (const fire of deadlines) fire();
        // Bound broken implementations without waiting the real 120 seconds.
        if (deadlines.length === 0) controller.abort();
      }
    }));
  };
  const requests = channel("http.client.request.start");
  requests.subscribe(onRequest);
  try {
    const toolCall = { type: "toolCall", id: call.id ?? "call-1", name: call.name, arguments: call.arguments ?? {} };
    const message = {
      role: "assistant", content: [toolCall], stopReason: "toolUse",
      api: "test", provider: "test", model: "test", usage: {}, timestamp: 0,
    };
    const events = [];
    // Synthetic model output, real pi tool wrapping, validation, execution,
    // finalization and transcript message creation. No model or auth is used.
    const messages = await runAgentLoop(
      [{ role: "user", content: "bridge probe", timestamp: 0 }],
      { messages: [], tools: [wrapRegisteredTool(tool, { createToolContext: () => ({}) })] },
      { model: { provider: "test" }, convertToLlm: (messages) => messages,
        finishTurn: () => ({ action: "end" }) },
      (event) => events.push(event),
      controller.signal,
      () => ({
        async *[Symbol.asyncIterator]() { yield { type: "done", message }; },
        result: async () => message,
      }),
    );
    const result = messages.find((item) => item.role === "toolResult");
    const execution = events.find((item) => item.type === "tool_execution_end");
    if (!result || !execution) throw new Error("pi did not process a tool result");
    calls.push({
      name: call.name,
      ok: true,
      content: result.content,
      isError: result.isError === true,
      detailsUndefined: result.details === undefined,
      executionIsError: execution.isError,
      toolCallId: result.toolCallId,
      timeouts,
      pendingTimers: pending.size,
      abortListeners: getEventListeners(controller.signal, "abort").length,
      error: null,
    });
  } catch (error) {
    calls.push({
      name: call.name,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  } finally {
    requests.unsubscribe(onRequest);
    globalThis.setTimeout = nativeSetTimeout;
    globalThis.clearTimeout = nativeClearTimeout;
  }
}

process.stdout.write(`${JSON.stringify({
  ok: true,
  loaded: true,
  error: null,
  extension: extension.path,
  loader: loaderPath,
  validator: validatorPath,
  processor: processorPath,
  nativeNormalization,
  tools,
  checks,
  calls,
})}\n`);
