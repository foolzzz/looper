// @vitest-environment happy-dom
import { act, type ComponentType } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "@/App";
import MainApp, { baselinePages } from "virtual:work-main-app";
import { SETTINGS_PAGES } from "@/lib/settings-pages";

vi.mock("@/lib/ids", () => ({ randomId: () => "freeze-session" }));
const identity = vi.hoisted(() => ({ next: 0 }));
vi.mock("react", async (original) => {
  const react = await original<typeof import("react")>();
  return { ...react, useId: () => react.useState(() => `freeze-id-${identity.next++}`)[0] };
});

// Both trees use the real pages, providers, banners, sidebar and shared components.
// Network, clock and generated identities are deterministic. The baseline App/sidebar
// are read from origin/main; this change leaves the existing page modules untouched.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const routes = [
  ...baselinePages.map((page) => `#${page}`),
  "#tasks/task-example", "#tasks?project=project-example", "#projects/project-example",
  "#schedules/schedule-example", "#chat?target=planner-task-example", "#history/planner-task-example?task=task-example",
  "#memory/example", ...SETTINGS_PAGES.map(({ id }) => `#settings/${id}`),
];
type Switch = "off" | "404" | "unknown";
let mode: Switch = "off";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T12:00:00Z"));
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (String(url).includes("project_admin/list")) return Response.json({ status: "ok", result: {
      projects: [{ project_id: "project-example", name: "Example project", work_dir: "", task_count: 12, active_count: 2 }],
      projects_root: "",
    } });
    if (String(url).includes("task_conversations/get")) {
      if (mode === "off") return Response.json({ status: "ok", result: { enabled: false } });
      return Response.json({ status: "error", error: {
        code: mode === "404" ? "not_found" : "unknown_operation",
        message: mode === "404" ? "Not found" : "Unknown operation: task_conversations.get",
      } }, { status: mode === "404" ? 404 : 400 });
    }
    // A deterministic unavailable server exercises each real page's error/empty UI.
    return Response.json({ status: "error", error: { code: "unavailable", message: "Server unavailable" } }, { status: 503 });
  }));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function rendered(Component: ComponentType, hash: string) {
  identity.next = 0;
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, "", hash);
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host, { identifierPrefix: "freeze-" });
  try {
    await act(async () => { root.render(<Component />); });
    for (let tick = 0; tick < 12; tick += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    }
    expect(host.querySelector("aside[aria-label=Navigation]")).not.toBeNull();
    expect(host.querySelector("main")).not.toBeNull();
    return { html: host.innerHTML, hash: window.location.hash };
  } finally {
    await act(async () => { root.unmount(); });
    host.remove();
  }
}

describe.each<Switch>(["off", "404", "unknown"])("Work default-off full-page freeze (%s)", (value) => {
  it.each(routes)("preserves the main sidebar and real page at %s", async (hash) => {
    mode = value;
    const baseline = await rendered(MainApp, hash);
    const current = await rendered(App, hash);
    expect(current).toEqual(baseline);
  });

  it.each(["#work", "#work/manager", "#work/task/task-example"])("redirects unavailable route %s to the main dashboard", async (hash) => {
    mode = value;
    const baseline = await rendered(MainApp, "#dashboard");
    expect(await rendered(App, hash)).toEqual(baseline);
  });
});
