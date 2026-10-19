// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LayerSettingsForm } from "@/components/layer-settings";
import type { EffectiveSettings, LayerUpdate, LayerValues, SettingLimits } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const VALUES: LayerValues = {
  harness_prompt: null, profile_id: null, env: [], run_budget_usd: null, max_concurrency: null, boundaries: null,
};
const LIMITS: SettingLimits = {
  harness_max_chars: 20000, env_max_entries: 20, builtin_harness_prompt: "", builtin_boundaries: "",
  builtin_model: "m", builtin_profile: { profile_id: "p", name: "p" }, budget_max_usd: 100, concurrency_max: 64,
};
const EFFECTIVE: EffectiveSettings = {
  harness_prompt: { value: "", source: "builtin" },
  profile: { value: { profile_id: "p", name: "p", model: "m", effort: null, runtime: "r", provider: "v" }, source: "builtin" },
  effort: { value: null, source: "builtin" },
  env: [],
  run_budget_usd: { value: null, source: "builtin" },
  system_prompt_mode: "replace",
};

describe("the role layer's system prompt mode", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the effective mode, saves nothing until changed, then saves the choice", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    const saved: LayerUpdate[] = [];
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(
      <LayerSettingsForm idPrefix="t" layer="role" roles={["planner"]} values={VALUES} effective={EFFECTIVE}
        limits={LIMITS} onSave={async (changes) => { saved.push(changes); return { ...VALUES, ...changes } as LayerValues; }} />,
    ));
    const field = container.querySelector<HTMLSelectElement>("#t-prompt-mode")!;
    expect(field.value).toBe("replace");
    const submit = () => act(async () => container.querySelector("form")!.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true })));
    await submit();
    expect(saved).toEqual([]);
    await act(async () => {
      field.value = "append";
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await submit();
    expect(saved).toEqual([{ system_prompt_mode: "append" }]);
    act(() => root.unmount());
    container.remove();
  });
});
