// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

const sources = import.meta.glob<string>("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true });

it("keeps production requests, downloads, stream URLs and frontend fixtures on v1", () => {
  const legacy: string[] = [];
  let versioned = 0;
  for (const [file, source] of Object.entries(sources)) {
    if (file.endsWith("/v1-migration.test.ts")) continue;
    for (const [, , path] of source.matchAll(/(["'`])(\/api(?:\/[^"'`\s]*)?)\1/g)) {
      if (path.startsWith("/api/v1/")) versioned++;
      // The session guard deliberately covers all API versions, including v1.
      else if (path !== "/api/" || !source.includes('startsWith("/api/")')) legacy.push(`${file}: ${path}`);
    }
  }
  expect(versioned).toBeGreaterThan(100);
  expect(legacy).toEqual([]);
  const transport = Object.entries(sources).find(([file]) => file.endsWith("/api.ts"))![1];
  expect(transport).not.toMatch(/\b(?:API_BASE|registryCall|scheduleCall|putJson|catalogRequest|managerJson)\b/);
  expect(Object.entries(sources).find(([file]) => file.endsWith("/session.ts"))![1]).toContain('startsWith("/api/")');
});

it("records only v1 requests across the dashboard domains and preserves nonempty results", async () => {
  const sample = { id: "example" };
  const calls: [string, () => Promise<unknown>, object][] = [
    ["tasks/page", () => api.fetchTasks(), { tasks: [sample], counts: {}, next_before: null }],
    ["project_admin/list", () => api.fetchProjects(), { projects: [sample] }],
    ["project_knowledge/list", () => api.fetchKnowledge("example"), { documents: [sample] }],
    ["project_skills/get", () => api.fetchProjectSkill("example", "skill"), { skill: sample }],
    ["roles/list", () => api.fetchRoleConfigs(), { configs: [sample] }],
    ["managers/list", () => api.fetchManagers(), { managers: [sample], max_enabled: 8 }],
    ["role_templates/list", () => api.fetchRoleTemplates("example"), { templates: [sample] }],
    ["channel_configs/list", () => api.fetchChannelConfigs(), { configs: [sample] }],
    ["catalog_items/list", () => api.fetchCatalog(), { items: [sample] }],
    ["agent_settings/get", () => api.fetchAgentSettings(), { system: sample }],
    ["runs/for_task", () => api.fetchTaskRuns("example"), { runs: [sample] }],
    ["tasks/activity", () => api.fetchTaskActivity("example"), { activity: [sample] }],
    ["operator_chat/targets", () => api.fetchChatTargets(), { targets: [sample] }],
    ["schedule_admin/list", () => api.fetchAllSchedules(), { schedules: [sample] }],
    ["registry/view", () => api.fetchRegistry(), { providers: [sample], models: [sample], profiles: [sample] }],
  ];
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  for (const [path, run, result] of calls) {
    fetcher.mockResolvedValueOnce(Response.json({ status: "ok", result }));
    expect(JSON.stringify(await run()), path).toContain("example");
  }
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual(calls.map(([path]) => `/api/v1/${path}`));
  expect(fetcher.mock.calls.every(([, init]) => init.method === "POST" && init.credentials === "same-origin")).toBe(true);
});
