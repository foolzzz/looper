import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LayerSettingsForm } from "@/components/layer-settings";
import type { AgentRole, EffectiveSettings, LayerValues, SettingLayer, SettingLimits } from "@/lib/api";

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

const effective = (limit?: number): EffectiveSettings => ({
  harness_prompt: { value: "", source: "builtin" },
  profile: { value: { profile_id: "p", name: "p", model: "m", effort: null, runtime: "r", provider: "v" }, source: "builtin" },
  effort: { value: null, source: "builtin" },
  env: [],
  run_budget_usd: { value: null, source: "builtin" },
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
