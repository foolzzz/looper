// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LayerSettingsForm } from "@/components/layer-settings";
import { ProjectAgentSettingsSection } from "@/components/project-agent-settings";
import { RolesSection } from "@/components/role-settings";
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

const REGISTERED_PROFILE = {
  profile_id: "p", name: "p", effort: null, runtime_id: "rt", runtime_name: "Runtime", runtime_type: "r",
  model_id: "m", model: { model_id: "m", name: "Exact model", provider_id: "v", protocol: "openai-responses" },
  provider: { provider_id: "v", name: "Account", vendor_type: "custom", auth: "api_key" },
  roles: ["manager", "planner", "worker", "reviewer"],
};
const SUMMARY_REGISTRY = { profiles: [REGISTERED_PROFILE], runtime_types: [], runtimes: [], providers: [], models: [] };
const MODEL_SUMMARY = "Account / Exact model / openai-responses";

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

  it.each(["layer", "project", "role"])("shows the registered account, model and protocol in %s summaries", async (view) => {
    const state = { values: VALUES, effective: EFFECTIVE };
    const roles = { manager: state, planner: state, worker: state, reviewer: state };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const result = url === "/api/v1/registry/view" ? SUMMARY_REGISTRY
        : url === "/api/v1/allowed_profiles/get" ? { profiles: [], choosable: [] }
        : url.endsWith("/roles/list") ? { configs: [{ role: "planner", display_name: "Planner", enabled: true }] }
        : url === "/api/v1/project_agent_settings/get" ? { limits: LIMITS, project: { values: VALUES }, roles }
        : { limits: LIMITS, system: state, roles };
      // Roles and agent settings are v1 operations. callV1 accepts only { status: "ok", result }.
      return Response.json(url.startsWith("/api/v1/") ? { status: "ok", result } : result);
    }));
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(view === "layer"
        ? <LayerSettingsForm idPrefix="summary" layer="role" roles={["planner"]} values={VALUES}
            effective={EFFECTIVE} limits={LIMITS} onSave={async () => VALUES} />
        : view === "project" ? <ProjectAgentSettingsSection projectId="project" linked="" />
        : <RolesSection sub="" />));
      const summary = view === "layer" ? container.querySelector("#summary-profile-now")
        : container.querySelector(view === "project" ? "#project-profiles-project-button" : "#roles-shared-button");
      expect(summary?.textContent).toContain(MODEL_SUMMARY);
      if (view !== "layer") {
        expect(container.querySelector(view === "project" ? "#project-profiles-planner-button" : "#roles-planner-button")?.textContent)
          .toContain(MODEL_SUMMARY);
      }
      if (view === "layer") expect(container.querySelector("#summary-prompt-mode")).toBeTruthy();
    } finally {
      act(() => root.unmount());
      container.remove();
    }
  });
});
