// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProvidersSection } from "@/components/provider-settings";
import type { ProviderPreset, Registry } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("@/lib/use-registry", () => ({ useRegistry: () => ({ registry: REGISTRY, error: null }) }));

const model = (name: string, context_window: number | null = null, background_model: string | null = null) =>
  ({ name, context_window, background_model });
const gpt = [model("gpt-6.1-sol", 1_050_000), model("gpt-5.6-sol", 1_050_000)];
const chat = [model("deepseek-v4-pro"), model("glm-5.3"), model("kimi-k3")];
const claude = [model("claude-opus-5-5"), model("claude-sonnet-5-5")];
const glm = [model("glm-5.3[1m]", 1_000_000, "glm-5.3-flash[1m]")];
const PRESETS: ProviderPreset[] = [
  {
    preset_id: "opencode", label: "OpenCode (Zen)", name: "opencode", models: [...gpt, ...claude, ...chat],
    apis: [
      { protocols: ["openai-responses"], base_url: "https://opencode.ai/inference/openai/v1", models: gpt },
      { protocols: ["openai-chat"], base_url: "https://opencode.ai/inference/openai/v1", models: chat },
      { protocols: ["anthropic-messages"], base_url: "https://opencode.ai/inference/anthropic", models: claude },
    ], env: [], docs: [], note: "",
  },
  {
    preset_id: "glm", label: "GLM", name: "glm", models: glm,
    apis: [
      { protocols: ["anthropic-messages"], base_url: "https://example.invalid/anthropic", models: glm },
      { protocols: ["openai-chat"], base_url: "https://example.invalid/v1",
        models: [model("glm-5.3", 1_000_000, "glm-5.3-flash")] },
      { protocols: ["openai-responses"], base_url: "https://example.invalid/v1", models: [] },
      { protocols: ["anthropic-messages"], base_url: "https://example.invalid/legacy" },
    ], env: [{ key: "API_TIMEOUT_MS", value: "3000000" }], docs: [], note: "",
  },
];
const REGISTRY: Registry = {
  runtime_types: [], runtimes: [], providers: [], profiles: [], provider_presets: PRESETS,
  prices: { as_of: "", sources: [], models: {} },
  limits: {
    name_max_chars: 100, models_max: 50, price_fields: [],
    protocols: ["anthropic-messages", "openai-chat", "openai-responses"], auth_kinds: ["api_key"], effort_levels: [],
    role_capabilities: { manager: [], planner: [], worker: [], reviewer: [] }, secret_mask: "", provider_env_owned: [],
  },
};

let container: HTMLDivElement;
let root: Root;
let bodies: Record<string, unknown>[];
beforeEach(() => {
  bodies = [];
  vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.body) bodies.push(JSON.parse(String(init.body)));
    return new Response(JSON.stringify({ provider: {}, warnings: [] }), { status: 200 });
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

it.each([
  ["opencode:0", gpt], ["opencode:1", chat], ["opencode:2", claude],
  ["glm:0", glm], ["glm:1", [model("glm-5.3", 1_000_000, "glm-5.3-flash")]],
  ["glm:2", []], ["glm:3", glm],
] as const)("fills and saves the selected API's catalog for %s", async (choice, expected) => {
  await act(async () => root.render(<ProvidersSection adding />));
  const label = [...container.querySelectorAll("label")].find((item) => item.textContent === "Start from a preset")!;
  await act(async () => {
    const select = document.getElementById(label.htmlFor) as HTMLSelectElement;
    select.value = choice;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const names = [...container.querySelectorAll<HTMLInputElement>('input[id*="-m"][id$="-name"]')].map((i) => i.value);
  expect(names).toEqual(expected.map((m) => m.name));
  await act(async () => container.querySelector("form")!.dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true }),
  ));
  expect(bodies).toHaveLength(1);
  expect(bodies[0].models).toEqual(expected.map((m) => ({ ...m, efforts: null })));
  const [id, index] = choice.split(":");
  const preset = PRESETS.find((p) => p.preset_id === id)!;
  const api = preset.apis[Number(index)];
  expect(bodies[0].protocols).toEqual(api.protocols);
  expect(bodies[0].base_url).toBe(api.base_url);
  expect(bodies[0].env).toEqual(api.protocols.includes("anthropic-messages") ? preset.env : []);
});
