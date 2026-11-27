// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkTaskSandbox } from "@/components/work-task-sandbox";
import { V1Error } from "@/lib/v1";

const v1 = vi.hoisted(() => ({ callV1: vi.fn() }));
vi.mock("@/lib/v1", async (original) => ({ ...(await original<typeof import("@/lib/v1")>()), ...v1 }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
const show = async (projectId: string | null) => {
  await act(async () => root.render(<WorkTaskSandbox projectId={projectId} />));
  await act(async () => {});
};
const view = (domains: string[]) => ({ project: "p1", network_allowed_domains: domains, revision: domains.length });

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  v1.callV1.mockReset();
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("WorkTaskSandbox", () => {
  it("says nothing is allowed when the list is empty, and states the scope", async () => {
    v1.callV1.mockResolvedValue(view([]));
    await show("p1");
    expect(v1.callV1).toHaveBeenCalledWith("/api/v1/projects/task_manager_sandbox_get", { project: "p1" }, expect.anything());
    expect(host.textContent).toContain("没有放行任何域名");
    expect(host.textContent).toContain("Go 编写的命令行工具");
    expect(host.querySelector("ul")).toBeNull();
  });

  it("lists the effective domains and offers no editing control", async () => {
    v1.callV1.mockResolvedValue(view(["registry.npmjs.org", "*.pypi.org"]));
    await show("p1");
    expect([...host.querySelectorAll("li")].map((item) => item.textContent)).toEqual(["registry.npmjs.org", "*.pypi.org"]);
    expect(host.querySelector("button, input, textarea")).toBeNull();
    expect(host.textContent).not.toContain("没有放行任何域名");
  });

  it("shows a failed read as a failure, never as an empty list", async () => {
    v1.callV1.mockRejectedValue(new V1Error(500, "internal", "database is down"));
    await show("p1");
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("database is down");
    expect(host.textContent).not.toContain("没有放行任何域名");
  });

  it("rejects an answer that is not the closed view of this project", async () => {
    v1.callV1.mockResolvedValue({ ...view(["a.example"]), project: "other" });
    await show("p1");
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    expect(host.textContent).not.toContain("a.example");
  });

  it("renders nothing for an older server without the operation, or without a project", async () => {
    v1.callV1.mockRejectedValue(new V1Error(404, "not_found", "no POST /api/v1/projects/task_manager_sandbox_get"));
    await show("p1");
    expect(host.textContent).toBe("");
    v1.callV1.mockClear();
    await show(null);
    expect(host.textContent).toBe("");
    expect(v1.callV1).not.toHaveBeenCalled();
  });
});
