// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectAgentSettingsSection } from "@/components/project-agent-settings";
import { RolesSection } from "@/components/role-settings";
import type { EffectiveSettings, LayerValues, SettingLimits } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const VALUES: LayerValues = {
  harness_prompt: null, profile_id: null, env: [], run_budget_usd: null, max_concurrency: null, boundaries: null,
};
const LIMITS: SettingLimits = {
  harness_max_chars: 20000, env_max_entries: 20, builtin_harness_prompt: "", builtin_boundaries: "",
  builtin_model: "m", builtin_profile: { profile_id: "tools", name: "tools" }, budget_max_usd: 100, concurrency_max: 64,
};
// Every role inherits a profile whose runtime has tools only: it cannot run the processor.
const EFFECTIVE: EffectiveSettings = {
  harness_prompt: { value: "", source: "builtin" },
  profile: { value: { profile_id: "tools", name: "tools", model: "m", effort: null, runtime: "r", provider: "v" },
             source: "system" },
  effort: { value: null, source: "builtin" },
  env: [],
  run_budget_usd: { value: null, source: "builtin" },
  system_prompt_mode: "replace",
};
const PROFILE = {
  profile_id: "tools", name: "tools", effort: null, runtime_id: "rt", runtime_name: "Runtime", runtime_type: "r",
  model_id: "m", model: { model_id: "m", name: "Model", provider_id: "v", protocol: "openai-responses" },
  provider: { provider_id: "v", name: "Account", vendor_type: "custom", auth: "api_key", enabled: true },
  roles: ["manager", "planner", "worker", "reviewer"],
};
const STATE = { values: VALUES, effective: EFFECTIVE };
const ROLES = { manager: STATE, planner: STATE, worker: STATE, reviewer: STATE, processor: STATE };

const posted: { url: string; body: unknown }[] = [];

function serve() {
  posted.length = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.body) posted.push({ url, body: JSON.parse(String(init.body)) });
    const managed = { source: "/managed/settings.json", events: ["UserPromptSubmit"], readable: true };
    if (url.startsWith("/api/v1/processor_settings/")) {
      const refuse = url.endsWith("/update") ? (JSON.parse(String(init?.body)).refuse_with_managed_hooks ?? false) : false;
      return Response.json({ status: "ok", result: { refuse_with_managed_hooks: refuse, managed_hooks: managed } });
    }
    const result = url === "/api/v1/registry/view" ? { profiles: [PROFILE], runtime_types: [], runtimes: [], providers: [], models: [] }
      : url === "/api/v1/allowed_profiles/get" ? { profiles: [], choosable: [] }
      : url.endsWith("/roles/list") ? { configs: [
          { role: "planner", display_name: "Planner", enabled: true, system_prompt: "", default_prompt: "" },
          { role: "processor", display_name: "Processor", enabled: true, system_prompt: "", default_prompt: "" }] }
      : url === "/api/v1/project_agent_settings/get" ? { limits: LIMITS, project: { values: VALUES }, roles: ROLES }
      : { limits: LIMITS, system: STATE, roles: ROLES };
    return Response.json(url.startsWith("/api/v1/") ? { status: "ok", result } : result);
  }));
}

async function rendered(view: React.ReactElement) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(view));
  return { container, done: () => { act(() => root.unmount()); container.remove(); } };
}

describe("the processor's settings", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each(["roles", "project"])("have a %s row with no harness or prompt mode that says when its profile cannot run it", async (view) => {
    serve();
    const { container, done } = await rendered(view === "roles" ? <RolesSection sub="processor" />
      : <ProjectAgentSettingsSection projectId="project" linked="processor" />);
    try {
      const row = view === "roles" ? "roles-processor" : "project-profiles-processor";
      expect(container.querySelector(`#${row}-button`)?.textContent).toContain("cannot run the Processor");
      const panel = container.querySelector(`#${row}-panel`)!;
      expect(panel.querySelector("[id$='-profile']")).toBeTruthy();
      expect(panel.querySelector("[id$='-harness']")).toBeNull();
      expect(panel.querySelector("[id$='-prompt-mode']")).toBeNull();  // it always replaces the runtime's prompt
      if (view === "roles") expect(panel.textContent).toContain("No tools");
    } finally {
      done();
    }
  });

  it("name the machine's managed hooks and can refuse to run while they are present", async () => {
    serve();
    const { container, done } = await rendered(<RolesSection sub="processor" />);
    try {
      const panel = container.querySelector("#roles-processor-panel")!;
      expect(panel.textContent).toContain("UserPromptSubmit");
      const toggle = panel.querySelector<HTMLButtonElement>("[role='switch'][aria-label^='Refuse']")!;
      expect(toggle.getAttribute("aria-checked")).toBe("false");
      await act(async () => toggle.click());
      expect(posted.at(-1)).toEqual({ url: "/api/v1/processor_settings/update", body: { refuse_with_managed_hooks: true } });
      expect(toggle.getAttribute("aria-checked")).toBe("true");
    } finally {
      done();
    }
  });

  it("leave the shared defaults free to choose a profile that has tools only", async () => {
    serve();
    const { container, done } = await rendered(<RolesSection sub="shared" />);
    try {
      const option = container.querySelector<HTMLOptionElement>("#roles-system-profile option[value='tools']")!;
      expect([option.disabled, option.textContent?.includes("cannot run")]).toEqual([false, false]);
    } finally {
      done();
    }
  });
});
