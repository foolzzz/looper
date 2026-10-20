// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProvidersSection } from "@/components/provider-settings";
import type { Provider, ProviderFields, ProviderPreset, Registry } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ANTHROPIC = "anthropic-messages";
const CHAT = "openai-chat";
const RESPONSES = "openai-responses";
const KIMI: ProviderPreset = {
  preset_id: "kimi", label: "Kimi", name: "kimi",
  apis: [
    { protocols: [ANTHROPIC], base_url: "https://api.moonshot.ai/anthropic" },
    { protocols: [RESPONSES, CHAT], base_url: "https://api.moonshot.ai/v1" },
  ],
  models: [{ name: "kimi-k3[1m]", context_window: 1048576, background_model: "kimi-k2.7-code" }],
  env: [{ key: "CLAUDE_CODE_AUTO_COMPACT_WINDOW", value: "1000000" }], docs: [], note: "",
};
const PRESETS: ProviderPreset[] = [KIMI, {
  ...KIMI, preset_id: "deepseek", label: "DeepSeek", name: "deepseek",
  apis: [
    { protocols: [ANTHROPIC], base_url: "https://api.deepseek.com/anthropic" },
    { protocols: [CHAT], base_url: "https://api.deepseek.com" },
  ],
}, {
  ...KIMI, preset_id: "cpa", label: "CLIProxyAPI on this machine", name: "cpa", env: [],
  apis: [
    { protocols: [RESPONSES, CHAT], base_url: "http://127.0.0.1:8317/v1" },
    { protocols: [ANTHROPIC], base_url: "http://127.0.0.1:8317" },
  ],
}];
const EXISTING: Provider = {
  provider_id: "existing", name: "custom", protocols: [CHAT], auth: "api_key", oauth_runtime_type: null,
  base_url: "https://custom.example/v1", models: [], suggested_models: [], login: null,
  env: [{ key: "CUSTOM", value: "keep" }], max_concurrency: 2, api_key: "***", builtin: false, profile_count: 0,
};
const REGISTRY: Registry = {
  runtime_types: [], runtimes: [], providers: [], profiles: [], provider_presets: PRESETS,
  prices: { as_of: "", sources: [], models: {} },
  limits: {
    name_max_chars: 100, models_max: 20, price_fields: [], protocols: [ANTHROPIC, RESPONSES, CHAT],
    auth_kinds: ["api_key", "oauth"], effort_levels: [],
    role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] }, secret_mask: "***", provider_env_owned: [],
  },
};

describe("Provider vendor form", () => {
  let container: HTMLDivElement;
  let root: Root;
  let registry: Registry;
  let requests: { method: string; body: ProviderFields }[];
  let failName: string;

  beforeEach(() => {
    registry = REGISTRY;
    requests = [];
    failName = "";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/registry")) return Response.json(registry);
      const body = JSON.parse(String(init?.body)) as ProviderFields;
      requests.push({ method: init!.method!, body });
      return body.name === failName
        ? Response.json({ error: "Address unavailable" }, { status: 400 })
        : Response.json({ provider: { ...EXISTING, ...body } });
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const show = async () => { await act(async () => root.render(<ProvidersSection adding />)); };
  const field = (label: string) => {
    const item = [...container.querySelectorAll("label")].find((l) => l.textContent === label);
    expect(item, label).toBeTruthy();
    return document.getElementById(item!.htmlFor) as HTMLInputElement;
  };
  const choose = async (value: string) => {
    await act(async () => {
      const select = field("Start from a preset");
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };
  const fill = async (label: string, value: string) => {
    await act(async () => {
      const input = field(label);
      const prototype = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const checkbox = (protocol: string) => {
    const label = [...container.querySelectorAll("label")].find((l) => l.textContent?.trim() === protocol);
    expect(label, protocol).toBeTruthy();
    return label!.querySelector("input")!;
  };
  const toggle = async (protocol: string) => { await act(async () => checkbox(protocol).click()); };
  const save = async () => {
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  };

  it("lists each vendor once, plus the manual option", async () => {
    await show();
    expect([...field("Start from a preset").querySelectorAll("option")].map((o) => o.textContent))
      .toEqual(["None: fill in every field", ...PRESETS.map((p) => p.label)]);
  });

  it.each(PRESETS)("preselects $label's first API and labels its protocol groups by address", async (preset) => {
    await show();
    await choose(preset.preset_id);
    expect(field("Name *").value).toBe(preset.name);
    expect(field("Base URL").value).toBe(preset.apis[0].base_url);
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(preset.apis.flatMap((api) => api.protocols).length);
    for (const protocol of preset.apis.flatMap((api) => api.protocols)) {
      expect(checkbox(protocol).checked).toBe(preset.apis[0].protocols.includes(protocol));
    }
    for (const api of preset.apis) {
      const group = checkbox(api.protocols[0]).closest("fieldset")!;
      expect(group.querySelector("legend")!.textContent).toContain(api.base_url);
      if (api.protocols.includes(ANTHROPIC)) expect(group.textContent).toContain("for Claude Code");
    }
  });

  it("keeps the editable URL when protocols at the same address are toggled", async () => {
    await show();
    await choose("cpa");
    await toggle(CHAT);
    await fill("Base URL", "https://proxy.example/v1");
    await toggle(CHAT);
    expect(field("Base URL").value).toBe("https://proxy.example/v1");
    await save();
    expect(requests[0].body).toMatchObject({ protocols: [RESPONSES, CHAT], base_url: "https://proxy.example/v1" });
  });

  it("splits selected addresses into providers, sharing the key and models and scoping Claude env", async () => {
    await show();
    await choose("kimi");
    await toggle(CHAT);
    expect(container.textContent).toContain("served at two addresses: Looper adds one provider per address");
    expect(field("Base URL").disabled).toBe(true);
    await toggle(RESPONSES);
    await fill("API key", "test-provider-key");
    await fill("Extra environment", "CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000\nCUSTOM=shared");
    await save();
    expect(requests.map((r) => r.body)).toEqual([
      expect.objectContaining({ name: "kimi (Anthropic)", protocols: [ANTHROPIC], base_url: KIMI.apis[0].base_url,
        api_key: "test-provider-key", models: [expect.objectContaining(KIMI.models[0])],
        env: [...KIMI.env, { key: "CUSTOM", value: "shared" }] }),
      expect.objectContaining({ name: "kimi (OpenAI)", protocols: [RESPONSES, CHAT], base_url: KIMI.apis[1].base_url,
        api_key: "test-provider-key", models: [expect.objectContaining(KIMI.models[0])], env: [{ key: "CUSTOM", value: "shared" }] }),
    ]);
    expect(container.querySelector('[role="status"]')!.textContent).toContain("kimi (Anthropic): Created");
    expect(container.querySelector('[role="status"]')!.textContent).toContain("kimi (OpenAI): Created");
  });

  it.each(["kimi (Anthropic)", "kimi (OpenAI)"])("reports both outcomes when %s fails", async (name) => {
    failName = name;
    await show();
    await choose("kimi");
    await toggle(CHAT);
    await save();
    expect(requests).toHaveLength(2);
    expect(container.textContent).toContain(`${name}: Failed`);
    expect(container.textContent).toContain("Address unavailable");
    expect(container.textContent).toContain(`${name === "kimi (Anthropic)" ? "kimi (OpenAI)" : "kimi (Anthropic)"}: Created`);
  });

  it("follows the remaining address and removes Claude env when Anthropic is unchecked", async () => {
    await show();
    await choose("kimi");
    await toggle(CHAT);
    await toggle(ANTHROPIC);
    expect(field("Base URL").value).toBe(KIMI.apis[1].base_url);
    expect(field("Extra environment").value).toBe("");
    await save();
    expect(requests[0].body).toMatchObject({ name: "kimi", protocols: [CHAT], base_url: KIMI.apis[1].base_url, env: [] });
  });

  it("retries only failed addresses without creating the successful provider again", async () => {
    failName = "kimi (OpenAI)";
    await show();
    await choose("kimi");
    await toggle(CHAT);
    await save();
    failName = "";
    const retry = [...container.querySelectorAll("button")].find((button) => button.textContent === "Retry failed")!;
    await act(async () => retry.click());
    expect(requests.map((r) => r.body.name)).toEqual(["kimi (Anthropic)", "kimi (OpenAI)", "kimi (OpenAI)"]);
    expect(container.textContent).not.toContain("Failed");
    expect(container.textContent).toContain("kimi (Anthropic): Created");
    expect(container.textContent).toContain("kimi (OpenAI): Created");
  });

  it("requires a protocol and restores the address and Claude env when it is rechecked", async () => {
    await show();
    await choose("kimi");
    await toggle(ANTHROPIC);
    expect(field("Base URL").value).toBe("");
    await save();
    expect(requests).toHaveLength(0);
    expect(container.textContent).toContain("Choose at least one protocol");
    await toggle(ANTHROPIC);
    expect(field("Base URL").value).toBe(KIMI.apis[0].base_url);
    expect(field("Extra environment").value).toBe("CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000");
  });

  it("creates one provider per address when a vendor has three APIs", async () => {
    registry = { ...REGISTRY, provider_presets: [{ ...KIMI, apis: [KIMI.apis[0],
      { protocols: [CHAT], base_url: "https://vendor.example/chat" },
      { protocols: [RESPONSES], base_url: "https://vendor.example/responses" },
    ] }] };
    await show();
    await choose("kimi");
    await toggle(CHAT);
    await toggle(RESPONSES);
    expect(container.textContent).toContain("served at 3 addresses");
    await save();
    expect(requests.map((r) => [r.body.name, r.body.base_url, r.body.protocols])).toEqual([
      ["kimi (Anthropic)", KIMI.apis[0].base_url, [ANTHROPIC]],
      ["kimi (OpenAI Chat)", "https://vendor.example/chat", [CHAT]],
      ["kimi (OpenAI Responses)", "https://vendor.example/responses", [RESPONSES]],
    ]);
  });

  it("combines protocols from APIs sharing one address into a single provider", async () => {
    registry = { ...REGISTRY, provider_presets: [{ ...KIMI, apis: KIMI.apis.map((api) => ({ ...api, base_url: "https://vendor.example" })) }] };
    await show();
    await choose("kimi");
    await toggle(CHAT);
    expect(container.textContent).not.toContain("Looper adds one provider per address");
    await save();
    expect(requests).toHaveLength(1);
    expect(requests[0].body).toMatchObject({ name: "kimi", base_url: "https://vendor.example", protocols: [ANTHROPIC, CHAT] });
  });

  it("clears preset fields for None and saves manually entered fields", async () => {
    await show();
    await choose("kimi");
    await choose("");
    expect(field("Name *").value).toBe("");
    expect(field("Base URL").value).toBe("");
    expect(field("Extra environment").value).toBe("");
    expect(container.querySelector('button[aria-label^="Remove model"]')).toBeNull();
    await fill("Name *", "manual");
    await fill("Base URL", "https://manual.example");
    await toggle(CHAT);
    await save();
    expect(requests[0].body).toMatchObject({ name: "manual", protocols: [ANTHROPIC, CHAT], base_url: "https://manual.example", models: [], env: [] });
  });

  it("keeps existing providers on the original single-provider edit path", async () => {
    registry = { ...REGISTRY, providers: [EXISTING] };
    await act(async () => root.render(<ProvidersSection />));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Edit provider custom"]')!.click());
    expect(container.querySelector('label[for$="-preset"]')).toBeNull();
    expect(field("Base URL").value).toBe(EXISTING.base_url);
    await toggle(ANTHROPIC);
    await save();
    expect(requests).toEqual([{ method: "PUT", body: expect.objectContaining({
      name: "custom", base_url: EXISTING.base_url, protocols: [ANTHROPIC, CHAT], api_key: "***", env: EXISTING.env,
    }) }]);
  });
});
