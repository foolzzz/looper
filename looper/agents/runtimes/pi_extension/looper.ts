/**
 * Pi extension: Looper's platform tools, loaded by pi's TypeScript loader.
 *
 * The async factory runs before the model starts. It reads this run's binding
 * (`LOOPER_AGENT_URL` and `LOOPER_AGENT_TOKEN`, taken from the looper MCP
 * entry). No binding registers nothing. A binding that cannot list its tools
 * throws, so startup fails instead of calling the model with no platform tools.
 *
 * `GET /api/agent/v1/tools` supplies every tool. After the whole list checks
 * out, each one is registered as `mcp__looper__<name>`. The description and
 * input schema stay the server's; parameters are normalized the way pi's own
 * MCP adapter normalizes an input schema (`type` and `properties` filled in).
 * A call posts to `/api/agent/v1/ops/<name>`. Requests go straight to the bound
 * origin: no proxy and no redirect.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Agent as HttpAgent, request as httpRequest, type ClientRequest } from "node:http";
import { Agent as HttpsAgent, request as httpsRequest } from "node:https";

// A direct agent. Node's global agent follows HTTP_PROXY when
// NODE_USE_ENV_PROXY is set, which would send the bearer token to the proxy.
const directHttp = new HttpAgent();
const directHttps = new HttpsAgent();

const URL_ENV = "LOOPER_AGENT_URL";
const TOKEN_ENV = "LOOPER_AGENT_TOKEN";
const REQUEST_TIMEOUT_MS = 120_000;
const WAIT_MARGIN_MS = 20_000;
const WAITING_TOOL = "looper_room_inbox";

interface Binding {
  url: string;
  token: string;
}

interface ListedTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

type ParametersSchema = Parameters<ExtensionAPI["registerTool"]>[0]["parameters"];

interface ToolResult {
  content: { type: "text"; text: string }[];
  details: undefined;
  isError: boolean;
}

class BridgeError extends Error {}

class BridgeTimeout extends BridgeError {}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function readBinding(): Binding | null {
  if (process.env[URL_ENV] === undefined && process.env[TOKEN_ENV] === undefined) return null;
  const url = (process.env[URL_ENV] ?? "").trim();
  const token = (process.env[TOKEN_ENV] ?? "").trim();
  const missing = [url === "" ? URL_ENV : null, token === "" ? TOKEN_ENV : null].filter((item) => item !== null);
  if (missing.length > 0) {
    throw new Error(`Looper tool bridge needs ${missing.join(" and ")}`);
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error("LOOPER_AGENT_URL must be an http:// or https:// URL");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("LOOPER_AGENT_URL must be an http:// or https:// URL");
  }
  return { url: url.replace(/\/+$/, ""), token };
}

/** Check schema positions recursively, without interpreting enum/default values as schemas. */
function isSchema(schema: unknown): boolean {
  if (typeof schema === "boolean") return true;
  if (!isPlainObject(schema)) return false;
  const types = ["object", "array", "string", "number", "integer", "boolean", "null"];
  if ("type" in schema) {
    const declared = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (declared.length === 0 || new Set(declared).size !== declared.length
      || !declared.every((type) => typeof type === "string" && types.includes(type))) return false;
  }
  if ("required" in schema && (!isStringArray(schema.required)
    || new Set(schema.required).size !== schema.required.length)) return false;
  if ("enum" in schema && (!Array.isArray(schema.enum) || schema.enum.length === 0)) return false;
  for (const key of ["$ref", "$id", "$schema", "$anchor", "title", "description", "format", "pattern"]) {
    if (key in schema && typeof schema[key] !== "string") return false;
  }
  if (typeof schema.pattern === "string") {
    try { new RegExp(schema.pattern); } catch { return false; }
  }
  for (const key of ["properties", "patternProperties", "$defs", "definitions", "dependentSchemas"]) {
    if (key in schema && (!isPlainObject(schema[key]) || !Object.values(schema[key]).every(isSchema))) return false;
  }
  for (const key of ["additionalProperties", "additionalItems", "unevaluatedProperties", "unevaluatedItems",
    "contains", "propertyNames", "not", "if", "then", "else"]) {
    if (key in schema && !isSchema(schema[key])) return false;
  }
  for (const key of ["allOf", "anyOf", "oneOf", "prefixItems"]) {
    const value = schema[key];
    if (key in schema && (!Array.isArray(value) || value.length === 0 || !value.every(isSchema))) return false;
  }
  if ("items" in schema && !(Array.isArray(schema.items) ? schema.items.every(isSchema) : isSchema(schema.items))) {
    return false;
  }
  for (const key of ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "minContains", "maxContains"]) {
    const value = schema[key];
    if (!(key in schema)) continue;
    if (typeof value !== "number" || !Number.isFinite(value)) return false;
    if (key === "multipleOf" && value <= 0) return false;
    if ((key.startsWith("min") || key.startsWith("max")) && key !== "minimum" && key !== "maximum"
      && (!Number.isInteger(value) || value < 0)) return false;
  }
  for (const key of ["uniqueItems", "readOnly", "writeOnly", "deprecated"]) {
    if (key in schema && typeof schema[key] !== "boolean") return false;
  }
  return true;
}

function isLegalSchema(schema: unknown): schema is Record<string, unknown> {
  return isPlainObject(schema) && (!("type" in schema) || schema.type === "object") && isSchema(schema);
}

function listedTools(tools: unknown): ListedTool[] {
  if (!Array.isArray(tools)) {
    throw new Error("its answer has no tools list");
  }
  const seen = new Set<string>();
  const ready: ListedTool[] = [];
  tools.forEach((entry, index) => {
    if (!isPlainObject(entry) || typeof entry.name !== "string" || !/^[A-Za-z0-9_-]+$/.test(entry.name)) {
      throw new Error(`tools[${index}] has an illegal name`);
    }
    if (seen.has(entry.name)) {
      throw new Error(`duplicate tool name "${entry.name}"`);
    }
    if (typeof entry.description !== "string") {
      throw new Error(`tool "${entry.name}" needs a string description`);
    }
    if (!isLegalSchema(entry.inputSchema)) {
      throw new Error(`tool "${entry.name}" has an illegal inputSchema`);
    }
    seen.add(entry.name);
    ready.push({ name: entry.name, description: entry.description, inputSchema: entry.inputSchema });
  });
  return ready;
}

/** The same normalization as pi's MCP adapter: object type and a properties map, nothing rebuilt. */
function toParameters(schema: Record<string, unknown>): ParametersSchema {
  return {
    ...schema,
    type: schema.type ?? "object",
    ...(schema.properties === undefined ? { properties: {} } : {}),
  } as ParametersSchema;
}

function timeoutFor(name: string, args: Record<string, unknown>): number {
  const wait = name === WAITING_TOOL ? args.wait_seconds : undefined;
  if (typeof wait === "number" && Number.isFinite(wait) && wait > 0) {
    return Math.max(REQUEST_TIMEOUT_MS, wait * 1000 + WAIT_MARGIN_MS);
  }
  return REQUEST_TIMEOUT_MS;
}

function requestOnce(
  binding: Binding,
  path: string,
  method: "GET" | "POST",
  body: Record<string, unknown> | null,
  timeoutMs: number,
  signal: AbortSignal | undefined,
): Promise<{ status: number; raw: Buffer }> {
  const target = new URL(binding.url + path);
  const transport = target.protocol === "https:" ? httpsRequest : httpRequest;
  const payload = body === null ? null : Buffer.from(JSON.stringify(body));
  const headers: Record<string, string> = {
    Authorization: `Bearer ${binding.token}`,
    Accept: "application/json",
  };
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = String(payload.length);
  }
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new BridgeTimeout("the call was cancelled"));
      return;
    }
    let settled = false;
    let req: ClientRequest | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (settle: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      settle();
    };
    const fail = (error: BridgeError) => finish(() => {
      req?.destroy();
      reject(error);
    });
    const onAbort = () => fail(new BridgeTimeout("the call was cancelled"));
    // Parser/transport messages can include raw headers. Only expose the code,
    // never the native error object, response headers or an unparsed body.
    const unavailable = (error?: NodeJS.ErrnoException) => new BridgeError(
      `no answer from ${binding.url} (${error?.code ?? "connection closed"})`,
    );
    try {
      req = transport({
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port === "" ? undefined : target.port,
        path: `${target.pathname}${target.search}`,
        method,
        headers,
        agent: target.protocol === "https:" ? directHttps : directHttp,
      }, (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          finish(() => resolve({ status: response.statusCode ?? 0, raw: Buffer.concat(chunks) }));
        });
        response.on("error", (error: NodeJS.ErrnoException) => fail(unavailable(error)));
        response.on("close", () => {
          if (!response.complete) fail(unavailable());
        });
      });
      req.on("error", (error: NodeJS.ErrnoException) => {
        if (signal?.aborted) onAbort();
        else fail(unavailable(error));
      });
      // A wall-clock deadline also bounds DNS, connect and a trickling body;
      // ClientRequest's timeout option only bounds socket inactivity.
      timer = setTimeout(() => fail(new BridgeTimeout(
        `no answer from ${binding.url} within ${timeoutMs / 1000} s`,
      )), timeoutMs);
      signal?.addEventListener("abort", onAbort, { once: true });
      if (signal?.aborted) {
        onAbort();
        return;
      }
      if (payload !== null) req.write(payload);
      req.end();
    } catch (error) {
      fail(unavailable(error as NodeJS.ErrnoException));
    }
  });
}

async function readObject(
  binding: Binding,
  path: string,
  method: "GET" | "POST",
  body: Record<string, unknown> | null,
  timeoutMs: number,
  signal: AbortSignal | undefined,
): Promise<Record<string, unknown>> {
  const { status, raw } = await requestOnce(binding, path, method, body, timeoutMs, signal);
  let payload: unknown = null;
  try {
    payload = JSON.parse(raw.toString("utf8"));
  } catch {
    payload = null;
  }
  if (status !== 200) {
    const error = isPlainObject(payload) && typeof payload.error === "string" ? payload.error : "";
    throw new BridgeError(`HTTP ${status}${error ? `: ${error}` : ""}`);
  }
  if (!isPlainObject(payload)) {
    throw new Error("its answer is not a JSON object");
  }
  return payload;
}

function textResult(text: string, isError: boolean): ToolResult {
  return { content: [{ type: "text", text }], details: undefined, isError };
}

function errorMessage(error: unknown, token: string): string {
  return (error instanceof Error ? error.message : String(error)).replaceAll(token, "[redacted]");
}

async function callTool(
  binding: Binding,
  name: string,
  params: unknown,
  signal: AbortSignal | undefined,
): Promise<ToolResult> {
  const args = isPlainObject(params) ? params : {};
  const timeoutMs = timeoutFor(name, args);
  try {
    const answer = await readObject(
      binding,
      `/api/agent/v1/ops/${encodeURIComponent(name)}`,
      "POST",
      args,
      timeoutMs,
      signal,
    );
    // The bearer stays transport-only, even if an API answer echoes it.
    const text = JSON.stringify(answer).replaceAll(JSON.stringify(binding.token).slice(1, -1), "[redacted]");
    return textResult(text, answer.status === "error");
  } catch (error) {
    const reason = errorMessage(error, binding.token);
    if (error instanceof BridgeTimeout) {
      return textResult(
        `Looper did not answer the call to ${name}: ${reason}. It may still have run, `
        + "so check its effect before calling it again.",
        true,
      );
    }
    return textResult(`Looper did not run the tool ${name}: ${reason}`, true);
  }
}

export default async function (pi: ExtensionAPI): Promise<void> {
  const binding = readBinding();
  if (binding === null) return;
  try {
    const answer = await readObject(binding, "/api/agent/v1/tools", "GET", null, REQUEST_TIMEOUT_MS, undefined);
    const tools = listedTools(answer.tools);
    const registrations = tools.map((tool) => ({
      name: `mcp__looper__${tool.name}`,
      label: `looper/${tool.name}`,
      description: tool.description,
      parameters: toParameters(tool.inputSchema),
      original: tool.name,
    }));
    for (const tool of registrations) {
      pi.registerTool({
        name: tool.name,
        label: tool.label,
        description: tool.description,
        parameters: tool.parameters,
        execute: (_toolCallId, params, signal) => callTool(binding, tool.original, params, signal),
      });
    }
  } catch (error) {
    throw new Error(errorMessage(error, binding.token));
  }
}
