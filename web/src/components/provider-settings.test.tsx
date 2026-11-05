// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, expectTypeOf, it, vi } from "vitest";
import { ProvidersSection } from "@/components/provider-settings";
import { createProvider, updateProvider, type AgentProfile, type CreateProviderFields, type ProfileFields, type ProviderFields, type Provider, type Registry, type VendorType } from "@/lib/api";

import { useRegistry } from "@/lib/use-registry";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const endpoint = (protocol: "anthropic-messages" | "openai-chat" | "openai-responses", base_url: string) =>
  ({ protocol, base_url, use_runtime_default: false });
const VENDORS: VendorType[] = [
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
const account = (changes: Partial<Provider> = {}): Provider => ({
  provider_id: "account-1", name: "Saved account", vendor_type: "deepseek", auth: "api_key",
  oauth_runtime_type: null, endpoints: [endpoint("openai-chat", "https://saved.invalid/v2")],
  env: [], max_concurrency: 3, api_key: "***", builtin: false, enabled: true, login: null, suggested_models: [],
  endpoint_count: 1, model_count: 2, profile_count: 4, created_at: "", updated_at: "", ...changes,
});
const registry = (): Registry => ({
  runtime_types: [], runtimes: [], providers: [], models: [], profiles: [], vendor_types: VENDORS,
  prices: { as_of: "", sources: [], models: {} },
  limits: { api_formats: [{ family: "anthropic", label: "Anthropic", protocols: [{ protocol: "anthropic-messages", label: "Anthropic Messages" }] }, { family: "openai", label: "OpenAI", protocols: [{ protocol: "openai-responses", label: "OpenAI Responses" }, { protocol: "openai-chat", label: "OpenAI Chat Completions" }] }],  name_max_chars: 100, models_max: 50, price_fields: [], model_fields: [],
    protocols: ["anthropic-messages", "openai-chat", "openai-responses"], auth_kinds: ["api_key", "oauth"],
    effort_levels: [], role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] },
    secret_mask: "***", provider_env_owned: [] },
});
let data: Registry;
let container: HTMLDivElement;
let root: Root;
let bodies: Record<string, unknown>[];
let failure: string;
let warnings: string[];
let requests: { path: string; body: Record<string, unknown> }[];
let registryReads: number;
beforeEach(() => {
  data = registry(); bodies = []; failure = ""; warnings = []; requests = []; registryReads = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/v1/registry/view") {
      registryReads++;
      return Response.json({ status: "ok", result: data });
    }
    const path = String(input);
    expect(init?.method).toBe("POST");
    expect(path).toMatch(/^\/api\/v1\/provider_accounts\/(create|update|delete|clear_login_problem|set_enabled|fetch_models)$/);
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    requests.push({ path, body });
    if (path.endsWith("/fetch_models")) return Response.json({ status: "ok", result: { provider_id: body.provider_id,
      listings: [{ family: "openai", base_url: "https://saved.invalid/v2", protocols: ["openai-chat"], state: "failed",
        problem: "could not connect to the endpoint", truncated: false, models: [] }] } });
    const fields = path.endsWith("/update") ? body.fields : body;
    bodies.push(fields);
    if (failure) return Response.json({ status: "error", error: { code: "conflict", message: failure } }, { status: 409 });
    if (path.endsWith("/delete")) {
      data = { ...data, providers: data.providers.filter((provider) => provider.provider_id !== body.provider_id) };
      return Response.json({ status: "ok", result: { deleted: body.provider_id } });
    }
    const saved: Provider = path.endsWith("/set_enabled")
      ? { ...data.providers.find((provider) => provider.provider_id === body.provider_id)!, enabled: body.enabled }
      : path.endsWith("/clear_login_problem")
      ? { ...data.providers.find((provider) => provider.provider_id === body.provider_id)!, login: { state: "chatgpt", detail: "ChatGPT login" } }
      : account({ ...fields, auth: VENDORS.find((v) => v.vendor_type === fields.vendor_type)?.auth ?? "api_key", api_key: fields.api_key === null ? null : "***" } as Partial<Provider>);
    data = { ...data, providers: [saved] };
    return Response.json({ status: "ok", result: { provider: saved, warnings } });
  }));
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.useRealTimers(); });
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
  expect(container.querySelectorAll('input[type="radio"]')).toHaveLength(2);
  expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
  await click("Set a URL per protocol instead");
  await toggle("openai-chat");
  await fill("openai-chat URL", "https://custom.invalid/api");
  await fill("Name *", "Custom account");
  await submit();
  expect(bodies[0]).toEqual({ name: "Custom account", vendor_type: "custom", env: [], max_concurrency: null,
    endpoints: [endpoint("openai-chat", "https://custom.invalid/api")] });
  expect(requests[0].path).toBe("/api/v1/provider_accounts/create");
  expect(registryReads).toBe(2);
});

it("prefills all vendor endpoints once, saves their own URLs and never submits suggested models", async () => {
  data.models = [{ model_id: "model-1", provider_id: "account-1", provider_name: "Saved account", provider_enabled: true,
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
  expect(container.textContent).toContain("Saved. Tick the models deepseek offers to add them:");
  expect(requests[1]).toEqual({ path: "/api/v1/provider_accounts/fetch_models", body: { provider_id: "account-1" } });
  expect(JSON.stringify(requests[1])).not.toContain("synthetic-key");
  await click("Done");
  expect(container.querySelector("form")).toBeNull();
  expect(container.textContent).not.toContain("suggestion-only");
  expect(container.querySelector("table")).toBeNull();
});

it.each([
  ["OpenAI", "both", ["openai-responses", "openai-chat"]],
  ["OpenAI", "openai-chat", ["openai-chat"]],
  ["OpenAI", "openai-responses", ["openai-responses"]],
  ["Anthropic", "both", ["anthropic-messages"]],
])("saves %s with %s at one URL and opens the model picker", async (format, apis, protocols) => {
  await render(); await toggle(format);
  if (format === "OpenAI") {
    expect(field("OpenAI APIs it serves").value).toBe("both");
    await fill("OpenAI APIs it serves", apis);
  }
  await fill("Name *", "Custom account");
  await fill("Base URL *", "  https://custom.invalid/api/  ");
  expect(container.textContent).not.toContain("api//");
  await submit();
  expect(bodies[0].endpoints).toEqual(protocols.map((protocol) => endpoint(protocol as Provider["endpoints"][number]["protocol"], "https://custom.invalid/api/")));
  expect(requests[1].path).toBe("/api/v1/provider_accounts/fetch_models");
  expect(container.textContent).toContain("Saved. Tick the models Custom account offers to add them:");
});

it.each([
  ["", "https://custom.invalid/v1", "Choose the API format."],
  ["OpenAI", "  ", "Enter the base URL."],
])("validates format %s and URL before sending", async (format, url, error) => {
  await render(); if (format) await toggle(format);
  await fill("Base URL *", url); await submit();
  expect(container.querySelector('[role="alert"]')?.textContent).toBe(error);
  expect(requests).toHaveLength(0);
});

it("describes format URLs and compatible runtime labels using registry data", async () => {
  const runtime = (label: string, protocols: string[]) => ({ label, protocols }) as Registry["runtime_types"][number];
  data.runtime_types = [runtime("Codex", ["openai-responses"]), runtime("Pi", ["openai-chat", "openai-responses"]),
    runtime("Claude Code", ["anthropic-messages"])];
  await render();
  expect(container.textContent).toContain("Spoken by Codex, Pi");
  expect(container.textContent).toContain("Spoken by Claude Code");
  expect(field("Base URL *").getAttribute("placeholder")).toBe("https://api.example.com/v1");
  expect(container.textContent).toContain("<base URL>/models");
  await toggle("Anthropic");
  expect(field("Base URL *").getAttribute("placeholder")).toBe("https://api.example.com/anthropic");
  await fill("Base URL *", "https://custom.invalid/anthropic/");
  expect(container.textContent).toContain("Runs call https://custom.invalid/anthropic/v1/messages; the model list is https://custom.invalid/anthropic/v1/models.");
});

it.each([
  ["both", "https://custom.invalid/v1/responses or https://custom.invalid/v1/chat/completions"],
  ["openai-chat", "https://custom.invalid/v1/chat/completions"],
  ["openai-responses", "https://custom.invalid/v1/responses"],
])("describes only the selected %s OpenAI APIs", async (apis, calls) => {
  await render(); await toggle("OpenAI");
  await fill("OpenAI APIs it serves", apis);
  await fill("Base URL *", "https://custom.invalid/v1/");
  const hint = [...container.querySelectorAll("p")].find((p) => p.textContent?.startsWith("Runs call"));
  expect(hint?.textContent).toBe(`Runs call ${calls}; the model list is https://custom.invalid/v1/models.`);
});

it.each(["https://custom.invalid/v1", "https://custom.invalid/v1/", "https://custom.invalid/"])(
  "describes the stored Anthropic root for %s", async (url) => {
    await render(); await toggle("Anthropic"); await fill("Base URL *", url);
    expect(container.textContent).toContain("Runs call https://custom.invalid/v1/messages; the model list is https://custom.invalid/v1/models.");
  },
);

it.each(["both", "openai-chat", "openai-responses"])("preserves simple URL and %s through the protocol editor", async (apis) => {
  await render(); await toggle("OpenAI"); await fill("OpenAI APIs it serves", apis);
  await fill("Base URL *", "https://custom.invalid/v1/");
  await click("Set a URL per protocol instead");
  const protocols = apis === "both" ? ["openai-responses", "openai-chat"] : [apis];
  for (const protocol of protocols) expect(field(`${protocol} URL`).value).toBe("https://custom.invalid/v1/");
  for (const protocol of protocols) await fill(`${protocol} URL`, "https://changed.invalid/v1");
  await click("Describe it by API format instead");
  expect(field("Base URL *").value).toBe("https://changed.invalid/v1");
  expect(field("OpenAI APIs it serves").value).toBe(apis);
  await submit();
  expect(bodies[0].endpoints).toEqual(protocols.map((protocol) => endpoint(protocol as Provider["endpoints"][number]["protocol"], "https://changed.invalid/v1")));
});

it.each(["https://api.example.com/v1", "https://api.example.com/v1/"])("normalizes %s only in the simple Anthropic form", async (url) => {
  await render(); await toggle("Anthropic"); await fill("Base URL *", url);
  expect(container.textContent).toContain("Looper adds /v1/messages itself, so this is saved as https://api.example.com.");
  await submit();
  expect(bodies[0].endpoints).toEqual([endpoint("anthropic-messages", "https://api.example.com")]);
});

it("preserves the typed Anthropic URL when submitting per protocol", async () => {
  await render(); await toggle("Anthropic");
  await fill("Base URL *", "https://host/gateway/v1/");
  await click("Set a URL per protocol instead");
  expect(field("anthropic-messages URL").value).toBe("https://host/gateway/v1/");
  expect(container.textContent).not.toContain("Looper adds /v1/messages itself");
  await submit();
  expect(bodies[0].endpoints).toEqual([endpoint("anthropic-messages", "https://host/gateway/v1/")]);
});

it("copies the typed URL to every protocol before a format is chosen", async () => {
  await render(); await fill("Base URL *", "https://host/gateway/v1/");
  await click("Set a URL per protocol instead");
  for (const protocol of data.limits.protocols) expect(field(`${protocol} URL`).value).toBe("https://host/gateway/v1/");
  await submit();
  expect(bodies[0].endpoints).toEqual(data.limits.protocols.map((protocol) => endpoint(protocol as Provider["endpoints"][number]["protocol"], "https://host/gateway/v1/")));
});

it.each(["openai-chat", "openai-responses"])("keeps %s after clearing all protocol boxes", async (apis) => {
  await render(); await toggle("OpenAI"); await fill("OpenAI APIs it serves", apis);
  await fill("Base URL *", "https://host/gateway/v1");
  await click("Set a URL per protocol instead"); await toggle(apis);
  await click("Describe it by API format instead");
  expect(field("OpenAI APIs it serves").value).toBe(apis);
  expect(field("Base URL *").value).toBe("https://host/gateway/v1");
  await submit();
  expect(bodies[0].endpoints).toEqual([endpoint(apis as Provider["endpoints"][number]["protocol"], "https://host/gateway/v1")]);
});

it.each(["different URLs", "mixed formats"])("keeps editing per protocol for %s", async (configuration) => {
  await render(); await toggle("OpenAI"); await fill("Base URL *", "https://custom.invalid/v1");
  await click("Set a URL per protocol instead");
  if (configuration === "different URLs") await fill("openai-chat URL", "https://chat.invalid/v1");
  else {
    await toggle("anthropic-messages");
    await fill("anthropic-messages URL", "https://custom.invalid/v1");
  }
  await click("Describe it by API format instead");
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("This provider uses different URLs per protocol; keep editing it per protocol.");
  expect(container.querySelectorAll('input[type="radio"]')).toHaveLength(0);
  expect(field("openai-chat URL").value).toBe(configuration === "different URLs" ? "https://chat.invalid/v1" : "https://custom.invalid/v1");
  expect(field("openai-responses URL").value).toBe("https://custom.invalid/v1");
  if (configuration === "mixed formats") expect(field("anthropic-messages URL").value).toBe("https://custom.invalid/v1");
  if (configuration === "different URLs") {
    await fill("openai-chat URL", "https://custom.invalid/v1");
    await click("Describe it by API format instead");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(field("OpenAI APIs it serves").value).toBe("both");
  }
});

it("keeps the protocol editor for edits of custom providers and preset vendors", async () => {
  data.providers = [account({ vendor_type: "custom" })];
  await render(false); await click("Edit provider Saved account");
  expect(container.querySelectorAll('input[type="radio"]')).toHaveLength(0);
  expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(4);
  expect(field("openai-chat URL").value).toBe("https://saved.invalid/v2");
  await click("Cancel"); await click("Add provider"); await fill("Vendor", "deepseek");
  expect(container.querySelectorAll('input[type="radio"]')).toHaveLength(0);
  for (const entry of VENDORS[0].endpoints) expect(field(`${entry.protocol} URL`).value).toBe(entry.base_url);
});

it("keeps migrated runtime defaults until an explicit endpoint URL replaces them", async () => {
  data.providers = [account({ vendor_type: "custom", endpoints: [{ ...endpoint("openai-chat", ""), use_runtime_default: true }] })];
  await render(false); expect(container.textContent).toContain("runtime default");
  await click("Edit provider Saved account"); await submit();
  expect(bodies[0].endpoints).toEqual([{ ...endpoint("openai-chat", ""), use_runtime_default: true }]);
  await click("Edit provider Saved account"); await fill("openai-chat URL", "https://explicit.invalid/v1"); await submit();
  expect(bodies[1].endpoints).toEqual([endpoint("openai-chat", "https://explicit.invalid/v1")]);
});

it.each(["rename", "replace key"])("preserves an unchanged Anthropic URL when saving %s", async (edit) => {
  const storedUrl = "https://example.invalid/v1/";
  data.providers = [account({ endpoints: [endpoint("anthropic-messages", storedUrl)] })];
  await render(false); await click("Edit provider Saved account");
  expect(field("anthropic-messages URL").getAttribute("placeholder")).toBe("https://api.example.com");
  if (edit === "rename") await fill("Name *", "Renamed account");
  else await fill("API key", "synthetic-replacement");
  await submit();
  expect(bodies[0].endpoints).toEqual([endpoint("anthropic-messages", storedUrl)]);
  expect(bodies[0].api_key).toBe(edit === "rename" ? "***" : "synthetic-replacement");
});

it("switching back to custom clears vendor endpoints and environment", async () => {
  await render(); await toggle("OpenAI"); await fill("Base URL *", "https://old.invalid/v1");
  await fill("OpenAI APIs it serves", "openai-chat"); await click("Set a URL per protocol instead");
  await fill("Vendor", "deepseek"); await fill("Vendor", "custom");
  expect(field("Name *").value).toBe(""); expect(field("Extra environment").value).toBe("");
  expect(field("Base URL *").value).toBe("");
  expect(container.querySelector('input[type="radio"]:checked')).toBeNull();
  await toggle("OpenAI"); expect(field("OpenAI APIs it serves").value).toBe("both");
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
  expect(requests[0]).toEqual({ path: "/api/v1/provider_accounts/update",
    body: { provider_id: "account-1", fields: bodies[0] } });
  expect(registryReads).toBe(2);
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
  expect(container.querySelector("form")).toBeNull();
  expect(requests).toHaveLength(1);
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
  expect(container.querySelector("form")).not.toBeNull(); await click("Done");
  expect(container.querySelector("form")).toBeNull();
});

it("clears a login problem through v1 after the existing confirmation and refreshes the registry", async () => {
  data.providers = [account({ vendor_type: "codex-login", auth: "oauth", oauth_runtime_type: "codex",
    api_key: null, login: { state: "unavailable", detail: "Check the runtime login" } })];
  const confirm = vi.fn(() => true); vi.stubGlobal("confirm", confirm);
  await render(false); await click("Use the login again");
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(requests).toEqual([{ path: "/api/v1/provider_accounts/clear_login_problem", body: { provider_id: "account-1" } }]);
  expect(container.textContent).toContain("Login: ChatGPT login");
  expect(registryReads).toBe(2);
});

it("deletes through v1 after the existing confirmation and refreshes the registry", async () => {
  data.providers = [account()];
  const confirm = vi.fn(() => true); vi.stubGlobal("confirm", confirm);
  await render(false); await click("Delete provider Saved account");
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(requests).toEqual([{ path: "/api/v1/provider_accounts/delete", body: { provider_id: "account-1" } }]);
  expect(container.textContent).not.toContain("Saved account");
  expect(registryReads).toBe(2);
});

it("keeps a pending v1 write visible as an error without refreshing or confirming it", async () => {
  await render();
  await toggle("OpenAI"); await fill("Base URL *", "https://pending.invalid/v1");
  vi.mocked(fetch).mockResolvedValueOnce(Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 60, impact: "This account has active references." }, { status: 202 }));
  await fill("Name *", "Pending account"); await submit();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("active references");
  expect(container.querySelector("form")).not.toBeNull();
  expect(registryReads).toBe(1);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("uses the normalized registry contract throughout", () => {
  expectTypeOf<ReturnType<typeof useRegistry>["registry"]>().toEqualTypeOf<Registry | null>();
  expectTypeOf<Parameters<typeof createProvider>[0]>().toEqualTypeOf<CreateProviderFields>();
  expectTypeOf<Parameters<typeof updateProvider>[1]>().toEqualTypeOf<ProviderFields>();
  expectTypeOf<Provider>().not.toHaveProperty("models");
  expectTypeOf<ProviderFields>().not.toHaveProperty("base_url");
  expectTypeOf<ProfileFields>().not.toHaveProperty("provider_id");
  expectTypeOf<AgentProfile["model"]>().toHaveProperty("model_id");
});

it("shows the Cursor sign-in command without offering login-link recovery", async () => {
  data.providers = [account({ vendor_type: "cursor-login", auth: "oauth", oauth_runtime_type: "cursor-agent",
    api_key: null, endpoints: [endpoint("openai-responses", "")],
    login: { state: "not_logged_in", detail: "not signed in" } })];
  await render(false);
  expect(container.textContent).toContain("Login not usable: not signed in");
  expect(container.textContent).toContain("cursor-agent login");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.includes("Use the login again"))).toBe(false);
});

it.each(["logged_in", "not_logged_in", "unknown"] as const)("refreshes a cold Cursor login to %s without user action, then stops", async (state) => {
  vi.useFakeTimers();
  data.providers = [account({ vendor_type: "cursor-login", auth: "oauth", oauth_runtime_type: "cursor-agent",
    login: { state: "checking", detail: "checking the Cursor login" } })];
  await render(false);
  expect(container.textContent).toContain("checking the Cursor login");
  expect(container.textContent).not.toContain("cursor-agent login");
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(registryReads).toBe(2);
  data = { ...data, providers: [{ ...data.providers[0], login: { state, detail: `result: ${state}` } }] };
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(container.textContent).toContain(`result: ${state}`);
  expect(container.textContent).not.toContain("checking the Cursor login");
  expect(container.textContent?.includes("cursor-agent login")).toBe(state !== "logged_in");
  const reads = registryReads;
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(registryReads).toBe(reads);
});

it("stops checking the Cursor login when the last registry consumer unmounts", async () => {
  vi.useFakeTimers();
  data.providers = [account({ login: { state: "checking", detail: "checking the Cursor login" } })];
  await render(false);
  await act(async () => { root.render(null); });
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(registryReads).toBe(1);
  expect(vi.getTimerCount()).toBe(0);
});

it("disables with confirmation, preserves selections and displays effects until acknowledged", async () => {
  data.providers = [account()];
  const confirmed = vi.fn(() => true); vi.stubGlobal("confirm", confirmed);
  warnings = ["Agent Profile 'worker' is selected by the defaults of every role: its runs will not start"];
  await render(false); await click("Enable provider Saved account");
  expect(confirmed).toHaveBeenCalledWith('Disable provider "Saved account"? 4 Agent Profile(s) use it. They keep their selections, but their runs will not start until you enable it again.');
  expect(requests).toEqual([{ path: "/api/v1/provider_accounts/set_enabled", body: { provider_id: "account-1", enabled: false } }]);
  expect(container.querySelector('[role="switch"]')!.getAttribute("aria-checked")).toBe("false");
  expect(container.textContent).toContain("Disabled: its Agent Profiles do not run");
  expect([...container.querySelectorAll("span")].some((item) => item.textContent === "Disabled")).toBe(true);
  expect(container.querySelector('[role="status"]')!.textContent).toContain("Disabled, with these effects:");
  expect(container.querySelector('[role="status"]')!.textContent).toContain(warnings[0]);
  await click("OK"); expect(container.querySelector('[role="status"]')).toBeNull();
  await click("Enable provider Saved account");
  expect(confirmed).toHaveBeenCalledTimes(1);
  expect(requests[1].body).toEqual({ provider_id: "account-1", enabled: true });
  expect(container.textContent).not.toContain("Disabled: its Agent Profiles do not run");
});

it("does not disable when the confirmation is cancelled", async () => {
  data.providers = [account()]; vi.stubGlobal("confirm", vi.fn(() => false));
  await render(false); await click("Enable provider Saved account");
  expect(requests).toEqual([]);
  expect(container.querySelector('[role="switch"]')!.getAttribute("aria-checked")).toBe("true");
});

it("toggles a built-in provider without profiles without confirmation", async () => {
  data.providers = [account({ builtin: true, profile_count: 0 })];
  const confirmed = vi.fn(); vi.stubGlobal("confirm", confirmed);
  await render(false); await click("Enable provider Saved account");
  expect(confirmed).not.toHaveBeenCalled();
  expect(requests[0]).toEqual({ path: "/api/v1/provider_accounts/set_enabled", body: { provider_id: "account-1", enabled: false } });
  expect(container.querySelector('[aria-label="Edit provider Saved account"]')).toBeNull();
});

it("keeps the provider enabled and shows a failed transition's error", async () => {
  data.providers = [account({ profile_count: 0 })]; failure = "provider update failed";
  await render(false); await click("Enable provider Saved account");
  expect(container.querySelector('[role="switch"]')!.getAttribute("aria-checked")).toBe("true");
  expect(container.querySelector('[role="alert"]')!.textContent).toBe(failure);
  expect(container.querySelector('[role="status"]')).toBeNull();
});

it("offers on-demand model fetching only for API-key providers", async () => {
  data.providers = [account(), account({ provider_id: "login", name: "Login account", auth: "oauth", api_key: null })];
  await render(false);
  expect(container.querySelectorAll('button[aria-label^="Fetch the models"]')).toHaveLength(1);
  await click("Fetch the models of provider Saved account");
  expect(container.textContent).toContain("could not list models: could not connect to the endpoint");
  expect(container.querySelector('button[aria-label="Fetch the models of provider Saved account"]')?.getAttribute("aria-expanded")).toBe("true");
  await click("Done");
  expect(container.querySelector('button[aria-label="Fetch the models of provider Saved account"]')?.getAttribute("aria-expanded")).toBe("false");
});
