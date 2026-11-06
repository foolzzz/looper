// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { GatewaySettingsSection } from "./gateway-settings";
import type { GatewaySettings } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());
const alias = { alias: "local-chat", model_id: "chat", model_name: "Chat model", provider_id: "key", provider_name: "Example", enabled: true };
function settings(): GatewaySettings {
  return { enabled: false, port: 32991, url: "http://127.0.0.1:32991/gw", key_exists: true, aliases: [alias],
    proxy: { state: "running", port: 32991, pid: null, requests: 12, recorded: 12, gaps: 0, gap_reasons: {}, dropped: 0, write_errors: 0 },
    translate: { requests: 12, input_tokens: 20, cache_read_input_tokens: 80, output_tokens: 10, usage_missing: 1 },
    auth_failures: 2, alias_usage: { "local-chat": { requests: 12, input_tokens: 20, cache_read_input_tokens: 80, output_tokens: 10 } } };
}
function fixture() {
  let saved = settings();
  let fail = false;
  const writes: [string, Record<string, unknown>][] = [];
  const providers = [
    { provider_id: "key", auth: "api_key", api_key: "***", enabled: true },
    { provider_id: "oauth", auth: "oauth", api_key: null, enabled: true },
    { provider_id: "missing", auth: "api_key", api_key: null, enabled: true },
    { provider_id: "disabled", auth: "api_key", api_key: "***", enabled: false },
  ];
  const models = [
    { model_id: "chat", provider_id: "key", provider_name: "Example", name: "Chat model", protocol: "openai-chat" },
    { model_id: "responses", provider_id: "key", name: "Responses model", protocol: "openai-responses" },
    ...["oauth", "missing", "disabled"].map((provider_id) => ({ model_id: provider_id, provider_id, name: provider_id, protocol: "openai-chat" })),
  ];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    if (!path.includes("/gateway/")) return Response.json({ status: "ok", result: { providers, models } });
    const operation = path.split("/").at(-1)!;
    const fields = JSON.parse(String(init.body));
    if (operation !== "get") writes.push([operation, fields]);
    if (fail) return Response.json({ status: "error", error: { code: "unavailable", message: "Configuration unavailable" } }, { status: 503 });
    if (operation === "update") saved = { ...saved, ...fields };
    if (operation === "alias_set") saved = { ...saved, aliases: [...saved.aliases.filter((entry) => entry.alias !== fields.alias), { ...alias, ...fields }] };
    if (operation === "alias_delete") saved = { ...saved, aliases: saved.aliases.filter((entry) => entry.alias !== fields.alias) };
    return Response.json({ status: "ok", result: ["key_reveal", "key_rotate"].includes(operation) ? { key: operation === "key_rotate" ? "rotated-test-key" : "example-test-key" } : saved });
  }));
  return { writes, fail: () => { fail = true; } };
}
async function mount() {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<GatewaySettingsSection />));
  return { container, button: (label: string) => Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find((entry) => entry.textContent === label)!,
    close: () => { act(() => root.unmount()); container.remove(); } };
}
async function enter(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("saves endpoint enablement and fixed port, preserving state after a failure", async () => {
  const api = fixture(); const page = await mount();
  try {
    const toggle = page.container.querySelector<HTMLButtonElement>('[role="switch"]')!;
    await act(async () => toggle.click());
    expect(api.writes).toEqual([["update", { enabled: true, port: 32991 }]]);
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    const port = page.container.querySelector<HTMLInputElement>('input[type="number"]')!;
    await enter(port, "65536"); expect(page.button("Save port").disabled).toBe(true);
    await enter(port, "32992"); await act(async () => page.button("Save port").click());
    expect(api.writes.at(-1)).toEqual(["update", { enabled: true, port: 32992 }]);
    api.fail(); await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    expect(page.container.querySelector('[role="alert"]')?.textContent).toContain("Configuration unavailable");
  } finally { page.close(); }
});

it("limits alias targets to enabled API key Chat models and adds and deletes aliases", async () => {
  const api = fixture(); const page = await mount();
  try {
    const model = page.container.querySelector<HTMLSelectElement>('select[id$="-model"]')!;
    expect(Array.from(model.options).map((entry) => entry.value)).toEqual(["", "chat"]);
    const name = page.container.querySelector<HTMLInputElement>('input[id$="-alias"]')!;
    await enter(name, "bad alias"); expect(page.button("Add alias").disabled).toBe(true);
    await enter(name, "local-fast");
    await act(async () => { model.value = "chat"; model.dispatchEvent(new Event("change", { bubbles: true })); });
    await act(async () => page.button("Add alias").click());
    expect(api.writes.at(-1)).toEqual(["alias_set", { alias: "local-fast", model_id: "chat" }]);
    const remove = page.container.querySelector<HTMLButtonElement>('[aria-label="Delete alias local-fast"]')!;
    await act(async () => remove.click());
    expect(api.writes.at(-1)).toEqual(["alias_delete", { alias: "local-fast" }]);
    expect(page.container.querySelector('[aria-label="Delete alias local-fast"]')).toBeNull();
  } finally { page.close(); }
});

it("fetches keys only on demand, copies them, and confirms rotation with cancel focused", async () => {
  const api = fixture(); const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  const page = await mount();
  try {
    expect(api.writes).toEqual([]);
    await act(async () => page.button("Show key").click());
    expect(page.container.querySelector<HTMLInputElement>('[aria-label="Revealed local key"]')?.value).toBe("example-test-key");
    await act(async () => page.button("Copy key").click());
    expect(writeText).toHaveBeenCalledWith("example-test-key");
    await act(async () => page.button("Rotate key").click());
    expect(document.activeElement).toBe(page.button("Cancel"));
    expect(api.writes.some(([operation]) => operation === "key_rotate")).toBe(false);
    await act(async () => page.button("Cancel").click());
    expect(document.activeElement).toBe(page.button("Rotate key"));
    await act(async () => page.button("Rotate key").click());
    await act(async () => page.button("Rotate and invalidate old key").click());
    expect(api.writes.filter(([operation]) => operation === "key_rotate")).toHaveLength(1);
    expect(page.container.querySelector('[aria-label="Revealed local key"]')).toBeNull();
  } finally { page.close(); }
});

it("shows all model tiers pinned to the alias, token totals, cache hit and authentication failures", async () => {
  fixture(); const page = await mount();
  try {
    const env = page.container.querySelector("pre")!.textContent!;
    for (const variable of ["ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"]) {
      expect(env).toContain(`export ${variable}='local-chat'`);
    }
    expect(env).toContain("ANTHROPIC_AUTH_TOKEN='<local-key>'");
    expect(env).toContain("ANTHROPIC_API_KEY=''");
    expect(page.container.textContent).not.toContain("example-test-key");
    expect(page.container.textContent).toContain("12 requests · Cache hit 80%");
    const facts = Object.fromEntries(Array.from(page.container.querySelectorAll("dl > div")).map((entry) => [entry.querySelector("dt")!.textContent, entry.querySelector("dd")!.textContent]));
    expect(facts).toMatchObject({ Requests: "12", "Authentication failures": "2", "Cache hit": "80%", "Missing usage": "1" });
  } finally { page.close(); }
});
