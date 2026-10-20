// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, expectTypeOf, it, vi } from "vitest";
import { ProvidersSection } from "@/components/provider-settings";
import { createProvider, updateProvider, type AgentProfileV3, type ProfileFieldsV3, type ProviderFieldsV3, type ProviderV3, type Registry, type RegistryV3, type VendorTypeV3 } from "@/lib/api";

import { useRegistry, useRegistryV3 } from "@/lib/use-registry";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const endpoint = (protocol: "anthropic-messages" | "openai-chat" | "openai-responses", base_url: string) =>
  ({ protocol, base_url, use_runtime_default: false });
const VENDORS: VendorTypeV3[] = [
  { vendor_type: "deepseek", auth: "api_key", oauth_runtime_type: null,
    endpoints: [endpoint("anthropic-messages", "https://example.invalid/anthropic"),
      endpoint("openai-chat", "https://example.invalid/v1"), endpoint("openai-responses", "https://example.invalid/responses")],
    env: [{ key: "API_TIMEOUT_MS", value: "3000000" }], requires_url_confirmation: false,
    suggested_models: [{ name: "suggestion-only", protocol: "openai-chat" }] },
  { vendor_type: "custom", auth: "api_key", oauth_runtime_type: null, endpoints: [], env: [],
    requires_url_confirmation: false, suggested_models: [] },
  { vendor_type: "codex-login", auth: "oauth", oauth_runtime_type: "codex",
    endpoints: [endpoint("openai-responses", "")], env: [], requires_url_confirmation: false,
    suggested_models: [{ name: "login-suggestion", protocol: "openai-responses" }] },
  { vendor_type: "opencode", auth: "api_key", oauth_runtime_type: null,
    endpoints: [endpoint("openai-chat", "https://example.invalid/confirm")], env: [],
    requires_url_confirmation: true, suggested_models: [] },
];
const account = (changes: Partial<ProviderV3> = {}): ProviderV3 => ({
  provider_id: "account-1", name: "Saved account", vendor_type: "deepseek", auth: "api_key",
  oauth_runtime_type: null, endpoints: [endpoint("openai-chat", "https://saved.invalid/v2")],
  env: [], max_concurrency: 3, api_key: "***", builtin: false, login: null, suggested_models: [],
  endpoint_count: 1, model_count: 2, profile_count: 4, created_at: "", updated_at: "", ...changes,
});
const registry = (): RegistryV3 => ({
  runtime_types: [], runtimes: [], providers: [], models: [], profiles: [], vendor_types: VENDORS,
  prices: { as_of: "", sources: [], models: {} },
  limits: { name_max_chars: 100, models_max: 50, price_fields: [], model_fields: [],
    protocols: ["anthropic-messages", "openai-chat", "openai-responses"], auth_kinds: ["api_key", "oauth"],
    effort_levels: [], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
    secret_mask: "***", provider_env_owned: [] },
});
let data: RegistryV3;
let container: HTMLDivElement;
let root: Root;
let bodies: Record<string, unknown>[];
let failure: string;
let warnings: string[];
beforeEach(() => {
  data = registry(); bodies = []; failure = ""; warnings = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/registry") return Response.json(data);
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    bodies.push(body);
    if (failure) return Response.json({ error: failure }, { status: 409 });
    const saved = account({ ...body, api_key: body.api_key === null ? null : "***" } as Partial<ProviderV3>);
    data = { ...data, providers: [saved] };
    return Response.json({ provider: saved, warnings });
  }));
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });
const render = async (adding = true) => { await act(async () => root.render(<ProvidersSection adding={adding} />)); };
function field(label: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement {
  const item = [...container.querySelectorAll("label")].find((l) => l.textContent === label);
  expect(item, `field ${label}`).toBeTruthy();
  return document.getElementById(item!.htmlFor) as HTMLInputElement;
}
async function fill(label: string, value: string) {
  await act(async () => {
    const item = field(label);
    const proto = item instanceof HTMLSelectElement ? HTMLSelectElement.prototype :
      item instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(item, value);
    item.dispatchEvent(new Event(item instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
async function click(text: string) {
  const button = [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === text || b.getAttribute("aria-label") === text);
  expect(button, `button ${text}`).toBeTruthy();
  await act(async () => button!.click());
}
const submit = async () => { await act(async () => container.querySelector("form")!.dispatchEvent(
  new Event("submit", { bubbles: true, cancelable: true }),
)); };
async function toggle(protocol: string) {
  const label = [...container.querySelectorAll("label")].find((l) => l.textContent === protocol)!;
  await act(async () => label.querySelector("input")!.click());
}

it("offers custom and built-in vendors together and starts a blank custom account", async () => {
  await render();
  const select = field("Vendor") as HTMLSelectElement;
  expect(select.value).toBe("custom");
  expect([...select.options].map((o) => o.value)).toEqual(expect.arrayContaining(VENDORS.map((v) => v.vendor_type)));
  expect(select.querySelector("optgroup")).toBeNull();
  expect(field("Name *").value).toBe("");
  expect(field("Extra environment").value).toBe("");
  await toggle("openai-chat");
  await fill("openai-chat URL", "https://custom.invalid/api");
  await fill("Name *", "Custom account");
  await submit();
  expect(bodies[0]).toEqual({ name: "Custom account", vendor_type: "custom", env: [], max_concurrency: null,
    endpoints: [endpoint("openai-chat", "https://custom.invalid/api")] });
});

it("prefills all vendor endpoints once, saves their own URLs and never submits suggested models", async () => {
  data.models = [{ model_id: "model-1", provider_id: "account-1", provider_name: "Saved account",
    name: "separate-model", protocol: "openai-chat", context_window: 100000, efforts: null, background_model: null,
    input_usd_per_mtok: 2, output_usd_per_mtok: null, cache_read_usd_per_mtok: null, cache_write_usd_per_mtok: null,
    catalog_order: 0, builtin: false, profile_count: 1, endpoint: endpoint("openai-chat", "https://saved.invalid/v2"),
    prices: {
      input_usd_per_mtok: { override: 2, effective: 2, source: "override" },
      output_usd_per_mtok: { override: null, effective: null, source: "unknown" },
      cache_read_usd_per_mtok: { override: null, effective: null, source: "unknown" },
      cache_write_usd_per_mtok: { override: null, effective: null, source: "unknown" },
    }, warnings: [], created_at: "", updated_at: "" }];
  await render(); await fill("Vendor", "deepseek");
  expect(container.textContent).not.toContain("separate-model");
  expect(field("Name *").value).toBe("deepseek");
  expect(field("Extra environment").value).toBe("API_TIMEOUT_MS=3000000");
  for (const e of VENDORS[0].endpoints) expect(field(`${e.protocol} URL`).value).toBe(e.base_url);
  await fill("anthropic-messages URL", "https://edited.invalid/anthropic");
  await fill("openai-chat URL", "https://edited.invalid/chat");
  await fill("Max concurrent runs", "7"); await fill("API key", "synthetic-key"); await submit();
  expect(bodies[0]).toEqual({ name: "deepseek", vendor_type: "deepseek", max_concurrency: 7,
    env: VENDORS[0].env, api_key: "synthetic-key", endpoints: [
      endpoint("anthropic-messages", "https://edited.invalid/anthropic"),
      endpoint("openai-chat", "https://edited.invalid/chat"), VENDORS[0].endpoints[2]] });
  expect(container.querySelector("form")).toBeNull();
  expect(container.textContent).not.toContain("suggestion-only");
  expect(container.querySelector("table")).toBeNull();
});

it("keeps migrated runtime defaults until an explicit endpoint URL replaces them", async () => {
  data.providers = [account({ vendor_type: "custom", endpoints: [{ ...endpoint("openai-chat", ""), use_runtime_default: true }] })];
  await render(false); expect(container.textContent).toContain("runtime default");
  await click("Edit provider Saved account"); await submit();
  expect(bodies[0].endpoints).toEqual([{ ...endpoint("openai-chat", ""), use_runtime_default: true }]);
  await click("Edit provider Saved account"); await fill("openai-chat URL", "https://explicit.invalid/v1"); await submit();
  expect(bodies[1].endpoints).toEqual([endpoint("openai-chat", "https://explicit.invalid/v1")]);
});

it("switching back to custom clears vendor endpoints and environment", async () => {
  await render(); await fill("Vendor", "deepseek"); await fill("Vendor", "custom");
  expect(field("Name *").value).toBe(""); expect(field("Extra environment").value).toBe("");
  expect(container.querySelector('input[id$="-url"]')).toBeNull();
});

it.each(["keep", "replace", "remove"])("%s a masked key without putting its mask in the password input", async (mode) => {
  data.providers = [account()]; await render(false); await click("Edit provider Saved account");
  expect(field("API key").value).toBe(""); expect(container.textContent).not.toContain("synthetic-key");
  if (mode === "replace") await fill("API key", "synthetic-replacement");
  if (mode === "remove") await act(async () => {
    const label = [...container.querySelectorAll("label")].find((l) => l.textContent?.includes("Remove the stored key"))!;
    label.querySelector("input")!.click();
  });
  await submit();
  expect(bodies[0].api_key).toBe(mode === "keep" ? "***" : mode === "replace" ? "synthetic-replacement" : null);
});

it("reads saved vendor values after reload without reapplying defaults and keeps the vendor immutable", async () => {
  data.providers = [account()]; await render(false);
  expect(container.textContent).toContain("2 models"); expect(container.textContent).toContain("used by 4 Agent Profiles");
  await click("Edit provider Saved account");
  expect((field("Vendor") as HTMLSelectElement).disabled).toBe(true);
  expect(field("openai-chat URL").value).toBe("https://saved.invalid/v2");
  expect(field("Extra environment").value).toBe("");
  await fill("openai-chat URL", "https://changed.invalid/v3"); await submit();
  await click("Edit provider Saved account");
  expect(field("openai-chat URL").value).toBe("https://changed.invalid/v3");
  expect(field("Extra environment").value).toBe("");
  expect(container.querySelectorAll('input[id$="-url"]')).toHaveLength(1);
});

it("shows login state and prevents editing a login account's endpoint or key", async () => {
  data.providers = [account({ vendor_type: "codex-login", auth: "oauth", oauth_runtime_type: "codex",
    api_key: null, endpoints: [endpoint("openai-responses", "")], login: { state: "chatgpt", detail: "ChatGPT login" } })];
  await render(false); expect(container.textContent).toContain("Login: ChatGPT login");
  await click("Edit provider Saved account");
  expect((field("openai-responses URL") as HTMLInputElement).disabled).toBe(true);
  expect([...container.querySelectorAll("label")].some((l) => l.textContent === "API key")).toBe(false);
  await fill("Max concurrent runs", "5"); await submit();
  expect(bodies[0].endpoints).toEqual([endpoint("openai-responses", "")]);
  expect(bodies[0]).not.toHaveProperty("api_key");
});

it("new login accounts use their runtime endpoint without an editable URL", async () => {
  await render(); await fill("Vendor", "codex-login");
  expect((field("openai-responses URL") as HTMLInputElement).disabled).toBe(true);
  await submit(); expect(bodies[0].vendor_type).toBe("codex-login");
  expect(bodies[0].endpoints).toEqual([endpoint("openai-responses", "")]);
});

it("requires explicit confirmation of self-hosted vendor URLs before saving", async () => {
  await render(); await fill("Vendor", "opencode"); await submit();
  expect(bodies).toHaveLength(0); expect(container.querySelector('[role="alert"]')).toBeTruthy();
  const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"][aria-label="Confirm endpoint URLs"]')!;
  expect(checkbox).toBeTruthy(); await act(async () => checkbox.click()); await submit();
  expect(bodies[0].endpoints).toEqual(VENDORS[3].endpoints);
});

it.each(["save", "delete"])("reads back reference errors on %s and keeps the account available", async (operation) => {
  data.providers = [account()]; failure = "Model 'in-use' uses this endpoint; Agent Profile 'worker' references it";
  await render(false);
  if (operation === "save") {
    await click("Edit provider Saved account"); await toggle("openai-chat"); await submit();
    expect(bodies[0].endpoints).toEqual([]); expect(container.querySelector("form")).not.toBeNull();
  } else {
    vi.stubGlobal("confirm", () => true); await click("Delete provider Saved account");
  }
  expect(container.querySelector('[role="alert"]')?.textContent).toBe(failure);
  expect(container.textContent).toContain("Saved account");
});

it("retains server warnings until acknowledged", async () => {
  warnings = ["The server returned an account warning"];
  await render(); await fill("Vendor", "deepseek"); await submit();
  expect(container.querySelector('[role="status"]')?.textContent).toContain(warnings[0]);
  expect(container.querySelector("form")).not.toBeNull(); await click("OK");
  expect(container.querySelector("form")).toBeNull();
});

it("keeps normalized DTOs separate from legacy consumers", () => {
  expectTypeOf<ReturnType<typeof useRegistry>["registry"]>().toEqualTypeOf<Registry | null>();
  expectTypeOf<ReturnType<typeof useRegistryV3>["registry"]>().toEqualTypeOf<RegistryV3 | null>();
  expectTypeOf<Parameters<typeof createProvider>[0]>().toEqualTypeOf<ProviderFieldsV3>();
  expectTypeOf<Parameters<typeof updateProvider>[1]>().toEqualTypeOf<ProviderFieldsV3>();
  expectTypeOf<ProviderV3>().not.toHaveProperty("models");
  expectTypeOf<ProviderFieldsV3>().not.toHaveProperty("base_url");
  expectTypeOf<ProfileFieldsV3>().not.toHaveProperty("provider_id");
  expectTypeOf<AgentProfileV3["model"]>().toHaveProperty("model_id");
});
