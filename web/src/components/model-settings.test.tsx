// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProvidersSection } from "@/components/provider-settings";
import type { AgentProfile, Model, PriceField, Provider, Registry } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const PRICE_FIELDS: PriceField[] = ["input_usd_per_mtok", "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok"];
const endpoint = (protocol: Model["protocol"]) => ({ protocol, base_url: "https://example.invalid/v1", use_runtime_default: false });
const provider = (changes: Partial<Provider> = {}): Provider => ({
  provider_id: "provider-1", name: "Account", vendor_type: "deepseek", auth: "api_key", oauth_runtime_type: null,
  endpoints: [endpoint("openai-chat"), endpoint("anthropic-messages")], suggested_models: [], login: null,
  env: [], max_concurrency: null, api_key: "***", builtin: false, endpoint_count: 2, model_count: 0,
  profile_count: 0, created_at: "", updated_at: "", ...changes,
});
const model = (changes: Partial<Model> = {}): Model => ({
  model_id: "model-1", provider_id: "provider-1", provider_name: "Account", name: "exact[1m]", protocol: "openai-chat",
  context_window: null, efforts: null, background_model: null, input_usd_per_mtok: null, output_usd_per_mtok: null,
  cache_read_usd_per_mtok: null, cache_write_usd_per_mtok: null, catalog_order: 0, builtin: false, profile_count: 0,
  endpoint: endpoint("openai-chat"), prices: Object.fromEntries(PRICE_FIELDS.map((key) => [key,
    { override: null, effective: null, source: "unknown" }])) as Model["prices"], warnings: [], created_at: "", updated_at: "", ...changes,
});
let data: Registry;
let root: Root;
let container: HTMLDivElement;
let writes: { url: string; method: string; body: Record<string, unknown> }[];
let failure: string;
let warnings: string[];
beforeEach(() => {
  writes = []; failure = ""; warnings = [];
  data = {
    runtime_types: [], runtimes: [], providers: [provider()], models: [], profiles: [],
    vendor_types: [{ vendor_type: "deepseek", auth: "api_key", oauth_runtime_type: null,
      endpoints: [], env: [], requires_url_confirmation: false,
      suggested_models: [{ name: "vendor-exact", protocol: "openai-chat", context_window: 128000, efforts: [], input_usd_per_mtok: 0 },
        { name: "vendor-exact", protocol: "anthropic-messages", context_window: 1000000, background_model: "vendor-small" }] }],
    prices: { as_of: "", sources: [], models: {} },
    limits: { name_max_chars: 100, models_max: 50, price_fields: PRICE_FIELDS, model_fields: [],
      protocols: ["openai-chat", "anthropic-messages", "openai-responses"], auth_kinds: ["api_key", "oauth"],
      effort_levels: ["low", "high"], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
      secret_mask: "***", provider_env_owned: [] },
  };
  vi.stubGlobal("confirm", () => true);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/v1/registry/view") return Response.json({ status: "ok", result: data });
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    writes.push({ url, method: init!.method!, body });
    if (!url.startsWith("/api/models")) throw new Error(`Unexpected write: ${url}`);
    if (failure) return Response.json({ error: failure }, { status: init?.method === "DELETE" ? 409 : 400 });
    const id = decodeURIComponent(url.split("/").at(-1)!);
    if (init?.method === "DELETE") {
      data = { ...data, models: data.models.filter((m) => m.model_id !== id) };
      return Response.json({ deleted: id });
    }
    const existing = data.models.find((m) => m.model_id === id);
    const saved = model({ model_id: `model-${data.models.length + 1}`, ...existing, ...body });
    saved.prices = Object.fromEntries(PRICE_FIELDS.map((key) => [key, { override: saved[key], effective: saved[key],
      source: saved[key] === null ? "unknown" : "override" }])) as Model["prices"];
    data = { ...data, models: [...data.models.filter((m) => m.model_id !== saved.model_id), saved] };
    return Response.json({ model: saved, warnings });
  }));
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
async function click(name: string) {
  const button = [...container.querySelectorAll("button")].find((b) => b.getAttribute("aria-label") === name || b.textContent?.trim() === name);
  expect(button, name).toBeTruthy(); await act(async () => button!.click());
}
function field(label: string): HTMLInputElement | HTMLSelectElement {
  const element = [...container.querySelectorAll("label")].find((l) => l.textContent === label);
  expect(element, label).toBeTruthy(); return document.getElementById(element!.htmlFor) as HTMLInputElement;
}
async function fill(label: string, value: string) {
  await act(async () => {
    const item = field(label);
    const proto = item instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(item, value);
    item.dispatchEvent(new Event(item instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
async function render() { await act(async () => root.render(<ProvidersSection />)); }
async function open() { await render(); await click("Models for provider Account"); }
async function add(protocol = "openai-chat", name = "exact[1m]") {
  await click("Add model"); await fill("Protocol *", protocol); await fill("Exact model name *", name);
}
async function submit() { await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }))); }
const edit = (protocol = "openai-chat") => click(`Edit model exact[1m] (${protocol})`);

it("places models only inside the expanded provider and scopes them to that account", async () => {
  data.models = [model(), model({ model_id: "model-other", provider_id: "other", name: "Other account model" })];
  await render(); expect(container.textContent).not.toContain("exact[1m]");
  await click("Models for provider Account");
  expect(container.textContent).toContain("exact[1m]"); expect(container.textContent).not.toContain("Other account model");
  await click("Models for provider Account"); expect(container.textContent).not.toContain("exact[1m]");
});

it("creates and edits identical exact names independently for two protocols using only model APIs", async () => {
  await open(); await add(); await submit();
  await add("anthropic-messages"); await submit();
  expect(writes.slice(0, 2).map((w) => [w.url, w.method, w.body.provider_id, w.body.name, w.body.protocol])).toEqual([
    ["/api/models", "POST", "provider-1", "exact[1m]", "openai-chat"],
    ["/api/models", "POST", "provider-1", "exact[1m]", "anthropic-messages"],
  ]);
  for (const protocol of ["openai-chat", "anthropic-messages"]) {
    await edit(protocol);
    expect(field("Protocol *").disabled).toBe(true); expect(field("Exact model name *").disabled).toBe(true);
    await fill("Background model", `small-${protocol}`); await submit();
    const body = writes.at(-1)!.body;
    expect(body).not.toHaveProperty("provider_id"); expect(body).not.toHaveProperty("name"); expect(body).not.toHaveProperty("protocol");
  }
  expect(writes.slice(2).map((w) => [w.url, w.method])).toEqual([["/api/models/model-1", "PUT"], ["/api/models/model-2", "PUT"]]);
  expect(data.models.map((m) => m.background_model)).toEqual(["small-openai-chat", "small-anthropic-messages"]);
  expect(container.textContent).toContain("small-openai-chat"); expect(container.textContent).toContain("small-anthropic-messages");
});

it("requires protocol before the exact name and copies only that API's explicitly chosen vendor suggestion", async () => {
  await open(); await click("Add model");
  expect(field("Exact model name *").disabled).toBe(true); expect(writes).toHaveLength(0);
  await fill("Protocol *", "openai-chat");
  expect(field("Exact model name *").value).toBe("");
  await fill("Vendor suggestion", "vendor-exact");
  expect(field("Context window").value).toBe("128000"); expect(field("Effort support").value).toBe("none");
  expect(writes).toHaveLength(0); await submit();
  expect(writes[0].body).toMatchObject({ protocol: "openai-chat", context_window: 128000, efforts: [], input_usd_per_mtok: 0 });
  await add("anthropic-messages", ""); await fill("Vendor suggestion", "vendor-exact");
  expect(field("Context window").value).toBe("1000000"); await submit();
  expect(writes[1].body).toMatchObject({ name: "vendor-exact", protocol: "anthropic-messages", background_model: "vendor-small" });
});

it("keeps custom model names exact without deriving suffixes from context or protocol", async () => {
  data.providers = [provider({ vendor_type: "custom" })]; await open(); await add("openai-chat", "My.Model-V2[1m]");
  await fill("Context window", "200000"); await submit();
  expect(writes[0].body.name).toBe("My.Model-V2[1m]"); expect(container.textContent).toContain("My.Model-V2[1m]");
});

it("clears suggestions and their settings when choosing another protocol", async () => {
  await open(); await add("openai-chat", ""); await fill("Vendor suggestion", "vendor-exact");
  await fill("Protocol *", "anthropic-messages");
  expect(field("Exact model name *").value).toBe(""); expect(field("Context window").value).toBe("");
  expect(field("Effort support").value).toBe("unknown"); expect(field("Input price").value).toBe("");
  expect(writes).toHaveLength(0);
});

it("shows context, efforts, background, usage and all four effective price sources", async () => {
  const prices = model().prices;
  prices.input_usd_per_mtok = { override: 0, effective: 0, source: "override" };
  prices.output_usd_per_mtok = { override: null, effective: 2, source: "builtin" };
  data.models = [model({ context_window: 123456, efforts: ["high"], background_model: "small", profile_count: 2, prices })];
  await open();
  for (const text of ["123456", "high", "small", "2 Agent Profiles", "0 (override)", "2 (builtin)", "unknown"]) expect(container.textContent).toContain(text);
  expect(container.textContent).toContain("Cache read"); expect(container.textContent).toContain("Cache write");
  await edit(); expect(field("Output price").value).toBe(""); await submit();
  expect(writes[0].body.output_usd_per_mtok).toBeNull();
});

it.each(PRICE_FIELDS)("preserves zero and explicitly clears the %s override with null", async (key) => {
  const labels = ["Input price", "Output price", "Cache read price", "Cache write price"];
  const label = labels[PRICE_FIELDS.indexOf(key)];
  data.models = [model({ [key]: 0 })]; await open(); await edit(); expect(field(label).value).toBe("0");
  await submit(); expect(writes[0].body[key]).toBe(0);
  await edit(); await fill(label, ""); await submit(); expect(writes[1].body[key]).toBeNull();
});

it.each([{ efforts: null }, { efforts: [] }, { efforts: ["low", "high"] }])("round trips $efforts without conflating unknown and none", async ({ efforts }) => {
  data.models = [model({ efforts })]; await open(); await edit(); await submit(); expect(writes[0].body.efforts).toEqual(efforts);
  await edit(); await fill("Effort support", efforts === null ? "none" : "unknown"); await submit();
  expect(writes[1].body.efforts).toEqual(efforts === null ? [] : null);
});

it("saves supported efforts and clears context and background explicitly", async () => {
  data.models = [model({ context_window: 100000, background_model: "old-background" })]; await open(); await edit();
  await fill("Effort support", "supported"); await fill("Supported efforts", "low, high");
  await fill("Context window", ""); await fill("Background model", ""); await submit();
  expect(writes[0].body).toMatchObject({ efforts: ["low", "high"], context_window: null, background_model: null });
  await edit(); expect(field("Supported efforts").value).toBe("low, high");
});

it("requires a provider endpoint before a model can be created", async () => {
  data.providers = [provider({ endpoints: [] })]; await open(); await click("Add model");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("endpoint");
  expect((field("Protocol *") as HTMLSelectElement).options).toHaveLength(1);
  expect(field("Exact model name *").disabled).toBe(true); await submit(); expect(writes).toHaveLength(0);
});

it("lists the profiles affected by editing this exact model and explains effort changes", async () => {
  data.models = [model({ profile_count: 1 })];
  data.profiles = [{ profile_id: "profile-1", model_id: "model-1", name: "Affected worker" },
    { profile_id: "profile-2", model_id: "another-model", name: "Unrelated worker" }] as AgentProfile[];
  await open(); await edit();
  expect(container.textContent).toContain("Affected worker"); expect(container.textContent).not.toContain("Unrelated worker");
  expect(container.textContent).toMatch(/effort.*runs/i);
});

it.each(["missing endpoint", "referenced delete", "incompatible edit"])("keeps server errors visible for %s", async (operation) => {
  data.models = [model({ profile_count: 2 })]; await open();
  if (operation === "missing endpoint") {
    await add(); failure = "protocol 'openai-chat' needs an endpoint on this provider; available protocols: anthropic-messages";
    await submit(); expect(container.querySelector("form")).toBeTruthy();
  } else if (operation === "referenced delete") {
    failure = "this model still has Agent Profile references: remove or repoint them first";
    await click("Delete model exact[1m] (openai-chat)");
    expect(writes[0]).toMatchObject({ url: "/api/models/model-1", method: "DELETE" });
  } else {
    await edit(); failure = "Agent Profile 'worker' uses this model: incompatible context"; await submit();
    expect(container.querySelector("form")).toBeTruthy();
  }
  expect(container.querySelector('[role="alert"]')?.textContent).toBe(failure); expect(data.models).toHaveLength(1);
});

it("deletes through the model API and reloads the list", async () => {
  data.models = [model({ model_id: "model/a b" })]; await open(); await click("Delete model exact[1m] (openai-chat)");
  expect(writes[0]).toMatchObject({ url: "/api/models/model%2Fa%20b", method: "DELETE" });
  expect(container.textContent).not.toContain("exact[1m]");
});

it("keeps successful save warnings until acknowledged", async () => {
  warnings = ["No full price: spend caps may not apply."]; await open(); await add(); await submit();
  expect(container.querySelector('[role="status"]')?.textContent).toContain(warnings[0]);
  await click("OK"); expect(container.querySelector("form")).toBeNull(); expect(container.textContent).toContain("exact[1m]");
});

it("allows inspecting built-in providers and models without offering built-in mutation", async () => {
  data.providers = [provider({ builtin: true })]; data.models = [model({ builtin: true })]; await open();
  expect(container.textContent).toContain("exact[1m]");
  expect(container.querySelector('[aria-label="Edit model exact[1m] (openai-chat)"]')).toBeNull();
  expect(container.querySelector('[aria-label="Delete model exact[1m] (openai-chat)"]')).toBeNull();
});
