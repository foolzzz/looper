// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectMemoryCloseout } from "@/components/project-memory-closeout";
import type { Project } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("project memory closeout", () => {
  let container: HTMLDivElement;
  let root: Root;
  const project = (mode?: "on") => ({ project_id: "project-example", memory_closeout: mode } as Project);
  const select = () => container.querySelector<HTMLSelectElement>("select")!;
  const save = () => container.querySelector<HTMLButtonElement>("button")!;
  const choose = async (mode: string) => act(async () => {
    select().value = mode;
    select().dispatchEvent(new Event("change", { bubbles: true }));
  });

  beforeEach(() => {
    resetSessionGuard();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    resetSessionGuard();
    vi.unstubAllGlobals();
  });

  it("is off when omitted and saves through its own operator operation", async () => {
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => Response.json({
      status: "ok", result: { memory_closeout: JSON.parse(String(init?.body)).memory_closeout },
    }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ProjectMemoryCloseout project={project()} />));
    expect(select().value).toBe("off");
    expect(save().disabled).toBe(true);
    expect(fetcher).not.toHaveBeenCalled();
    expect(container.textContent).toContain("proposes lessons and project facts");
    expect(container.textContent).toContain("Memory writing has the tasks source on");
    expect(container.querySelector("label")?.htmlFor).toBe(select().id);
    await choose("on");
    await act(async () => save().click());
    expect(fetcher).toHaveBeenLastCalledWith("/api/v1/memory_closeout/update", expect.objectContaining({
      body: JSON.stringify({ project_id: "project-example", memory_closeout: "on" }),
    }));
    expect(select().value).toBe("on");
    expect(save().disabled).toBe(true);
    expect(container.querySelector("[role=status]")?.textContent).toContain("saved");
  });

  it("reads the enabled mode from the project without adding it to the ordinary project form", async () => {
    await act(async () => root.render(<ProjectMemoryCloseout project={project("on")} />));
    expect(select().value).toBe("on");
    expect(save().type).toBe("button");
    expect(select().name).toBe("");
  });

  it("keeps the draft and displays a rejected save instead of claiming it was saved", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error", error: {
      code: "forbidden", message: "Operator access required.",
    } }, { status: 403 })));
    await act(async () => root.render(<ProjectMemoryCloseout project={project()} />));
    await choose("on");
    await act(async () => save().click());
    expect(container.querySelector("[role=alert]")?.textContent).toBe("Operator access required.");
    expect(container.querySelector("[role=status]")).toBeNull();
    expect(select().value).toBe("on");
    expect(save().disabled).toBe(false);
  });
});
