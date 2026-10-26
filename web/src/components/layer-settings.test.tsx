// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskAgentsSection } from "@/components/task-agents";
import { LayerSettingsForm } from "@/components/layer-settings";
import type { AgentProfile, TaskAgents, AgentRole, EffectiveSettings, LayerValues, SettingLayer, SettingLimits, SystemPromptMode } from "@/lib/api";

const VALUES: LayerValues = {
  harness_prompt: null,
  profile_id: null,
  env: [],
  run_budget_usd: null,
  max_concurrency: null,
  boundaries: null,
};

const LIMITS: SettingLimits = {
  harness_max_chars: 20000,
  env_max_entries: 20,
  builtin_harness_prompt: "",
  builtin_boundaries: "",
  builtin_model: "m",
  builtin_profile: { profile_id: "p", name: "p" },
  budget_max_usd: 100,
  concurrency_max: 64,
};

const effective = (limit?: number, mode: SystemPromptMode = "append"): EffectiveSettings => ({
  harness_prompt: { value: "", source: "builtin" },
  profile: { value: { profile_id: "p", name: "p", model: "m", effort: null, runtime: "r", provider: "v" }, source: "builtin" },
  effort: { value: null, source: "builtin" },
  env: [],
  run_budget_usd: { value: null, source: "builtin" },
  system_prompt_mode: mode,
  ...(limit == null ? {} : { max_concurrency: { value: limit, source: "builtin" as const } }),
});

function render(layer: Exclude<SettingLayer, "builtin">, roles: AgentRole[], limit?: number) {
  return renderToStaticMarkup(
    <LayerSettingsForm
      idPrefix="t"
      layer={layer}
      roles={roles}
      values={VALUES}
      effective={effective(limit)}
      limits={LIMITS}
      onSave={async () => VALUES}
    />,
  );
}

const text = (html: string) => html.replace(/<[^>]+>/g, "");

describe("LayerSettingsForm's per-task limit", () => {
  it.each(["worker", "reviewer"] as const)("is the %s's role layer's default, with what applies now", (role) => {
    const html = render("role", [role], 16);
    expect(html).toContain('id="t-concurrency"');
    expect(text(html)).toContain("Now: 16 · from built-in default");
  });

  it.each([
    ["the Planner's role layer", "role", ["planner"]],
    ["the Manager's role layer", "role", ["manager"]],
    ["the shared defaults", "system", ["manager", "planner", "worker", "reviewer"]],
    ["a project's layer", "project", ["planner", "worker", "reviewer"]],
    ["a Worker's layer in a project", "role_project", ["worker"]],
    ["a Reviewer's layer in a project", "role_project", ["reviewer"]],
  ] as const)("is not on %s", (_, layer, roles) => {
    expect(render(layer, [...roles])).not.toContain("t-concurrency");
  });
});

describe("LayerSettingsForm's system prompt mode", () => {
  it.each(["manager", "planner", "worker", "reviewer"] as const)("is on the %s's role layer, for every runtime", (role) => {
    const html = renderToStaticMarkup(
      <LayerSettingsForm idPrefix="t" layer="role" roles={[role]} values={VALUES} effective={effective(undefined, "replace")}
        limits={LIMITS} onSave={async () => VALUES} />,
    );
    expect(html).toContain('id="t-prompt-mode"');
    expect(html).toMatch(/<option value="replace"[^>]*>Replace the runtime&#x27;s prompt/);
    expect(text(html)).not.toContain("Claude Code");
    expect(html).not.toMatch(/<option value="auto"|<option value="default"/);
  });

  it.each([
    ["the shared defaults", "system", ["manager", "planner", "worker", "reviewer"]],
    ["a project's layer", "project", ["planner", "worker", "reviewer"]],
    ["a role's layer in a project", "role_project", ["worker"]],
  ] as const)("is not on %s", (_, layer, roles) => {
    expect(render(layer, [...roles])).not.toContain("t-prompt-mode");
  });
});


(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const API_PROFILE: AgentProfile = {
  profile_id: "api-manager", name: "API Manager", model_id: "responses-model",
  model: { model_id: "responses-model", name: "model", protocol: "openai-responses", provider_id: "provider" },
  effort: null, agent_teams: false, custom_prompt: "", runtime_id: "rt-api", runtime_type: "api", runtime_name: "Model API",
  builtin: false, provider: { provider_id: "provider", name: "Provider", vendor_type: "custom", auth: "api_key" },
  roles: ["manager"], used_by: { layers: [], task_count: 0, schedule_count: 0, allowed_in: [] }, warnings: [],
  cost: { source: "provider_price", equivalent: false, unpriced: [] },
};

describe("Manager-only API profiles", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    const cliProfile = { ...API_PROFILE, profile_id: "cli", roles: ["manager", "planner", "worker", "reviewer"] };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { profiles: [API_PROFILE, cliProfile] } })));
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
    ["role", ["manager"], true],
    ["role", ["planner"], false],
    ["role", ["worker"], false],
    ["role", ["reviewer"], false],
    ["system", ["manager", "planner", "worker", "reviewer"], false],
    ["project", ["planner", "worker", "reviewer"], false],
    ["role_project", ["planner"], false],
    ["role_project", ["worker"], false],
    ["role_project", ["reviewer"], false],
  ] as const)("is selectable in %s for %j only when all roles are supported", async (layer, roles, allowed) => {
    await act(async () => root.render(
      <LayerSettingsForm idPrefix="api" layer={layer} roles={[...roles]} values={VALUES} limits={LIMITS}
        onSave={async () => VALUES} />,
    ));
    const option = container.querySelector<HTMLOptionElement>('option[value="api-manager"]');
    expect(option).not.toBeNull();
    expect(option!.disabled).toBe(!allowed);
    expect(container.querySelector<HTMLOptionElement>('option[value="cli"]')!.disabled).toBe(false);
  });

  it("disables the API profile in every task role selector", async () => {
    const role: TaskAgents["roles"]["worker"] = {
      profile: effective().profile, runtime_type: "claude-code",
      system_prompt_mode: { value: "append", source: "builtin" },
      concurrency: { value: 1, source: "builtin", task: null, default: { value: 1, source: "builtin" } },
    };
    const agents: TaskAgents = { concurrency_max: 64, editable: true, roles: { planner: role, worker: role, reviewer: role } };
    await act(async () => root.render(<TaskAgentsSection taskId="task" agents={agents} onSaved={() => {}} />));
    const options = container.querySelectorAll<HTMLOptionElement>('option[value="api-manager"]');
    expect(options).toHaveLength(3);
    for (const option of options) expect(option.disabled).toBe(true);
    for (const option of container.querySelectorAll<HTMLOptionElement>('option[value="cli"]')) expect(option.disabled).toBe(false);
  });
});
