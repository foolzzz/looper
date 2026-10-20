// Loads the real pi extension with pi's own TypeScript loader and, when asked,
// checks registered parameters with pi's tool-argument validator.
// stdout is one JSON object. Exit 2 asks the caller to skip: node or pi's loader is missing.
import { execFileSync } from "node:child_process";
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
try {
  ({ loadExtensions } = await import(pathToFileURL(loaderPath).href));
  ({ validateToolArguments } = await import(pathToFileURL(validatorPath).href));
  ({ createMcpToolDefinition } = await import(pathToFileURL(join(root, "dist/extensions/mcp/tools.js")).href));
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
  try {
    const result = await tool.definition.execute(
      call.id ?? "call-1",
      call.arguments ?? {},
      undefined,
      undefined,
      {},
    );
    calls.push({
      name: call.name,
      ok: true,
      content: result.content,
      isError: result.isError === true,
      detailsUndefined: result.details === undefined,
      error: null,
    });
  } catch (error) {
    calls.push({
      name: call.name,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

process.stdout.write(`${JSON.stringify({
  ok: true,
  loaded: true,
  error: null,
  extension: extension.path,
  loader: loaderPath,
  validator: validatorPath,
  nativeNormalization,
  tools,
  checks,
  calls,
})}\n`);
