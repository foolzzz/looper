// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectAcceptanceChecks } from "@/components/project-acceptance-checks";
import type { Project } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("project acceptance checks", () => {
  let container: HTMLDivElement;
  let root: Root;
  const project = (mode?: "local" | "network") => ({ project_id: "project-example", acceptance_checks: mode } as Project);
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

  it("is off when omitted, explains execution limits, and saves modes through its own operator operation", async () => {
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => Response.json({
      status: "ok", result: { acceptance_checks: JSON.parse(String(init?.body)).acceptance_checks },
    }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ProjectAcceptanceChecks project={project()} />));
    expect(select().value).toBe("off");
    expect(save().disabled).toBe(true);
    expect(fetcher).not.toHaveBeenCalled();
    expect(container.textContent).toContain("does not execute checks");
    expect(container.textContent).toContain("server's user");
    expect(container.textContent).toContain("not a sandbox");
    expect(container.querySelector("label")?.htmlFor).toBe(select().id);
    for (const mode of ["local", "network", "off"]) {
      await choose(mode);
      await act(async () => save().click());
      expect(fetcher).toHaveBeenLastCalledWith("/api/v1/acceptance_checks/update", expect.objectContaining({
        body: JSON.stringify({ project_id: "project-example", acceptance_checks: mode }),
      }));
      expect(select().value).toBe(mode);
      expect(save().disabled).toBe(true);
    }
  });

  it("reads the enabled mode from the project without adding it to the ordinary project form", async () => {
    await act(async () => root.render(<ProjectAcceptanceChecks project={project("network")} />));
    expect(select().value).toBe("network");
    expect(save().type).toBe("button");
    expect(select().name).toBe("");
  });

  it("keeps the draft and displays a rejected save instead of claiming it was saved", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error", error: {
      code: "forbidden", message: "Operator access required.",
    } }, { status: 403 })));
    await act(async () => root.render(<ProjectAcceptanceChecks project={project()} />));
    await choose("local");
    await act(async () => save().click());
    expect(container.querySelector("[role=alert]")?.textContent).toBe("Operator access required.");
    expect(container.querySelector("[role=status]")).toBeNull();
    expect(select().value).toBe("local");
    expect(save().disabled).toBe(false);
  });
});

it("keeps the save confirmation when the project list refetches the saved mode", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { acceptance_checks: "local" } })));
  try {
    await act(async () => root.render(<ProjectAcceptanceChecks project={{ project_id: "example" } as Project} />));
    const select = container.querySelector("select")!;
    await act(async () => {
      select.value = "local";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => container.querySelector("button")!.click());
    await act(async () => root.render(<ProjectAcceptanceChecks project={{ project_id: "example", acceptance_checks: "local" } as Project} />));
    expect(container.querySelector("[role=status]")?.textContent).toContain("saved");
  } finally {
    act(() => root.unmount());
    vi.unstubAllGlobals();
    resetSessionGuard();
  }
});
