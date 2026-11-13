// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { ProjectRoleTemplates, TaskRoleEditor } from "@/components/role-editor";
import type { AgentProfile, AllowedProfile, PlannerRole } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it.each([true, false])("preserves profile names in both role pickers when provider enabled is %s", async (enabled) => {
  const profile: AgentProfile = {
  model_path: "direct",
    profile_id: "disabled", name: "Saved profile", model_id: "model", effort: "high", agent_teams: false, web_search: true,
    model: { model_id: "model", name: "Exact model", protocol: "anthropic-messages", provider_id: "account" },
    custom_prompt: "", runtime_id: "runtime", runtime_type: "claude-code", runtime_name: "Claude Code", builtin: false,
    provider: { provider_id: "account", name: "Saved account", vendor_type: "custom", auth: "api_key", enabled },
    roles: ["worker", "reviewer"], used_by: { layers: [], task_count: 0, schedule_count: 0, allowed_in: [] }, warnings: [],
    cost: { source: "runtime", equivalent: false, unpriced: [] },
  };
  const allowed: AllowedProfile = {
    profile_id: profile.profile_id, name: "Allowed profile", runtime: profile.runtime_type, model: profile.model.name,
    effort: profile.effort, roles: profile.roles as AllowedProfile["roles"], usd_per_million_tokens: { input: null, output: null },
  };
  const role: PlannerRole = {
    id: "tester", kind: "work", version: 1, profile: { profile_id: profile.profile_id, name: profile.name, allowed: true },
    role_prompt: "Test the change.", harness: null, items: [],
    reason: "Verify the change.", created_at: "2026-11-05T00:00:00Z", via: "planner", template: null,
    versions: [], steps: [],
    stats: {
      runs: 0, running: 0, cost_usd: 0, contract_violations: 0, contract_violation_rate: null,
      steps: 0, accepted: 0, failed: 0, retries: 0, reviews: { passed: 0, not_passed: 0 }, review_pass_rate: null,
    },
  };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (input === "/api/v1/registry/view") {
      return Response.json({ status: "ok", result: { profiles: [profile], providers: [profile.provider] } });
    }
    if (input === "/api/v1/allowed_profiles/get") {
      return Response.json({ status: "ok", result: { profiles: [allowed], choosable: [allowed], restricted: true } });
    }
    if (input === "/api/v1/role_templates/list") {
      return Response.json({ status: "ok", result: { templates: [], role_items: [], role_text_max_chars: 20000 } });
    }
    throw new Error(`Unexpected request: ${input}`);
  }));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<>
      <TaskRoleEditor taskId="task" projectId="project" role={role} onSaved={() => {}} />
      <ProjectRoleTemplates projectId="project" rolesOn />
    </>));
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[aria-label="Edit role tester"]')!.click();
      [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("New template"))!.click();
    });
    const pickers = [...container.querySelectorAll<HTMLSelectElement>('select[id$="-profile"]')];
    expect(pickers).toHaveLength(2);
    expect(pickers.map((picker) => picker.querySelector('option[value="disabled"]')?.textContent)).toEqual([
      enabled ? allowed.name : `${allowed.name} · provider disabled`,
      enabled ? allowed.name : `${allowed.name} · provider disabled`,
    ]);
    expect(pickers[0].value).toBe("disabled");
  } finally {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
